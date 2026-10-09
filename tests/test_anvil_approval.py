#!/usr/bin/env python3
"""Unit tests for swarmforge.anvil.approval. Run: python3 tests/test_anvil_approval.py"""

import io
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)

# Standing in for the launcher's entry-point shim keeps this file runnable on its own.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# `python3 -m unittest tests.<module>` does not put this directory on the path.
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# `anvil` is already these tests' word for the container the launcher wraps.
from swarmforge import anvil as launcher
from swarmforge import tongs

from anvil_fixtures import _merged


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
        self.assertIn("docker socket", text)

    def test_calls_out_requested_gpus(self):
        defn = dict(WORKSPACE_TONG, resources={"gpus": '"device=0,1"'})
        text = launcher.render_privilege_summary("github", tongs.privilege_summary(defn))
        self.assertIn('  gpus:     "device=0,1" (host GPU access)', text.splitlines())

    def test_non_mapping_resources_renders_no_gpus_line(self):
        for resources in ("8g", ["gpus"]):
            with self.subTest(resources=resources):
                defn = dict(WORKSPACE_TONG, resources=resources)
                text = launcher.render_privilege_summary("github", tongs.privilege_summary(defn))
                self.assertNotIn("gpus", text)

    def test_calls_out_a_volume_as_persistent(self):
        defn = dict(WORKSPACE_TONG, mounts=["volume:models:/models"])
        scope = (tongs.WORKSPACE_VOLUME_SCOPE, "/home/me/proj")
        text = launcher.render_privilege_summary(
            "cache", tongs.privilege_summary(defn, "cache", scope))
        self.assertIn("volume:models:/models", text)
        lines = text.splitlines()
        self.assertIn("  volume:   models at /models (docker volume %s)"
                      % tongs.tong_volume_name("cache", "models", scope), lines)
        self.assertIn(
            "            persistent: outlives the tong, is mounted by each of its concurrent "
            "sessions, and reuses any data left by an earlier definition or checkout at "
            "this path", lines)

    def test_omits_unrequested_sections(self):
        defn = {"image": "x", "interface": {"kind": "none"}, "resources": {"memory": "1g"}}
        text = launcher.render_privilege_summary("x", tongs.privilege_summary(defn))
        self.assertNotIn("secrets:", text)
        self.assertNotIn("volume", text)
        self.assertNotIn("docker socket", text)
        self.assertNotIn("gpus", text)

    def test_escape_sequences_in_every_field_are_escaped(self):
        cursor_up_erase = "\x1b[1A\x1b[2K"
        defn = {
            "image": "img" + cursor_up_erase,
            "env": {"T": "${secret:op%s:ref%s}" % (cursor_up_erase, cursor_up_erase)},
            "mounts": ["docker-socket", "m" + cursor_up_erase],
            "networks": ["n" + cursor_up_erase],
            "resources": {"gpus": "g" + cursor_up_erase},
        }
        text = launcher.render_privilege_summary("x", tongs.privilege_summary(defn))
        shown = "\\x1b[1A\\x1b[2K"
        self.assertNotIn("\x1b", text)
        self.assertEqual(
            text.split("\n"),
            [
                "Workspace tong 'x' requests approval:",
                "  image:    img" + shown,
                "  secrets:  op%s:ref%s" % (shown, shown),
                "  mounts:   docker-socket, m" + shown,
                "  networks: n" + shown,
                "  docker socket: full host docker control",
                "  gpus:     g%s (host GPU access)" % shown,
            ],
        )

    def test_conceal_in_a_secret_from_an_unknown_key_cannot_hide_later_lines(self):
        defn = {"x-note": "${secret:a:\x1b[8m}", "mounts": ["docker-socket"]}
        text = launcher.render_privilege_summary("x", tongs.privilege_summary(defn))
        self.assertNotIn("\x1b", text)
        self.assertEqual(
            text.split("\n")[2:],
            [
                "  secrets:  a:\\x1b[8m",
                "  mounts:   docker-socket",
                "  docker socket: full host docker control",
            ],
        )

    def test_line_breaks_in_a_value_cannot_forge_a_line(self):
        defn = {
            "image": "evil\r  image:    registry/trusted@sha256:abc",
            "mounts": ["docker-socket\n  networks: none"],
        }
        lines = launcher.render_privilege_summary(
            "x", tongs.privilege_summary(defn)
        ).split("\n")
        self.assertEqual(
            lines[1:],
            [
                "  image:    evil\\r  image:    registry/trusted@sha256:abc",
                "  mounts:   docker-socket\\n  networks: none",
            ],
        )

    def test_c1_del_bidi_and_zero_width_characters_are_escaped(self):
        defn = {"image": "a\x9b2Jb\x7fc\u202ed\u200be"}
        text = launcher.render_privilege_summary("x", tongs.privilege_summary(defn))
        self.assertIn("  image:    a\\x9b2Jb\\x7fc\\u202ed\\u200be", text)

    def test_plain_values_render_unchanged(self):
        defn = {
            "image": "registry/naïve-日本@sha256:abc",
            "env": {"T": "${secret:op:op://Wörk/token}"},
            "mounts": ["workspace:ro"],
            "networks": ["café"],
        }
        text = launcher.render_privilege_summary("ü", tongs.privilege_summary(defn))
        self.assertEqual(
            text,
            "Workspace tong 'ü' requests approval:\n"
            "  image:    registry/naïve-日本@sha256:abc\n"
            "  secrets:  op:op://Wörk/token\n"
            "  mounts:   workspace:ro\n"
            "  networks: café",
        )

    def test_non_string_values_still_render(self):
        defn = {"image": 5, "mounts": [7, ["a"]], "networks": [8]}
        text = launcher.render_privilege_summary("x", tongs.privilege_summary(defn))
        self.assertIn("  image:    5", text)
        self.assertIn("  mounts:   7, ['a']", text)
        self.assertIn("  networks: 8", text)


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

    def test_a_repo_sourced_tong_is_not_gated(self):
        merged = _merged("gh", WORKSPACE_TONG, source=tongs.REPO)
        self.assertEqual(self._gate(merged), "")
        self.assertFalse(os.path.exists(self.approvals))

    def test_accepted_workspace_tong_records_and_persists(self):
        merged = _merged("gh", WORKSPACE_TONG)
        out = self._gate(merged, answer="y\n")
        self.assertIn("gh", out)
        self.assertEqual(self._gate(merged), "")
        stored = tongs.load_approvals(self.approvals)
        self.assertTrue(tongs.is_approved(stored, self.ws, "gh", WORKSPACE_TONG))

    def test_prompt_names_the_volume_this_checkout_would_mount(self):
        merged = _merged("cache", dict(WORKSPACE_TONG, mounts=["volume:models:/models"]))
        out = self._gate(merged, answer="y\n")
        scope = tongs.volume_scope(tongs.WORKSPACE, workspace=self.ws)
        self.assertIn(tongs.tong_volume_name("cache", "models", scope), out)

    def test_declined_workspace_tong_raises_and_does_not_persist(self):
        merged = _merged("gh", WORKSPACE_TONG)
        with self.assertRaises(launcher.ApprovalDenied):
            self._gate(merged, answer="n\n")
        self.assertFalse(os.path.exists(self.approvals))

    def test_eof_reads_as_decline(self):
        merged = _merged("gh", WORKSPACE_TONG)
        with self.assertRaises(launcher.ApprovalDenied):
            self._gate(merged, answer="")

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
        self.assertEqual(self._gate(merged, prompt=False), "")

    def test_a_changed_definition_is_unapproved(self):
        merged = _merged("gh", WORKSPACE_TONG)
        self._gate(merged, answer="y\n")
        changed = dict(WORKSPACE_TONG, image="registry/github@sha256:def")
        with self.assertRaises(launcher.ApprovalDenied):
            self._gate(_merged("gh", changed), prompt=False)

    def test_adding_gpus_to_an_approved_tong_reprompts_and_shows_them(self):
        self._gate(_merged("gh", WORKSPACE_TONG), answer="y\n")
        changed = dict(WORKSPACE_TONG, resources={"gpus": "all"})
        out = self._gate(_merged("gh", changed), answer="y\n")
        self.assertIn("gpus:     all (host GPU access)", out)
        self.assertIn("Approve workspace tong 'gh'?", out)

    def test_prompt_output_carries_no_raw_control_characters(self):
        defn = dict(WORKSPACE_TONG, image="img\x1b[2J\x9b2J")
        out = self._gate(_merged("gh\x1b[8m", defn), answer="y\n")
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x9b", out)
        self.assertIn("Approve workspace tong 'gh\\x1b[8m'? [y/N]: ", out)

    def test_missing_workspace_path_fails_closed(self):
        merged = _merged("gh", WORKSPACE_TONG)
        with self.assertRaises(launcher.ApprovalDenied):
            self._gate(merged, answer="y\n", workspace="")


if __name__ == "__main__":
    unittest.main(verbosity=2)
