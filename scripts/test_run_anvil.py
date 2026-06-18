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
from unittest import mock

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
        self.assertIsNone(opts.anvil_image)
        self.assertFalse(opts.no_prompt)

    def test_parses_workspace_approvals_and_no_prompt(self):
        opts, cmd = run_anvil.parse_args(
            ["--workspace", "/ws", "--approvals", "/a.json",
             "--anvil-image", "anvil:img", "--no-prompt", "--", "x"]
        )
        self.assertEqual(opts.workspace, "/ws")
        self.assertEqual(opts.approvals, "/a.json")
        self.assertEqual(opts.anvil_image, "anvil:img")
        self.assertTrue(opts.no_prompt)
        self.assertEqual(cmd, ["x"])

    def test_anvil_image_without_value_raises(self):
        with self.assertRaises(run_anvil.UsageError):
            run_anvil.parse_args(["--anvil-image"])

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

    def test_launcher_flags_do_not_leak_into_anvil_argv(self):
        # The Makefile always passes --anvil-image; with no tongs it is consumed
        # by the launcher and the anvil argv is forwarded unchanged.
        forwarded, stderr = _run_launcher([
            "--anvil-image", "opencode:local",
            "--repo-tongs", "/nonexistent/tongs",
        ])
        self.assertEqual(forwarded, ANVIL_ARGV)
        self.assertNotIn("tong", stderr)


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


