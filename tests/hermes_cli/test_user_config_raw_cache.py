"""``read_user_config_raw`` caches on (resolved path, mtime_ns, size).

Under profile multiplexing this function is called ~2/s per process against a ~100 KB
config.yaml (``profiles.list_profiles`` -> ``_profile_info`` -> ``_read_config_model`` /
``_served_by_running_multiplexer``, and ``env_loader._load_secrets_config`` for every
profile-scope entry). An idle-gateway py-spy profile attributed 89% of all active work to
re-parsing those files. These tests pin the cache AND the semantics it must not change.
"""
import logging
import os

import pytest

from hermes_cli import config as cfg_mod
from hermes_cli.config import read_user_config_raw


def _cache() -> dict:
    """The cache mapping, or an empty stand-in so the BEHAVIOUR tests below still execute
    (and fail on their assertions) against a build without the cache, instead of erroring
    out on a missing attribute and looking like an unrelated breakage."""
    return getattr(cfg_mod, "_USER_CONFIG_CACHE", {})


@pytest.fixture(autouse=True)
def _clear_cache():
    _cache().clear()
    yield
    _cache().clear()


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _count_parses(monkeypatch):
    """Wrap the YAML loader so tests can count actual parses (not just calls)."""
    calls = []
    real = cfg_mod.fast_safe_load

    def counting(stream, *a, **kw):
        calls.append(1)
        return real(stream, *a, **kw)

    monkeypatch.setattr(cfg_mod, "fast_safe_load", counting)
    return calls


def test_unchanged_file_is_parsed_once(tmp_path, monkeypatch):
    p = _write(tmp_path / "config.yaml", "model:\n  default: m1\n")
    calls = _count_parses(monkeypatch)
    for _ in range(5):
        assert read_user_config_raw(p) == {"model": {"default": "m1"}}
    assert len(calls) == 1, f"expected 1 parse for 5 reads, got {len(calls)}"


def test_changed_mtime_forces_reread(tmp_path, monkeypatch):
    p = _write(tmp_path / "config.yaml", "model:\n  default: m1\n")
    calls = _count_parses(monkeypatch)
    assert read_user_config_raw(p)["model"]["default"] == "m1"
    # Same size, new content, new mtime.
    _write(p, "model:\n  default: m2\n")
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert read_user_config_raw(p)["model"]["default"] == "m2"
    assert len(calls) == 2


def test_same_mtime_but_changed_size_forces_reread(tmp_path, monkeypatch):
    p = _write(tmp_path / "config.yaml", "model:\n  default: m1\n")
    st0 = p.stat()
    calls = _count_parses(monkeypatch)
    assert read_user_config_raw(p)["model"]["default"] == "m1"
    # Different size, mtime pinned back to the original value.
    _write(p, "model:\n  default: m1-longer-value\n")
    os.utime(p, ns=(st0.st_atime_ns, st0.st_mtime_ns))
    assert p.stat().st_mtime_ns == st0.st_mtime_ns
    assert p.stat().st_size != st0.st_size
    assert read_user_config_raw(p)["model"]["default"] == "m1-longer-value"
    assert len(calls) == 2


def test_distinct_profiles_never_share_an_entry(tmp_path, monkeypatch):
    a = _write(tmp_path / "profiles" / "alpha" / "config.yaml", "model:\n  default: A\n")
    b = _write(tmp_path / "profiles" / "beta" / "config.yaml", "model:\n  default: B\n")
    calls = _count_parses(monkeypatch)
    for _ in range(3):
        assert read_user_config_raw(a)["model"]["default"] == "A"
        assert read_user_config_raw(b)["model"]["default"] == "B"
    assert len(calls) == 2, "one parse per distinct file, then cached"
    assert len(_cache()) == 2


