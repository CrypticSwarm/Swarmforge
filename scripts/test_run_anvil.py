#!/usr/bin/env python3
"""Unit tests for scripts/run_anvil.py. Run: python3 scripts/test_run_anvil.py"""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE_PATH = os.path.join(HERE, "run_anvil.py")
spec = importlib.util.spec_from_file_location("run_anvil", MODULE_PATH)
run_anvil = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_anvil)
tongs = run_anvil.tongs


# A docker invocation shaped like the one run_agent_container builds: the
# interactive/remove flags, name, network, injected env/mounts, image, and the
# harness args. The launcher must forward this verbatim when no tongs exist.
ANVIL_ARGV = [
    "docker", "run", "-it", "--rm", "--name", "claude-myproject",
    "--network", "opencode-net",
    "-e", "OPENCODE_UID=1000",
    "-e", "TZ=Etc/UTC",
    "-v", "/home/me/proj:/workspace",
    # A path with a space exercises that a single argv word is forwarded whole,
    # never re-split, through the real execvp.
    "-v", "/home/me/my proj:/repos/me/my proj",
    "claude-code:local",
    "--some-harness-arg",
]


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


def _merged(name, defn, source=tongs.WORKSPACE):
    """A one-entry merged set as merge_tongs would return it."""
    return {name: {"source": source, "definition": defn}}


class ParseArgsTests(unittest.TestCase):
    def test_splits_layers_and_command_at_separator(self):
        opts, cmd = run_anvil.parse_args(
            ["--repo-tongs", "/r", "--workspace-tongs", "/w", "--", "docker", "run", "img"]
        )
        self.assertEqual(opts.layer_dirs, [(tongs.REPO, "/r"), (tongs.WORKSPACE, "/w")])
        self.assertEqual(cmd, ["docker", "run", "img"])

    def test_layers_ordered_canonically_regardless_of_flag_order(self):
        opts, _ = run_anvil.parse_args(
            ["--workspace-tongs", "/w", "--user-tongs", "/u", "--", "x"]
        )
        # USER precedes WORKSPACE in canonical precedence even though the
        # workspace flag came first.
        self.assertEqual(opts.layer_dirs, [(tongs.USER, "/u"), (tongs.WORKSPACE, "/w")])

    def test_no_layer_flags_is_valid(self):
        opts, cmd = run_anvil.parse_args(["--", "docker", "run", "img"])
        self.assertEqual(opts.layer_dirs, [])
        self.assertEqual(cmd, ["docker", "run", "img"])

    def test_approval_options_default_to_inert(self):
        opts, _ = run_anvil.parse_args(["--", "x"])
        self.assertIsNone(opts.workspace)
        self.assertIsNone(opts.approvals)
        self.assertFalse(opts.no_prompt)

    def test_parses_workspace_approvals_and_no_prompt(self):
        opts, cmd = run_anvil.parse_args(
            ["--workspace", "/ws", "--approvals", "/a.json", "--no-prompt", "--", "x"]
        )
        self.assertEqual(opts.workspace, "/ws")
        self.assertEqual(opts.approvals, "/a.json")
        self.assertTrue(opts.no_prompt)
        self.assertEqual(cmd, ["x"])

    def test_workspace_without_value_raises(self):
        with self.assertRaises(run_anvil.UsageError):
            run_anvil.parse_args(["--workspace"])

    def test_approvals_without_value_raises(self):
        with self.assertRaises(run_anvil.UsageError):
            run_anvil.parse_args(["--approvals"])

    def test_missing_separator_raises(self):
        with self.assertRaises(run_anvil.UsageError):
            run_anvil.parse_args(["--repo-tongs", "/r", "docker", "run"])

    def test_empty_command_after_separator_raises(self):
        with self.assertRaises(run_anvil.UsageError):
            run_anvil.parse_args(["--repo-tongs", "/r", "--"])

    def test_flag_without_value_raises(self):
        with self.assertRaises(run_anvil.UsageError):
            run_anvil.parse_args(["--repo-tongs"])

    def test_unknown_argument_raises(self):
        with self.assertRaises(run_anvil.UsageError):
            run_anvil.parse_args(["--bogus", "/r", "--", "x"])

    def test_command_tokens_are_preserved_even_if_they_look_like_flags(self):
        # Everything after '--' is the command; a later '--' or a tong-looking
        # flag inside it is data, not parsed.
        _, cmd = run_anvil.parse_args(["--", "docker", "run", "--user-tongs", "--"])
        self.assertEqual(cmd, ["docker", "run", "--user-tongs", "--"])


