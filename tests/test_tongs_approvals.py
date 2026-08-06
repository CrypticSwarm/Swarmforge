#!/usr/bin/env python3
"""Unit tests for swarmforge.tongs.approvals. Run: python3 tests/test_tongs_approvals.py"""

import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The launcher's entry-point shim puts the repo root on the path; standing in
# for it here keeps this file runnable on its own, not just under a discovery
# run that already set it.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from swarmforge import tongs

from tongs_fixtures import GITHUB_TONG, def_of


class ConfigHashTests(unittest.TestCase):
    def test_order_independent_and_stable(self):
        a = tongs.config_hash({"image": "x", "lifecycle": "session"})
        b = tongs.config_hash({"lifecycle": "session", "image": "x"})
        self.assertEqual(a, b)
        self.assertEqual(len(a), 64)  # sha256 hex

    def test_changes_when_definition_changes(self):
        base = def_of(GITHUB_TONG)
        changed = def_of(GITHUB_TONG)
        changed["image"] = "ghcr.io/crypticswarm/github-tong@sha256:DIFFERENT"
        self.assertNotEqual(tongs.config_hash(base), tongs.config_hash(changed))


class PrivilegeSummaryTests(unittest.TestCase):
    def test_summary_reports_requested_privileges(self):
        defn = def_of(GITHUB_TONG)
        defn["mounts"] = ["workspace:ro", "docker-socket"]
        summary = tongs.privilege_summary(defn)
        self.assertEqual(summary["image"], defn["image"])
        self.assertEqual(summary["secrets"], [{"provider": "op", "ref": "op://Work/github/token"}])
        self.assertEqual(summary["networks"], ["some-existing-net"])
        self.assertTrue(summary["socket"])

    def test_no_socket_without_mount(self):
        self.assertFalse(tongs.privilege_summary(def_of(GITHUB_TONG))["socket"])


class ApprovalKeyingTests(unittest.TestCase):
    def setUp(self):
        self.ws = "/home/me/project"
        self.defn = def_of(GITHUB_TONG)

    def test_unapproved_then_recorded(self):
        approvals = {}
        self.assertFalse(tongs.is_approved(approvals, self.ws, "github", self.defn))
        tongs.record_approval(approvals, self.ws, "github", self.defn)
        self.assertTrue(tongs.is_approved(approvals, self.ws, "github", self.defn))

    def test_definition_change_reprompts(self):
        approvals = {}
        tongs.record_approval(approvals, self.ws, "github", self.defn)
        changed = def_of(GITHUB_TONG)
        changed["image"] = "ghcr.io/crypticswarm/github-tong@sha256:MOVED"
        self.assertFalse(tongs.is_approved(approvals, self.ws, "github", changed))

    def test_keyed_by_workspace_path(self):
        approvals = {}
        tongs.record_approval(approvals, self.ws, "github", self.defn)
        self.assertFalse(tongs.is_approved(approvals, "/other/ws", "github", self.defn))

    def test_load_save_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "approvals.json")
            approvals = tongs.record_approval({}, self.ws, "github", self.defn)
            tongs.save_approvals(path, approvals)
            self.assertTrue(tongs.is_approved(tongs.load_approvals(path), self.ws, "github", self.defn))

    def test_is_approved_tolerates_malformed_store(self):
        # A hand-edited store with a non-dict workspace value must not crash.
        self.assertFalse(tongs.is_approved({self.ws: "junk"}, self.ws, "github", self.defn))

    def test_load_missing_or_corrupt_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(tongs.load_approvals(os.path.join(tmp, "nope.json")), {})
            bad = os.path.join(tmp, "bad.json")
            with open(bad, "w") as f:
                f.write("{not json")
            self.assertEqual(tongs.load_approvals(bad), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