def test_same_file_via_different_spellings_shares_one_entry(tmp_path):
    p = _write(tmp_path / "config.yaml", "model:\n  default: m1\n")
    indirect = tmp_path / "sub" / ".." / "config.yaml"
    (tmp_path / "sub").mkdir(exist_ok=True)
    assert read_user_config_raw(p) == read_user_config_raw(indirect)
    assert len(_cache()) == 1, "resolved path keying collapses the alias"


def test_missing_file_semantics_unchanged(tmp_path):
    assert read_user_config_raw(tmp_path / "nope.yaml") == {}
    assert _cache() == {}, "a missing file must not create an entry"


def test_malformed_yaml_semantics_unchanged_and_not_cached(tmp_path):
    p = _write(tmp_path / "config.yaml", "model:\n  default: [unterminated\n")
    with pytest.raises(Exception):
        read_user_config_raw(p)
    assert _cache() == {}, "a failed parse must never poison the cache"
    # Repairing the file yields the real value — the failure left no sticky state.
    _write(p, "model:\n  default: ok\n")
    assert read_user_config_raw(p)["model"]["default"] == "ok"


def test_non_mapping_document_yields_empty_mapping(tmp_path):
    p = _write(tmp_path / "config.yaml", "- just\n- a\n- list\n")
    assert read_user_config_raw(p) == {}


def test_caller_mutation_cannot_corrupt_later_reads(tmp_path):
    """Callers mutate this result and write it back (slash_commands_model, yuanbao)."""
    p = _write(tmp_path / "config.yaml", "model:\n  default: m1\nkeep: yes\n")
    first = read_user_config_raw(p)
    first["model"]["default"] = "CLOBBERED"
    first["injected"] = True
    del first["keep"]
    second = read_user_config_raw(p)
    assert second["model"]["default"] == "m1"
    assert "injected" not in second
    assert second["keep"] is True
    assert second is not first


def test_replacement_via_rename_is_picked_up(tmp_path):
    """atomic_config_write replaces the file by rename; the cache must not serve the old parse."""
    p = tmp_path / "config.yaml"
    _write(p, "model:\n  default: old\n")
    assert read_user_config_raw(p)["model"]["default"] == "old"
    tmp = tmp_path / "config.yaml.tmp"
    _write(tmp, "model:\n  default: new-after-rename\n")
    os.replace(tmp, p)
    assert read_user_config_raw(p)["model"]["default"] == "new-after-rename"


def test_secret_values_never_logged(tmp_path, monkeypatch, caplog):
    secret = "sk-live-DO-NOT-LOG-4242424242"
    p = _write(tmp_path / "config.yaml",
               f"secrets:\n  vault:\n    enabled: true\n    token: {secret}\n")
    from hermes_cli import env_loader
    with caplog.at_level(logging.DEBUG):
        cfg = env_loader._load_secrets_config(tmp_path)
        assert read_user_config_raw(p)["secrets"]["vault"]["token"] == secret
    assert cfg["vault"]["token"] == secret
    assert secret not in caplog.text
    assert secret not in "".join(r.getMessage() for r in caplog.records)


def test_load_secrets_config_missing_and_malformed(tmp_path):
    from hermes_cli import env_loader
    assert env_loader._load_secrets_config(tmp_path) == {}          # no config.yaml
    _write(tmp_path / "config.yaml", "secrets:\n  a:\n    enabled: true\n")
    assert env_loader._load_secrets_config(tmp_path) == {"a": {"enabled": True}}
    _write(tmp_path / "config.yaml", "secrets: [unterminated\n")
    assert env_loader._load_secrets_config(tmp_path) == {}          # malformed -> {} as before


def test_load_secrets_config_result_is_not_shared(tmp_path):
    from hermes_cli import env_loader
    _write(tmp_path / "config.yaml", "secrets:\n  a:\n    enabled: true\n")
    first = env_loader._load_secrets_config(tmp_path)
    first["a"]["enabled"] = False
    first["injected"] = 1
    second = env_loader._load_secrets_config(tmp_path)
    assert second == {"a": {"enabled": True}}