class DiscoverTongsTests(unittest.TestCase):
    def test_no_layers_is_empty(self):
        self.assertEqual(run_anvil.discover_tongs([]), {})

    def test_missing_dirs_are_empty(self):
        # The inert-when-empty basis: absent layer dirs discover nothing.
        layer_dirs = [(tongs.REPO, "/nonexistent/tongs"), (tongs.WORKSPACE, "/also/missing")]
        self.assertEqual(run_anvil.discover_tongs(layer_dirs), {})

    def test_discovers_a_present_definition(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "gh.yaml"), "w") as handle:
                handle.write("lifecycle: session\nimage: x\ninterface:\n  kind: none\n")
            merged = run_anvil.discover_tongs([(tongs.WORKSPACE, tmp)])
            self.assertEqual(sorted(merged), ["gh"])


class MainErrorTests(unittest.TestCase):
    def test_bad_args_return_two_without_exec(self):
        # main() reports usage and returns 2 for malformed argv; it must not
        # reach exec_anvil (which would replace the test process).
        self.assertEqual(run_anvil.main(["--repo-tongs", "/r"]), 2)
        self.assertEqual(run_anvil.main([]), 2)

    def test_unexecutable_anvil_returns_127(self):
        # A missing anvil binary yields the shell's uninvocable-command status
        # instead of an uncaught OSError. exec_anvil returns here because the
        # exec fails, so the test process is not replaced.
        self.assertEqual(run_anvil.exec_anvil(["/no/such/binary-xyz"]), 127)


def _run_launcher(extra_args):
    """Invoke run_anvil.py in a child process and capture the execed argv.

    The anvil "command" is a tiny python program that prints the argv it
    receives as JSON. Because the launcher execs it, the JSON we read back is
    exactly the argv the launcher forwarded -- letting us assert the forwarded
    command byte-for-byte through a real os.execvp.
    """
    echo = [sys.executable, "-c", "import sys, json; sys.stdout.write(json.dumps(sys.argv[1:]))"]
    argv = [sys.executable, MODULE_PATH] + extra_args + ["--"] + echo + ANVIL_ARGV
    completed = subprocess.run(argv, capture_output=True, text=True, check=True)
    return json.loads(completed.stdout), completed.stderr


class PassthroughInvariantTests(unittest.TestCase):
    """No tongs discovered => the anvil argv is forwarded byte-identically."""

    def test_no_tongs_forwards_anvil_argv_verbatim(self):
        forwarded, stderr = _run_launcher(["--repo-tongs", "/nonexistent/tongs"])
        self.assertEqual(forwarded, ANVIL_ARGV)
        # Nothing about tongs is reported when none are discovered.
        self.assertNotIn("tong", stderr)

    def test_no_layer_flags_forwards_anvil_argv_verbatim(self):
        forwarded, _ = _run_launcher([])
        self.assertEqual(forwarded, ANVIL_ARGV)

    def test_missing_workspace_tongs_dir_forwards_verbatim(self):
        # The workspace layer is always passed by the macro, even when its dir
        # does not exist; an absent dir must stay inert, not error.
        forwarded, stderr = _run_launcher(["--workspace-tongs", "/no/such/.swarmforge/tongs"])
        self.assertEqual(forwarded, ANVIL_ARGV)
        self.assertNotIn("tong", stderr)

    def test_present_trusted_tong_still_forwards_anvil_argv_unchanged(self):
        # A trusted-layer (repo) tong is discovered but not gated, and the
        # launcher does not rewrite the anvil command; it warns and runs the
        # anvil as given. (A workspace tong would gate -- see GateTests.)
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "gh.yaml"), "w") as handle:
                handle.write("lifecycle: session\nimage: x\ninterface:\n  kind: none\n")
            forwarded, stderr = _run_launcher(["--repo-tongs", tmp])
            self.assertEqual(forwarded, ANVIL_ARGV)
            self.assertIn("gh", stderr)


def _run_launcher_raw(extra_args, stdin_text=None):
    """Invoke run_anvil.py in a child process without asserting success.

    Like `_run_launcher` but returns the raw CompletedProcess so tests can
    inspect a non-zero exit (e.g. a denied approval that must not exec the
    anvil). `stdin_text` is fed to the launcher's stdin.
    """
    echo = [sys.executable, "-c", "import sys, json; sys.stdout.write(json.dumps(sys.argv[1:]))"]
    argv = [sys.executable, MODULE_PATH] + extra_args + ["--"] + echo + ANVIL_ARGV
    return subprocess.run(argv, input=stdin_text, capture_output=True, text=True)


