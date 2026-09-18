"""Tests for tools.subagent_write_contract: preflight path classification,
staging-lane routing, artifact manifests, and controlled commit."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from tools import subagent_write_contract as swc


class ClassifyOutputPathTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="swc_test_")
        self.safe_root = os.path.join(self._tmp, "safe_root")
        os.makedirs(self.safe_root, exist_ok=True)
        self._env_patch = mock.patch.dict(
            os.environ, {"HERMES_WRITE_SAFE_ROOT": self.safe_root}, clear=False,
        )
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_path_inside_safe_root_is_direct(self):
        target = os.path.join(self.safe_root, "report.md")
        decision = swc.classify_output_path(target)
        self.assertEqual(decision.lane, "direct")
        self.assertEqual(os.path.realpath(decision.write_target),
                          os.path.realpath(target))
        self.assertIsNone(decision.final_target)

    def test_path_outside_safe_root_is_staged(self):
        outside = os.path.join(self._tmp, "outside_dir", "report.md")
        decision = swc.classify_output_path(outside, staging_root=self.safe_root)
        self.assertEqual(decision.lane, "staged")
        self.assertTrue(decision.write_target.startswith(
            os.path.realpath(self.safe_root)))
        self.assertEqual(os.path.realpath(decision.final_target),
                          os.path.realpath(outside))

    def test_staged_write_targets_are_unique_per_call(self):
        outside = os.path.join(self._tmp, "outside_dir", "report.md")
        d1 = swc.classify_output_path(outside, staging_root=self.safe_root)
        d2 = swc.classify_output_path(outside, staging_root=self.safe_root)
        self.assertNotEqual(d1.write_target, d2.write_target,
                             "two classifications of the same final path must not collide")

    def test_classify_output_paths_batch(self):
        a = os.path.join(self.safe_root, "a.md")
        b = os.path.join(self._tmp, "outside", "b.md")
        decisions = swc.classify_output_paths([a, b], staging_root=self.safe_root)
        self.assertEqual(decisions[a].lane, "direct")
        self.assertEqual(decisions[b].lane, "staged")

    def test_no_safe_root_configured_means_everything_direct(self):
        with mock.patch.dict(os.environ, {"HERMES_WRITE_SAFE_ROOT": ""}, clear=False):
            somewhere = os.path.join(self._tmp, "anywhere", "x.md")
            decision = swc.classify_output_path(somewhere)
            self.assertEqual(decision.lane, "direct")


class PreflightBriefTests(unittest.TestCase):
    def test_empty_decisions_yields_empty_brief(self):
        self.assertEqual(swc.build_preflight_brief({}), "")

    def test_brief_mentions_direct_and_staged_paths(self):
        decisions = {
            "/safe/a.md": swc.PathDecision(
                requested_path="/safe/a.md", lane="direct", write_target="/safe/a.md"),
            "/outside/b.md": swc.PathDecision(
                requested_path="/outside/b.md", lane="staged",
                write_target="/stage/xyz_b.md", final_target="/outside/b.md"),
        }
        brief = swc.build_preflight_brief(decisions)
        self.assertIn("WRITE DIRECTLY", brief)
        self.assertIn("/safe/a.md", brief)
        self.assertIn("staging area", brief)
        self.assertIn("/stage/xyz_b.md", brief)
        self.assertIn("ArtifactManifest", brief)


class ArtifactManifestTests(unittest.TestCase):
    def test_round_trip_json(self):
        manifest = swc.ArtifactManifest(entries=[
            swc.ArtifactEntry(staged_path="/stage/a", final_path="/final/a",
                               size_bytes=123, sha256="deadbeef"),
        ])
        text = manifest.to_json()
        restored = swc.ArtifactManifest.from_json(text)
        self.assertEqual(len(restored.entries), 1)
        self.assertEqual(restored.entries[0].staged_path, "/stage/a")
        self.assertEqual(restored.entries[0].final_path, "/final/a")
        self.assertEqual(restored.entries[0].size_bytes, 123)
        self.assertEqual(restored.entries[0].sha256, "deadbeef")

    def test_from_json_tolerates_missing_optional_fields(self):
        text = json.dumps({"artifacts": [{"staged_path": "/s", "final_path": "/f"}]})
        restored = swc.ArtifactManifest.from_json(text)
        self.assertEqual(restored.entries[0].size_bytes, None)
        self.assertEqual(restored.entries[0].sha256, None)

    def test_manifest_instructions_mention_required_fields(self):
        instructions = swc.build_manifest_instructions()
        self.assertIn("staged_path", instructions)
        self.assertIn("final_path", instructions)


class CommitManifestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="swc_commit_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write(self, rel: str, content: str = "hello") -> str:
        path = os.path.join(self._tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path

    def test_commit_moves_staged_artifact_to_final_path(self):
        staged = self._write("stage/report.md", "content")
        final = os.path.join(self._tmp, "final", "report.md")
        manifest = swc.ArtifactManifest(entries=[
            swc.ArtifactEntry(staged_path=staged, final_path=final),
        ])
        results = swc.commit_manifest(manifest)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].ok, results[0].error)
        self.assertTrue(os.path.isfile(final))
        self.assertFalse(os.path.isfile(staged), "staged copy must be moved, not duplicated")
        with open(final, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "content")

    def test_commit_reports_missing_staged_artifact(self):
        manifest = swc.ArtifactManifest(entries=[
            swc.ArtifactEntry(staged_path=os.path.join(self._tmp, "missing.md"),
                               final_path=os.path.join(self._tmp, "final.md")),
        ])
        results = swc.commit_manifest(manifest)
        self.assertFalse(results[0].ok)
        self.assertIn("missing", results[0].error)

    def test_commit_refuses_credential_path_even_if_manifest_claims_it(self):
        staged = self._write("stage/sneaky", "content")
        home = os.path.realpath(os.path.expanduser("~"))
        credential_target = os.path.join(home, ".ssh", "id_rsa")
        manifest = swc.ArtifactManifest(entries=[
            swc.ArtifactEntry(staged_path=staged, final_path=credential_target),
        ])
        results = swc.commit_manifest(manifest)
        self.assertFalse(results[0].ok)
        self.assertIn("protected credential path", results[0].error)
        self.assertFalse(os.path.exists(credential_target),
                          "commit must never actually write a credential path")

    def test_dry_run_does_not_move_the_file(self):
        staged = self._write("stage/report.md", "content")
        final = os.path.join(self._tmp, "final", "report.md")
        manifest = swc.ArtifactManifest(entries=[
            swc.ArtifactEntry(staged_path=staged, final_path=final),
        ])
        results = swc.commit_manifest(manifest, dry_run=True)
        self.assertTrue(results[0].ok)
        self.assertTrue(os.path.isfile(staged), "dry_run must not move anything")
        self.assertFalse(os.path.isfile(final))

    def test_commit_creates_final_directory_if_missing(self):
        staged = self._write("stage/report.md", "content")
        final = os.path.join(self._tmp, "brand", "new", "dir", "report.md")
        manifest = swc.ArtifactManifest(entries=[
            swc.ArtifactEntry(staged_path=staged, final_path=final),
        ])
        results = swc.commit_manifest(manifest)
        self.assertTrue(results[0].ok, results[0].error)
        self.assertTrue(os.path.isfile(final))

    def test_multiple_entries_committed_independently(self):
        staged1 = self._write("stage/a.md")
        final1 = os.path.join(self._tmp, "final", "a.md")
        manifest = swc.ArtifactManifest(entries=[
            swc.ArtifactEntry(staged_path=staged1, final_path=final1),
            swc.ArtifactEntry(staged_path=os.path.join(self._tmp, "missing.md"),
                               final_path=os.path.join(self._tmp, "final", "b.md")),
        ])
        results = swc.commit_manifest(manifest)
        self.assertTrue(results[0].ok)
        self.assertFalse(results[1].ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
