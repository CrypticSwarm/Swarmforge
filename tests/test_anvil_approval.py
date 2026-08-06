#!/usr/bin/env python3
"""Unit tests for swarmforge.anvil.approval. Run: python3 tests/test_anvil_approval.py"""

import io
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)

# The launcher's entry-point shim puts the repo root on the path; standing in
# for it here keeps this file runnable on its own, not just under a discovery
# run that already set it.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Discovery and `python3 tests/<file>.py` both put this directory on the path,
# but `python3 -m unittest tests.<module>` does not; the sibling fixture module
# has to import under all three.
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# Aliased because `anvil` is already these tests' word for the container
# the launcher wraps.
from swarmforge import anvil as launcher
from swarmforge import tongs

from anvil_fixtures import _merged


# A workspace-sourced tong that requests the privileges the gate must surface:
# a pinned image, a secret reference, a workspace mount, and docker-socket access.
WORKSPACE_TONG = {
    "lifecycle": "session",
    "image": "registry/github@sha256:abc",
    "env": {"GITHUB_TOKEN": "${secret:op:op://Work/github/token}"},
    "mounts": ["workspace:ro", "docker-socket"],
    "interface": {"kind": "none"},
    "readiness": {"mode": "none"},
}


class RenderPrivilegeSummaryTests(unittest.TestCase):
    def test_renders_requested_privileges(self):
        text = launcher.render_privilege_summary(
            "github", tongs.privilege_summary(WORKSPACE_TONG)
        )
        self.assertIn("github", text)
        self.assertIn("registry/github@sha256:abc", text)
        self.assertIn("op:op://Work/github/token", text)
        self.assertIn("workspace:ro", text)
        # Docker-socket access is the broadest grant and is always called out.
        self.assertIn("docker socket", text)

    def test_omits_unrequested_sections(self):
        defn = {"image": "x", "interface": {"kind": "none"}}
        text = launcher.render_privilege_summary("x", tongs.privilege_summary(defn))
        self.assertNotIn("secrets:", text)
        self.assertNotIn("docker socket", text)


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.approvals = os.path.join(self.tmp, "nested", "approvals.json")
        self.ws = "/home/me/proj"

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _gate(self, merged, answer="", prompt=True, workspace=None):
        out = io.StringIO()
        launcher.gate_workspace_tongs(
            merged,
            self.ws if workspace is None else workspace,
            self.approvals,
            prompt=prompt,
            out=out,
            inp=io.StringIO(answer),
        )
        return out.getvalue()

    def test_empty_set_is_inert(self):
        self.assertEqual(self._gate({}), "")
        self.assertFalse(os.path.exists(self.approvals))

    def test_trusted_tong_is_not_gated(self):
        # Only the workspace layer gates; a repo-sourced tong prints nothing and
        # records nothing, preserving the inert-when-trusted behavior.
        merged = _merged("gh", WORKSPACE_TONG, source=tongs.REPO)
        self.assertEqual(self._gate(merged), "")
        self.assertFalse(os.path.exists(self.approvals))

    def test_accepted_workspace_tong_records_and_persists(self):
        merged = _merged("gh", WORKSPACE_TONG)
        out = self._gate(merged, answer="y\n")
        self.assertIn("gh", out)
        # Persisted by workspace + name + hash, so a second pass is silent.
        self.assertEqual(self._gate(merged), "")
        stored = tongs.load_approvals(self.approvals)
        self.assertTrue(tongs.is_approved(stored, self.ws, "gh", WORKSPACE_TONG))

    def test_declined_workspace_tong_raises_and_does_not_persist(self):
        merged = _merged("gh", WORKSPACE_TONG)
        with self.assertRaises(launcher.ApprovalDenied):
            self._gate(merged, answer="n\n")
        self.assertFalse(os.path.exists(self.approvals))

    def test_eof_reads_as_decline(self):
        merged = _merged("gh", WORKSPACE_TONG)
        with self.assertRaises(launcher.ApprovalDenied):
            self._gate(merged, answer="")  # empty stdin => EOF => No

    def test_no_prompt_fails_closed_when_unapproved(self):
        merged = _merged("gh", WORKSPACE_TONG)
        with self.assertRaises(launcher.ApprovalDenied):
            self._gate(merged, prompt=False)
        self.assertFalse(os.path.exists(self.approvals))

    def test_no_prompt_passes_when_already_approved(self):
        merged = _merged("gh", WORKSPACE_TONG)
        tongs.save_approvals(
            self.approvals, tongs.record_approval({}, self.ws, "gh", WORKSPACE_TONG)
        )
        # Already approved => no prompt needed, so --no-prompt does not fail.
        self.assertEqual(self._gate(merged, prompt=False), "")

    def test_changed_definition_reprompts(self):
        merged = _merged("gh", WORKSPACE_TONG)
        self._gate(merged, answer="y\n")
        changed = dict(WORKSPACE_TONG, image="registry/github@sha256:def")
        # A new hash is unapproved, so the fail-closed path fires again.
        with self.assertRaises(launcher.ApprovalDenied):
            self._gate(_merged("gh", changed), prompt=False)

    def test_missing_workspace_path_fails_closed(self):
        merged = _merged("gh", WORKSPACE_TONG)
        with self.assertRaises(launcher.ApprovalDenied):
            self._gate(merged, answer="y\n", workspace="")


if __name__ == "__main__":
    unittest.main(verbosity=2)