class DefaultApprovalsPathTests(unittest.TestCase):
    def setUp(self):
        self.saved = os.environ.get("SWARMFORGE_USER_ASSETS_DIR")

    def tearDown(self):
        if self.saved is None:
            os.environ.pop("SWARMFORGE_USER_ASSETS_DIR", None)
        else:
            os.environ["SWARMFORGE_USER_ASSETS_DIR"] = self.saved

    def test_honors_user_assets_dir(self):
        os.environ["SWARMFORGE_USER_ASSETS_DIR"] = "/opt/sf"
        self.assertEqual(run_anvil.default_approvals_path(), "/opt/sf/approvals.json")

    def test_falls_back_to_home_swarmforge(self):
        os.environ.pop("SWARMFORGE_USER_ASSETS_DIR", None)
        expected = os.path.join(os.path.expanduser("~"), ".swarmforge", "approvals.json")
        self.assertEqual(run_anvil.default_approvals_path(), expected)


class RenderPrivilegeSummaryTests(unittest.TestCase):
    def test_renders_requested_privileges(self):
        text = run_anvil.render_privilege_summary(
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
        text = run_anvil.render_privilege_summary("x", tongs.privilege_summary(defn))
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
        run_anvil.gate_workspace_tongs(
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
        with self.assertRaises(run_anvil.ApprovalDenied):
            self._gate(merged, answer="n\n")
        self.assertFalse(os.path.exists(self.approvals))

    def test_eof_reads_as_decline(self):
        merged = _merged("gh", WORKSPACE_TONG)
        with self.assertRaises(run_anvil.ApprovalDenied):
            self._gate(merged, answer="")  # empty stdin => EOF => No

    def test_no_prompt_fails_closed_when_unapproved(self):
        merged = _merged("gh", WORKSPACE_TONG)
        with self.assertRaises(run_anvil.ApprovalDenied):
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
        with self.assertRaises(run_anvil.ApprovalDenied):
            self._gate(_merged("gh", changed), prompt=False)

    def test_missing_workspace_path_fails_closed(self):
        merged = _merged("gh", WORKSPACE_TONG)
        with self.assertRaises(run_anvil.ApprovalDenied):
            self._gate(merged, answer="y\n", workspace="")


class MainGateTests(unittest.TestCase):
    """main() stops before exec when a workspace tong is unapproved."""

    def _workspace_tongs_dir(self, tmp):
        tongs_dir = os.path.join(tmp, "tongs")
        os.makedirs(tongs_dir)
        with open(os.path.join(tongs_dir, "gh.yaml"), "w") as handle:
            handle.write("lifecycle: session\nimage: x\ninterface:\n  kind: none\n")
        return tongs_dir

    def test_no_prompt_unapproved_returns_one_without_exec(self):
        # The gate raises before exec_anvil, so main returns 1 in-process (the
        # test process is not replaced).
        with tempfile.TemporaryDirectory() as tmp:
            tongs_dir = self._workspace_tongs_dir(tmp)
            rc = run_anvil.main(
                [
                    "--workspace-tongs", tongs_dir,
                    "--workspace", tmp,
                    "--approvals", os.path.join(tmp, "approvals.json"),
                    "--no-prompt",
                    "--", "/no/such/binary-xyz",
                ]
            )
            self.assertEqual(rc, 1)

    def test_no_prompt_unapproved_does_not_forward_anvil(self):
        with tempfile.TemporaryDirectory() as tmp:
            tongs_dir = self._workspace_tongs_dir(tmp)
            completed = _run_launcher_raw(
                [
                    "--workspace-tongs", tongs_dir,
                    "--workspace", tmp,
                    "--approvals", os.path.join(tmp, "approvals.json"),
                    "--no-prompt",
                ]
            )
            self.assertNotEqual(completed.returncode, 0)
            # The echo anvil never ran, so nothing was forwarded.
            self.assertEqual(completed.stdout, "")
            self.assertIn("fails closed", completed.stderr)

    def test_approved_workspace_tong_forwards_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            tongs_dir = self._workspace_tongs_dir(tmp)
            defn = tongs.load_tong_file(os.path.join(tongs_dir, "gh.yaml"))
            approvals_path = os.path.join(tmp, "approvals.json")
            tongs.save_approvals(
                approvals_path, tongs.record_approval({}, tmp, "gh", defn)
            )
            forwarded, stderr = _run_launcher(
                [
                    "--workspace-tongs", tongs_dir,
                    "--workspace", tmp,
                    "--approvals", approvals_path,
                ]
            )
            self.assertEqual(forwarded, ANVIL_ARGV)
            self.assertIn("gh", stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