class SecretResolverTests(unittest.TestCase):
    """make_secret_resolver shells out to the provider CLI and reports failures."""

    # Portable provider commands built on the test interpreter so the suite does
    # not depend on op/pass/echo being installed. "{ref}" is substituted by
    # tongs.secret_provider_command before exec.
    def _writes(self, expr):
        return [sys.executable, "-c", "import sys; sys.stdout.write(%s)" % expr, "{ref}"]

    def test_resolves_ref_via_provider_cli(self):
        resolve = run_anvil.make_secret_resolver({"echo": self._writes("sys.argv[1]")})
        self.assertEqual(resolve("echo", "op://Work/secret"), "op://Work/secret")

    def test_provider_stderr_inherits_terminal(self):
        with mock.patch.object(run_anvil.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(["provider"], 0, stdout=b"secret\n")
            resolve = run_anvil.make_secret_resolver({"p": ["provider", "{ref}"]})
            self.assertEqual(resolve("p", "ref"), "secret")
            self.assertIsNone(run.call_args.kwargs.get("stderr"))

    def test_strips_single_trailing_newline(self):
        resolve = run_anvil.make_secret_resolver({"echo": self._writes("sys.argv[1] + '\\n'")})
        self.assertEqual(resolve("echo", "token"), "token")

    def test_preserves_inner_and_other_whitespace(self):
        # Only one trailing newline is stripped; interior/extra newlines survive.
        resolve = run_anvil.make_secret_resolver({"echo": self._writes("sys.argv[1] + '\\n\\n'")})
        self.assertEqual(resolve("echo", "a\nb"), "a\nb\n")

    def test_unknown_provider_raises(self):
        resolve = run_anvil.make_secret_resolver({"op": ["op", "read", "{ref}"]})
        with self.assertRaises(run_anvil.SecretResolutionError):
            resolve("vault", "x")

    def test_nonzero_exit_raises(self):
        resolve = run_anvil.make_secret_resolver(
            {"boom": [sys.executable, "-c", "import sys; sys.exit(3)"]}
        )
        with self.assertRaises(run_anvil.SecretResolutionError):
            resolve("boom", "x")

    def test_unrunnable_provider_raises(self):
        resolve = run_anvil.make_secret_resolver({"missing": ["/no/such/binary-xyz", "{ref}"]})
        with self.assertRaises(run_anvil.SecretResolutionError):
            resolve("missing", "x")

    def test_error_message_never_contains_the_secret(self):
        # A failing CLI must not surface the resolved value; here it prints the
        # ref to stderr and fails, and the error names provider/ref (which are
        # not secret) -- the resolver never reaches a secret value on failure.
        resolve = run_anvil.make_secret_resolver(
            {"boom": [sys.executable, "-c", "import sys; sys.exit(1)"]}
        )
        with self.assertRaises(run_anvil.SecretResolutionError) as ctx:
            resolve("boom", "ref-token")
        self.assertIn("boom", str(ctx.exception))

    def test_drives_plan_tong_secrets_end_to_end(self):
        # The resolver is the impure half of tongs.plan_tong_secrets: a secret
        # env var ends up as a tmpfs file, never as a -e value.
        resolve = run_anvil.make_secret_resolver({"echo": self._writes("sys.argv[1]")})
        plan = tongs.plan_tong_secrets({"TOKEN": "${secret:echo:s3cr3t}"}, resolve)
        self.assertEqual(plan["files"], {"/run/swarmforge/secrets/TOKEN": "s3cr3t"})
        self.assertEqual(plan["env"], {"TOKEN_FILE": "/run/swarmforge/secrets/TOKEN"})


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

    def test_approved_workspace_tong_passes_gate_then_refused_as_unsupported(self):
        # Approval is no longer the only gate: an approved (and otherwise valid)
        # workspace tong clears the approval prompt but, being a `session` tong, is
        # then refused as unsupported -- proving the gate passed without the anvil
        # ever running.
        with tempfile.TemporaryDirectory() as tmp:
            tongs_dir = os.path.join(tmp, "tongs")
            os.makedirs(tongs_dir)
            with open(os.path.join(tongs_dir, "gh.yaml"), "w") as handle:
                handle.write(
                    "lifecycle: session\nimage: x\ninterface:\n  kind: none\n"
                    "readiness:\n  mode: none\n"
                )
            defn = tongs.load_tong_file(os.path.join(tongs_dir, "gh.yaml"))
            approvals_path = os.path.join(tmp, "approvals.json")
            tongs.save_approvals(
                approvals_path, tongs.record_approval({}, tmp, "gh", defn)
            )
            completed = _run_launcher_raw(
                [
                    "--workspace-tongs", tongs_dir,
                    "--workspace", tmp,
                    "--approvals", approvals_path,
                ]
            )
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(completed.stdout, "")  # anvil never ran
            self.assertIn("session", completed.stderr)
            self.assertNotIn("fails closed", completed.stderr)

    def test_invalid_tong_returns_one_without_exec(self):
        # A discovered but invalid definition stops the launch before docker.
        with tempfile.TemporaryDirectory() as tmp:
            tongs_dir = os.path.join(tmp, "tongs")
            os.makedirs(tongs_dir)
            with open(os.path.join(tongs_dir, "bad.yaml"), "w") as handle:
                handle.write("image: x\n")  # missing lifecycle + interface
            completed = _run_launcher_raw(["--repo-tongs", tongs_dir])
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(completed.stdout, "")  # anvil never ran

    def test_session_tong_refused_without_exec(self):
        # A `session` tong is beyond the shared-only launch path, so it is refused
        # before any docker call -- the anvil never runs.
        with tempfile.TemporaryDirectory() as tmp:
            tongs_dir = os.path.join(tmp, "tongs")
            os.makedirs(tongs_dir)
            with open(os.path.join(tongs_dir, "ship.yaml"), "w") as handle:
                handle.write(
                    "lifecycle: session\nimage: x\ninterface:\n  kind: none\n"
                    "readiness:\n  mode: none\n"
                )
            completed = _run_launcher_raw(["--repo-tongs", tongs_dir])
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(completed.stdout, "")
            self.assertIn("session", completed.stderr)

    def test_secret_tong_refused_without_exec(self):
        # A shared tong that references a secret cannot be delivered here, so it
        # is refused before docker.
        with tempfile.TemporaryDirectory() as tmp:
            tongs_dir = os.path.join(tmp, "tongs")
            os.makedirs(tongs_dir)
            with open(os.path.join(tongs_dir, "creds.yaml"), "w") as handle:
                handle.write(
                    "lifecycle: shared\nimage: x\n"
                    "env:\n  TOKEN: ${secret:op:op://Work/t}\n"
                    "interface:\n  kind: none\nreadiness:\n  mode: none\n"
                )
            completed = _run_launcher_raw(["--repo-tongs", tongs_dir])
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(completed.stdout, "")
            self.assertIn("secret", completed.stderr)

    def test_keyboard_interrupt_during_run_returns_130(self):
        # Ctrl-C while the anvil runs leaves the (long-lived) shared tongs up and
        # reports the conventional 128+SIGINT status rather than a traceback.
        with tempfile.TemporaryDirectory() as tmp:
            tongs_dir = os.path.join(tmp, "tongs")
            os.makedirs(tongs_dir)
            with open(os.path.join(tongs_dir, "shipper.yaml"), "w") as handle:
                handle.write(
                    "lifecycle: shared\nimage: x\ninterface:\n  kind: none\n"
                    "readiness:\n  mode: none\n"
                )
            with mock.patch.object(run_anvil, "run_with_tongs", side_effect=KeyboardInterrupt):
                rc = run_anvil.main(["--repo-tongs", tongs_dir, "--", "/no/such/binary-xyz"])
            self.assertEqual(rc, 130)

    def test_mcp_tong_refused_without_exec(self):
        # An `mcp`-interface tong needs generated MCP config the launcher does not
        # emit here, so it is refused before docker.
        with tempfile.TemporaryDirectory() as tmp:
            tongs_dir = os.path.join(tmp, "tongs")
            os.makedirs(tongs_dir)
            with open(os.path.join(tongs_dir, "gh.yaml"), "w") as handle:
                handle.write(
                    "lifecycle: shared\nimage: x\ninterface:\n"
                    "  kind: mcp\n  name: github\n  port: 8080\n"
                    "readiness:\n  mode: none\n"
                )
            completed = _run_launcher_raw(["--repo-tongs", tongs_dir])
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(completed.stdout, "")
            self.assertIn("mcp", completed.stderr)

    def test_volume_tong_refused_without_exec(self):
        # A `volume` interface (a shared named volume) has no consumer yet, so it
        # is refused before docker.
        with tempfile.TemporaryDirectory() as tmp:
            tongs_dir = os.path.join(tmp, "tongs")
            os.makedirs(tongs_dir)
            with open(os.path.join(tongs_dir, "cache.yaml"), "w") as handle:
                handle.write(
                    "lifecycle: shared\nimage: x\ninterface:\n"
                    "  kind: volume\n  volume: build-cache\n  mountpoint: /cache\n"
                    "readiness:\n  mode: none\n"
                )
            completed = _run_launcher_raw(["--repo-tongs", tongs_dir])
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(completed.stdout, "")
            self.assertIn("volume", completed.stderr)

    def test_shared_workspace_mount_refused_without_exec(self):
        # A `shared` tong is reused across sessions, so mounting the workspace
        # into it would leak one session's workspace into the next -- refused.
        with tempfile.TemporaryDirectory() as tmp:
            tongs_dir = os.path.join(tmp, "tongs")
            os.makedirs(tongs_dir)
            with open(os.path.join(tongs_dir, "watch.yaml"), "w") as handle:
                handle.write(
                    "lifecycle: shared\nimage: x\nmounts:\n  - workspace:ro\n"
                    "interface:\n  kind: none\nreadiness:\n  mode: none\n"
                )
            completed = _run_launcher_raw(["--repo-tongs", tongs_dir])
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(completed.stdout, "")
            self.assertIn("workspace", completed.stderr)


class UnsupportedTongReasonsTests(unittest.TestCase):
    """The single chokepoint that refuses tongs the launcher cannot start yet."""

    def _reasons(self, defn):
        return run_anvil.unsupported_tong_reasons(_merged("t", defn, source=tongs.REPO))

    def test_startable_port_tong_has_no_reasons(self):
        self.assertEqual(
            self._reasons({
                "lifecycle": "shared", "image": "x",
                "interface": {"kind": "port", "port": 5432}, "readiness": {"mode": "none"},
            }),
            [],
        )

    def test_startable_none_tong_has_no_reasons(self):
        self.assertEqual(self._reasons(SHARED_NONE), [])

    def test_session_secret_mcp_volume_each_refused(self):
        self.assertTrue(self._reasons(
            {"lifecycle": "session", "image": "x", "interface": {"kind": "none"},
             "readiness": {"mode": "none"}}))
        self.assertTrue(self._reasons(
            {"lifecycle": "shared", "image": "x", "env": {"T": "${secret:op:r}"},
             "interface": {"kind": "none"}, "readiness": {"mode": "none"}}))
        self.assertTrue(self._reasons(
            {"lifecycle": "shared", "image": "x",
             "interface": {"kind": "mcp", "name": "g", "port": 8080},
             "readiness": {"mode": "none"}}))
        self.assertTrue(self._reasons(
            {"lifecycle": "shared", "image": "x",
             "interface": {"kind": "volume", "volume": "v", "mountpoint": "/m"},
             "readiness": {"mode": "none"}}))

    def test_shared_workspace_mount_refused_but_docker_socket_allowed(self):
        # A shared tong that mounts the workspace leaks it across sessions, so it
        # is refused; the docker-socket mount (the broker pattern) is not.
        self.assertTrue(any(
            "workspace" in r for r in self._reasons({
                "lifecycle": "shared", "image": "x", "mounts": ["workspace:ro"],
                "interface": {"kind": "none"}, "readiness": {"mode": "none"},
            })
        ))
        self.assertEqual(
            self._reasons({
                "lifecycle": "shared", "image": "x", "mounts": ["docker-socket"],
                "interface": {"kind": "none"}, "readiness": {"mode": "none"},
            }),
            [],
        )

    def test_workspace_refusal_is_shared_scoped(self):
        # The workspace-mount leak is a `shared`-reuse hazard, so a non-shared tong
        # that mounts the workspace must NOT be refused for the workspace -- only a
        # `shared` one. (A session+workspace watcher is legitimate; it lands when
        # session tongs turn on.) A session tong here is refused for being session,
        # not for the workspace mount.
        session_reasons = self._reasons({
            "lifecycle": "session", "image": "x", "mounts": ["workspace:ro"],
            "interface": {"kind": "none"}, "readiness": {"mode": "none"},
        })
        self.assertTrue(any("session" in r for r in session_reasons))
        self.assertFalse(any("workspace" in r for r in session_reasons))


class FakeDocker:
    """In-process stand-in for DockerCLI that records calls and returns canned
    results, so orchestration is tested without a docker daemon."""

    def __init__(self, states=None, ready=True, anvil_rc=0):
        self.calls = []
        self._states = states or {}      # container -> inspect_state dict
        self._ready = ready
        self._anvil_rc = anvil_rc
        self.run_argvs = []              # detached `docker run` argvs
        self.anvil_argv = None           # set when the anvil runs via run_foreground

    def rm_force(self, container):
        self.calls.append(("rm_force", container))

    def run_detached(self, argv):
        self.run_argvs.append(argv)

    def inspect_state(self, container):
        return self._states.get(container)

    def health_status(self, container):
        return "healthy" if self._ready else "starting"

    def exec_ok(self, container, command):
        return self._ready

    def tcp_probe(self, network, host, port, image):
        self.calls.append(("tcp_probe", network, host, port, image))
        return self._ready

    def run_foreground(self, argv):
        self.anvil_argv = argv
        self.calls.append(("run_foreground", argv))
        return self._anvil_rc


# Tiny launcher options for driving run_with_tongs directly.
def _opts(workspace=None, anvil_image="anvil:img"):
    return run_anvil.LauncherOptions(
        layer_dirs=[], workspace=workspace, approvals=None,
        anvil_image=anvil_image, no_prompt=False,
    )


# A counter clock so readiness loops never sleep on the wall clock in tests.
class _Clock:
    def __init__(self, step=1.0):
        self.t = 0.0
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t


SHARED_OLLAMA = {
    "lifecycle": "shared",
    "image": "ollama/ollama",
    "interface": {"kind": "port", "port": 11434},
    "readiness": {"mode": "tcp"},
}

# A background side-effect tong with no anvil-facing surface and no probe.
SHARED_NONE = {
    "lifecycle": "shared",
    "image": "log-shipper",
    "interface": {"kind": "none"},
    "readiness": {"mode": "none"},
}


class _RecordingRun:
    """A subprocess.run stand-in that records argvs and returns canned codes.

    `codes` maps the first three argv tokens to a return code (default 0), so a
    test can make one docker subcommand "fail" while the rest succeed.
    """

    def __init__(self, codes=None):
        self.argvs = []
        self._codes = codes or {}

    def __call__(self, argv, **kwargs):
        self.argvs.append(list(argv))
        return subprocess.CompletedProcess(argv, self._codes.get(tuple(argv[:3]), 0))


class DockerCLITests(unittest.TestCase):
    """The network seam used by the session-network launch path."""

    def test_ensure_network_creates_when_absent(self):
        rec = _RecordingRun({("docker", "network", "inspect"): 1})
        run_anvil.DockerCLI(run=rec).ensure_network("sess-net")
        self.assertEqual(rec.argvs[0][:4], ["docker", "network", "inspect", "sess-net"])
        self.assertIn(["docker", "network", "create", "sess-net"], rec.argvs)

    def test_ensure_network_reuses_existing(self):
        rec = _RecordingRun()  # inspect returns 0 => already present
        run_anvil.DockerCLI(run=rec).ensure_network("sess-net")
        self.assertNotIn(["docker", "network", "create", "sess-net"], rec.argvs)

    def test_ensure_network_raises_when_create_fails(self):
        rec = _RecordingRun(
            {("docker", "network", "inspect"): 1, ("docker", "network", "create"): 1}
        )
        with self.assertRaises(run_anvil.DockerError):
            run_anvil.DockerCLI(run=rec).ensure_network("sess-net")

    def test_network_connect_passes_alias(self):
        rec = _RecordingRun()
        run_anvil.DockerCLI(run=rec).network_connect("net", "ctr", alias="gh")
        self.assertEqual(
            rec.argvs[-1], ["docker", "network", "connect", "--alias", "gh", "net", "ctr"]
        )

    def test_network_connect_without_alias(self):
        rec = _RecordingRun()
        run_anvil.DockerCLI(run=rec).network_connect("net", "ctr")
        self.assertEqual(rec.argvs[-1], ["docker", "network", "connect", "net", "ctr"])

    def test_network_connect_raises_on_failure(self):
        rec = _RecordingRun({("docker", "network", "connect"): 1})
        with self.assertRaises(run_anvil.DockerError):
            run_anvil.DockerCLI(run=rec).network_connect("net", "ctr")

    def test_network_disconnect_and_rm_are_best_effort(self):
        # Teardown must not raise even when the network or endpoint is already gone.
        rec = _RecordingRun(
            {("docker", "network", "disconnect"): 1, ("docker", "network", "rm"): 1}
        )
        cli = run_anvil.DockerCLI(run=rec)
        cli.network_disconnect("net", "ctr")
        cli.network_rm("net")
        self.assertIn(["docker", "network", "disconnect", "net", "ctr"], rec.argvs)
        self.assertIn(["docker", "network", "rm", "net"], rec.argvs)

    def test_run_foreground_multi_creates_connects_then_starts(self):
        rec = _RecordingRun()
        cli = run_anvil.DockerCLI(run=rec)
        argv = ["docker", "run", "-it", "--name", "anvil", "--network", "sess", "img"]
        with mock.patch.object(run_anvil.subprocess, "Popen") as popen:
            popen.return_value.wait.return_value = 7
            rc = cli.run_foreground_multi(argv, ["base-net"], "anvil")
        self.assertEqual(rc, 7)
        # Created on its primary (session) network...
        self.assertEqual(rec.argvs[0][:2], ["docker", "create"])
        self.assertEqual(rec.argvs[0][rec.argvs[0].index("--network") + 1], "sess")
        # ...connected to the extra network, then started attached.
        self.assertIn(["docker", "network", "connect", "base-net", "anvil"], rec.argvs)
        popen.assert_called_once_with(
            ["docker", "start", "--attach", "--interactive", "anvil"]
        )


class RunWithTongsTests(unittest.TestCase):
    def _run(self, docker, merged, anvil=None, workspace=None):
        return run_anvil.run_with_tongs(
            merged, anvil or ANVIL_ARGV, _opts(workspace=workspace),
            docker=docker, sleep=lambda _s: None, monotonic=_Clock(),
        )

    def test_shared_tong_starts_when_absent_and_runs_anvil(self):
        # ollama-shape shared tong on the anvil's base network: it is started
        # there under its canonical alias, then the anvil runs on that network.
        docker = FakeDocker()
        rc = self._run(docker, _merged("ollama", SHARED_OLLAMA, source=tongs.REPO))
        self.assertEqual(rc, 0)
        self.assertEqual(len(docker.run_argvs), 1)
        started = docker.run_argvs[0]
        self.assertIn("swarmforge-shared-ollama", started)
        self.assertEqual(started[started.index("--network") + 1], "opencode-net")
        self.assertIn("ollama", started)  # network-alias
        # The anvil ran on the unchanged base network.
        self.assertEqual(
            docker.anvil_argv[docker.anvil_argv.index("--network") + 1], "opencode-net"
        )

    def test_shared_tong_reused_when_running_and_hash_matches(self):
        defn = SHARED_OLLAMA
        states = {"swarmforge-shared-ollama": {"running": True, "label": tongs.config_hash(defn)}}
        docker = FakeDocker(states=states)
        self._run(docker, _merged("ollama", defn, source=tongs.REPO))
        self.assertEqual(docker.run_argvs, [])  # reused, not restarted

    def test_shared_tong_recreated_when_hash_differs(self):
        states = {"swarmforge-shared-ollama": {"running": True, "label": "stale"}}
        docker = FakeDocker(states=states)
        self._run(docker, _merged("ollama", SHARED_OLLAMA, source=tongs.REPO))
        self.assertIn(("rm_force", "swarmforge-shared-ollama"), docker.calls)
        self.assertEqual(len(docker.run_argvs), 1)

    def test_shared_tong_recreated_when_absent(self):
        # No running container of that name => start fresh (rm_force clears any
        # stopped leftover first).
        docker = FakeDocker()
        self._run(docker, _merged("ollama", SHARED_OLLAMA, source=tongs.REPO))
        self.assertIn(("rm_force", "swarmforge-shared-ollama"), docker.calls)
        self.assertEqual(len(docker.run_argvs), 1)

    def test_stopped_shared_tong_is_recreated(self):
        # A container exists by name but is not running (a stale leftover) =>
        # recreate even though its label happens to match.
        states = {"swarmforge-shared-ollama":
                  {"running": False, "label": tongs.config_hash(SHARED_OLLAMA)}}
        docker = FakeDocker(states=states)
        self._run(docker, _merged("ollama", SHARED_OLLAMA, source=tongs.REPO))
        self.assertIn(("rm_force", "swarmforge-shared-ollama"), docker.calls)
        self.assertEqual(len(docker.run_argvs), 1)

    def test_multiple_shared_tongs_started_and_injected(self):
        # Two shared tongs in one launch: both are started and both contribute
        # their reachability to the anvil.
        pg = {
            "lifecycle": "shared", "image": "pg",
            "interface": {"kind": "port", "port": 5432}, "readiness": {"mode": "none"},
        }
        redis = {
            "lifecycle": "shared", "image": "redis",
            "interface": {"kind": "port", "port": 6379}, "readiness": {"mode": "none"},
        }
        docker = FakeDocker()
        merged = {
            "pg": {"source": tongs.REPO, "definition": pg},
            "redis": {"source": tongs.REPO, "definition": redis},
        }
        self._run(docker, merged)
        self.assertEqual(len(docker.run_argvs), 2)
        argv = docker.anvil_argv
        self.assertIn("SWARMFORGE_TONG_PG_HOST=pg", argv)
        self.assertIn("SWARMFORGE_TONG_PG_PORT=5432", argv)
        self.assertIn("SWARMFORGE_TONG_REDIS_HOST=redis", argv)
        self.assertIn("SWARMFORGE_TONG_REDIS_PORT=6379", argv)

    def test_tcp_readiness_probes_alias_with_anvil_image(self):
        docker = FakeDocker()
        self._run(docker, _merged("ollama", SHARED_OLLAMA, source=tongs.REPO))
        self.assertIn(("tcp_probe", "opencode-net", "ollama", 11434, "anvil:img"), docker.calls)

    def test_port_tong_injects_host_and_port_env_into_anvil(self):
        defn = {
            "lifecycle": "shared", "image": "pg",
            "interface": {"kind": "port", "port": 5432}, "readiness": {"mode": "none"},
        }
        docker = FakeDocker()
        self._run(docker, _merged("pg", defn, source=tongs.REPO))
        argv = docker.anvil_argv
        self.assertIn("SWARMFORGE_TONG_PG_HOST=pg", argv)
        self.assertIn("SWARMFORGE_TONG_PG_PORT=5432", argv)

    def test_none_tong_leaves_anvil_argv_unchanged(self):
        # A `none` shared tong has no anvil-facing surface, so nothing is injected
        # and the anvil command is exactly what the macro built.
        docker = FakeDocker()
        self._run(docker, _merged("shipper", SHARED_NONE, source=tongs.REPO))
        self.assertEqual(docker.anvil_argv, ANVIL_ARGV)

    def test_unready_tong_raises_and_anvil_never_runs(self):
        docker = FakeDocker(ready=False)
        defn = {
            "lifecycle": "shared", "image": "pg",
            "interface": {"kind": "port", "port": 5432},
            "readiness": {"mode": "tcp", "timeout": "1s"},
        }
        with self.assertRaises(run_anvil.OrchestrationError):
            self._run(docker, _merged("pg", defn, source=tongs.REPO))
        self.assertIsNone(docker.anvil_argv)  # anvil never ran

    def test_anvil_exit_code_is_returned(self):
        docker = FakeDocker(anvil_rc=42)
        rc = self._run(docker, _merged("ollama", SHARED_OLLAMA, source=tongs.REPO))
        self.assertEqual(rc, 42)

    def test_no_anvil_image_degrades_tcp_to_running_check(self):
        # Without an anvil image a TCP probe cannot dial the tong's port, so it
        # falls back to "is the container running" using inspect_state.
        states = {"swarmforge-shared-ollama": {"running": True, "label": tongs.config_hash(SHARED_OLLAMA)}}
        docker = FakeDocker(states=states)
        rc = run_anvil.run_with_tongs(
            _merged("ollama", SHARED_OLLAMA, source=tongs.REPO), ANVIL_ARGV,
            _opts(anvil_image=None), docker=docker,
            sleep=lambda _s: None, monotonic=_Clock(),
        )
        self.assertEqual(rc, 0)
        self.assertNotIn("tcp_probe", [c[0] for c in docker.calls])


if __name__ == "__main__":
    unittest.main(verbosity=2)
