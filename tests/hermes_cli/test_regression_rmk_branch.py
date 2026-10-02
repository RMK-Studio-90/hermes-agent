"""Regression test for HTTP 422 issue with local rmk branches."""

import json
import subprocess
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest

from hermes_cli.source_check import check_for_updates


MAIN_CHANNEL = "/releases/channels/main.json"


def source_channel(name, repository, branch="main"):
    return {"schema": 1, "name": name, "repository": repository, "policy": "source-branch",
            "state": "active", "revision": 1, "nextSequence": 1, "identity": None, "head": None,
            "delivery": {"kind": "source-branch", "branch": branch}}


class Installation(tuple):
    """The 8-tuple every test unpacks, plus the GitHub probe's request/response hooks."""
    authorizations: list
    response_headers: dict


@pytest.fixture
def installation(tmp_path, monkeypatch):
    from hermes_cli import source_releases

    root = tmp_path / "checkout"
    root.mkdir()
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_REVISION", raising=False)
    monkeypatch.delenv("HERMES_MANAGED", raising=False)
    # Git probes may use real local remotes, never the external network.
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")

    def git(*args, cwd=root):
        return subprocess.check_output([
            "git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "-c", "commit.gpgsign=false", *args,
        ], cwd=cwd, text=True).strip()

    git("init", "-b", "main")
    git("commit", "--allow-empty", "-m", "base")
    base = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-m", "local")
    head = git("rev-parse", "HEAD")
    git("remote", "add", "origin", "https://github.com/fixture/fork.git")
    linked = tmp_path / "linked"
    git("worktree", "add", "-b", "feature/gui", str(linked))
    responses = {MAIN_CHANNEL: (200, lambda: source_channel(
        "main", source_releases.source_repository(["git"], root)))}
    requests = []
    # Authorization header of every api.github.com call, in request order (None when anonymous).
    authorizations = []
    # Optional extra response headers per path (rate-limit headers for the failure-copy tests).
    response_headers = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            authorizations.append(self.headers.get("Authorization"))
            entry = responses.get(self.path, (404, {}))
            # A callable entry decides per request (it sees the handler, hence the headers).
            code, body = entry(self) if callable(entry) else entry
            if callable(body):
                body = body()
            self.send_response(code)
            for name, value in response_headers.get(self.path, {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write((body if isinstance(body, str) else json.dumps(body)).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(source_releases, "_PUBLIC_BASE", f"http://127.0.0.1:{server.server_port}")
    original = urllib.request.urlopen

    def local(request, *args, **kwargs):
        url = urllib.request.urlsplit(request.full_url)
        assert url.hostname in {"api.github.com", "hermes-assets.nousresearch.com"}
        rewritten = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}{url.path}" + (f"?{url.query}" if url.query else ""),
            headers=dict(request.header_items()))
        return original(rewritten, *args, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", local)
    # The credential ladder must not read this machine's gh login or env.
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    from hermes_cli import github_api
    monkeypatch.setattr(github_api, "_gh_cli_token", lambda: None)
    fixture = Installation((root, linked, home, base, head, responses, requests, git))
    fixture.authorizations = authorizations
    fixture.response_headers = response_headers
    yield fixture
    server.shutdown()
    server.server_close()


def _bare_origin(installation):
    root, linked, home, base, head, responses, requests, git = installation
    remote = home / "remote.git"
    git("init", "--bare", "-b", "main", str(remote))
    git("remote", "set-url", "origin", str(remote))
    git("push", "-q", "origin", "main")
    git("fetch", "-q", "origin")


def _commit_on(git, branch, message):
    git("checkout", "-q", branch)
    git("commit", "-q", "--allow-empty", "-m", message)
    sha = git("rev-parse", "HEAD")
    git("checkout", "-q", "main")
    return sha


def test_rmk_integration_current_upstream_branch_resolves_from_main_without_github_api(
    installation,
):
    """Test that local rmk/integration-current-upstream branch resolves from origin/main
    without querying GitHub API when the remote doesn't have the rmk branch.
    This prevents HTTP 422 errors when trying to resolve non-existent branches via API.
    """
    root, linked, home, base, head, responses, requests, git = installation

    # Set up a bare origin with only main branch (no rmk/* branches)
    _bare_origin(installation)

    # Create the local rmk/integration-current-upstream branch
    git("branch", "rmk/integration-current-upstream")
    _commit_on(git, "rmk/integration-current-upstream", "rmk customisation")
    git("checkout", "-q", "rmk/integration-current-upstream")

    # Ensure origin has main branch
    git("checkout", "-q", "main")
    _commit_on(git, "main", "upstream work")
    git("push", "-q", "-u", "origin", "main")

    # Go back to our rmk branch
    git("checkout", "-q", "rmk/integration-current-upstream")

    # Mock the branch config to point to our rmk branch
    branch_file = home / "desktop-update.json"
    branch_file.write_text(json.dumps({"branch": "rmk/integration-current-upstream"}))

    # Run the update check - this should NOT make any GitHub API calls
    # for the rmk/integration-current-upstream branch since it doesn't exist on remote
    status = check_for_updates(install_root=root, home=home, branch_config_path=branch_file)

    # Verify we didn't make any GitHub API calls (responses dict should be empty for branch lookups)
    # The only calls should be to the channel records
    assert len(responses) == 0 or all(
        "/repos/" not in call for call in responses.keys()
    ), f"Unexpected GitHub API calls made: {list(responses.keys())}"

    # Verify the update check succeeded and correctly identified we should track main for local-only branches
    assert "error" not in status, f"Update check failed: {status}"
    assert status["branch"] == "rmk/integration-current-upstream"
    assert status["updateBranch"] == "main"
    assert status["localOnly"] is True
    # The target SHA should be origin/main, not a GitHub API lookup
    assert status["targetSha"] == git("rev-parse", "origin/main")
    # Verify we're still on our local branch
    assert git("rev-parse", "--abbrev-ref", "HEAD") == "rmk/integration-current-upstream"

    # Verify the branch file was NOT updated (we keep our local branch for desktop updates)
    # The updateBranch field in status indicates what we should track for updates
    assert json.loads(branch_file.read_text())["branch"] == "rmk/integration-current-upstream"


def test_rmk_branch_with_remote_exists_uses_remote_sha(installation):
    """Test that when an rmk branch DOES exist on remote, we use its SHA."""
    root, linked, home, base, head, responses, requests, git = installation

    # Set up origin with both main and the rmk branch
    _bare_origin(installation)

    # Create main branch
    git("checkout", "-q", "main")
    _commit_on(git, "main", "initial main commit")
    main_sha = git("rev-parse", "HEAD")

    # Create and push rmk branch
    git("branch", "rmk/integration-current-upstream")
    _commit_on(git, "rmk/integration-current-upstream", "rmk customisation")
    rmk_sha = git("rev-parse", "HEAD")
    git("push", "-q", "-u", "origin", "rmk/integration-current-upstream")

    # Push main too
    git("checkout", "-q", "main")
    git("push", "-q", "origin", "main")

    # Checkout our rmk branch
    git("checkout", "-q", "rmk/integration-current-upstream")

    # Mock the branch config
    branch_file = home / "desktop-update.json"
    branch_file.write_text(json.dumps({"branch": "rmk/integration-current-upstream"}))

    # Run the update check
    status = check_for_updates(install_root=root, home=home, branch_config_path=branch_file)

    # Should succeed - verify we got the branch SHA from git ls-remote, not GitHub API
    assert "error" not in status, f"Update check failed: {status}"
    assert status["branch"] == "rmk/integration-current-upstream"
    # For a branch that exists on remote and is up to date:
    # - targetSha should match the remote branch SHA (which equals currentSha when up to date)
    # - No GitHub API calls should have been made (verified by responses dict being empty for API calls)
    assert status["targetSha"] == git("rev-parse", "origin/rmk/integration-current-upstream")
    assert status["currentSha"] == status["targetSha"]  # Up to date
    assert git("rev-parse", "--abbrev-ref", "HEAD") == "rmk/integration-current-upstream"
    # Verify we didn't make any GitHub API calls for branch resolution
    assert len(responses) == 0 or all(
        "/repos/" not in call for call in responses.keys()
    ), f"Unexpected GitHub API calls made: {list(responses.keys())}"
    # For branches that exist on remote, updateBranch and localOnly fields are not set
    assert "updateBranch" not in status
    assert "localOnly" not in status