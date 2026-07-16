#!/usr/bin/env python3
"""Unit tests for scripts/run_anvil.py. Run: python3 scripts/test_run_anvil.py"""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
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
        self.assertIsNone(opts.providers)
        self.assertIsNone(opts.anvil_image)
        self.assertFalse(opts.no_prompt)

    def test_parses_workspace_approvals_and_no_prompt(self):
        opts, cmd = run_anvil.parse_args(
            ["--workspace", "/ws", "--approvals", "/a.json",
             "--providers", "/p.yaml", "--anvil-image", "anvil:img",
             "--no-prompt", "--", "x"]
        )
        self.assertEqual(opts.workspace, "/ws")
        self.assertEqual(opts.approvals, "/a.json")
        self.assertEqual(opts.providers, "/p.yaml")
        self.assertEqual(opts.anvil_image, "anvil:img")
        self.assertTrue(opts.no_prompt)
        self.assertEqual(cmd, ["x"])

    def test_parses_harness(self):
        opts, _ = run_anvil.parse_args(["--harness", "claude", "--", "x"])
        self.assertEqual(opts.harness, "claude")

    def test_harness_defaults_to_none(self):
        opts, _ = run_anvil.parse_args(["--", "x"])
        self.assertIsNone(opts.harness)

    def test_harness_without_value_raises(self):
        with self.assertRaises(run_anvil.UsageError):
            run_anvil.parse_args(["--harness"])

    def test_anvil_image_without_value_raises(self):
        with self.assertRaises(run_anvil.UsageError):
            run_anvil.parse_args(["--anvil-image"])

    def test_workspace_without_value_raises(self):
        with self.assertRaises(run_anvil.UsageError):
            run_anvil.parse_args(["--workspace"])

    def test_approvals_without_value_raises(self):
        with self.assertRaises(run_anvil.UsageError):
            run_anvil.parse_args(["--approvals"])

    def test_providers_without_value_raises(self):
        with self.assertRaises(run_anvil.UsageError):
            run_anvil.parse_args(["--providers"])

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
        # The Makefile always passes --anvil-image and --providers; with no tongs
        # they are consumed by the launcher and the anvil argv is forwarded
        # unchanged (the secret-provider table is never even read).
        forwarded, stderr = _run_launcher([
            "--anvil-image", "opencode:local",
            "--providers", "/nonexistent/secret-providers.yaml",
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


class DefaultProvidersPathTests(unittest.TestCase):
    def setUp(self):
        self.saved = os.environ.get("SWARMFORGE_USER_ASSETS_DIR")

    def tearDown(self):
        if self.saved is None:
            os.environ.pop("SWARMFORGE_USER_ASSETS_DIR", None)
        else:
            os.environ["SWARMFORGE_USER_ASSETS_DIR"] = self.saved

    def test_honors_user_assets_dir(self):
        os.environ["SWARMFORGE_USER_ASSETS_DIR"] = "/opt/sf"
        self.assertEqual(
            run_anvil.default_providers_path(), "/opt/sf/secret-providers.yaml"
        )

    def test_falls_back_to_home_swarmforge(self):
        os.environ.pop("SWARMFORGE_USER_ASSETS_DIR", None)
        expected = os.path.join(
            os.path.expanduser("~"), ".swarmforge", "secret-providers.yaml"
        )
        self.assertEqual(run_anvil.default_providers_path(), expected)


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

    def test_override_resolves_matching_ref(self):
        resolve = run_anvil.make_secret_resolver(
            {"shared": {"default": None, "overrides": {"tok": self._writes("sys.argv[1]")}}}
        )
        self.assertEqual(resolve("shared", "tok"), "tok")

    def test_unmapped_ref_without_default_raises(self):
        # A structured provider that names neither the ref nor `default` stops the
        # launch with a clear message rather than shelling out to a wrong command.
        resolve = run_anvil.make_secret_resolver(
            {"shared": {"default": None, "overrides": {"tok": ["op", "read", "{ref}"]}}}
        )
        with self.assertRaises(run_anvil.SecretResolutionError) as ctx:
            resolve("shared", "other")
        self.assertIn("shared", str(ctx.exception))
        self.assertIn("other", str(ctx.exception))

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
        # The resolver is the impure half of tongs.plan_tong_secrets: a secret env
        # var resolves to a value under `secrets`, never the plain `-e` env.
        resolve = run_anvil.make_secret_resolver({"echo": self._writes("sys.argv[1]")})
        plan = tongs.plan_tong_secrets(
            {"REGION": "us", "TOKEN": "${secret:echo:s3cr3t}"}, resolve
        )
        self.assertEqual(plan["env"], {"REGION": "us"})
        self.assertEqual(plan["secrets"], {"TOKEN": "s3cr3t"})


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
        # workspace tong clears the approval prompt but, having a `volume`
        # interface this launcher cannot wire up yet, is then refused as
        # unsupported -- proving the gate passed without the anvil ever running.
        with tempfile.TemporaryDirectory() as tmp:
            tongs_dir = os.path.join(tmp, "tongs")
            os.makedirs(tongs_dir)
            with open(os.path.join(tongs_dir, "cache.yaml"), "w") as handle:
                handle.write(
                    "lifecycle: shared\nimage: x\ninterface:\n"
                    "  kind: volume\n  volume: build-cache\n  mountpoint: /cache\n"
                    "readiness:\n  mode: none\n"
                )
            defn = tongs.load_tong_file(os.path.join(tongs_dir, "cache.yaml"))
            approvals_path = os.path.join(tmp, "approvals.json")
            tongs.save_approvals(
                approvals_path, tongs.record_approval({}, tmp, "cache", defn)
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
            self.assertIn("volume", completed.stderr)
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

    def test_malformed_providers_file_returns_one_without_exec(self):
        # The secret-provider table is loaded before any tong starts, so a
        # malformed file stops the launch (a clear error, anvil never runs) rather
        # than dropping a provider and failing mid-resolution.
        with tempfile.TemporaryDirectory() as tmp:
            tongs_dir = os.path.join(tmp, "tongs")
            os.makedirs(tongs_dir)
            with open(os.path.join(tongs_dir, "shipper.yaml"), "w") as handle:
                handle.write(
                    "lifecycle: shared\nimage: x\ninterface:\n  kind: none\n"
                    "readiness:\n  mode: none\n"
                )
            providers = os.path.join(tmp, "secret-providers.yaml")
            with open(providers, "w") as handle:
                handle.write("providers:\n  op: not-a-list\n")
            completed = _run_launcher_raw(
                ["--repo-tongs", tongs_dir, "--providers", providers]
            )
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(completed.stdout, "")  # anvil never ran
            self.assertIn("op", completed.stderr)

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

    def test_colliding_mcp_aliases_refused_without_exec(self):
        # Two `mcp` tongs that resolve to the same canonical alias (their shared
        # interface.name) would make DNS nondeterministic, so the set is refused
        # before docker -- the anvil never runs.
        with tempfile.TemporaryDirectory() as tmp:
            tongs_dir = os.path.join(tmp, "tongs")
            os.makedirs(tongs_dir)
            for filename in ("gh.yaml", "gh2.yaml"):
                with open(os.path.join(tongs_dir, filename), "w") as handle:
                    handle.write(
                        "lifecycle: shared\nimage: x\ninterface:\n"
                        "  kind: mcp\n  name: github\n  port: 8080\n"
                        "readiness:\n  mode: none\n"
                    )
            completed = _run_launcher_raw(["--repo-tongs", tongs_dir])
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(completed.stdout, "")
            self.assertIn("github", completed.stderr)  # the colliding alias

    def test_mcp_tong_without_supported_harness_refused_without_exec(self):
        # MCP tongs need a harness-specific config emitter. A direct launcher use
        # without --harness, or a typo, must stop before starting the tong.
        for harness_args in ([], ["--harness", "opencdoe"]):
            with self.subTest(harness_args=harness_args):
                with tempfile.TemporaryDirectory() as tmp:
                    tongs_dir = os.path.join(tmp, "tongs")
                    os.makedirs(tongs_dir)
                    with open(os.path.join(tongs_dir, "gh.yaml"), "w") as handle:
                        handle.write(
                            "lifecycle: shared\nimage: x\ninterface:\n"
                            "  kind: mcp\n  name: github\n  port: 8080\n"
                            "readiness:\n  mode: none\n"
                        )
                    completed = _run_launcher_raw(
                        harness_args + ["--repo-tongs", tongs_dir]
                    )
                    self.assertEqual(completed.returncode, 1)
                    self.assertEqual(completed.stdout, "")
                    self.assertIn("--harness", completed.stderr)

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

    def test_startable_session_tong_has_no_reasons(self):
        # A `session` tong reached over the network (or with no surface) is now
        # startable -- it runs on a per-session network.
        self.assertEqual(
            self._reasons({
                "lifecycle": "session", "image": "x",
                "interface": {"kind": "port", "port": 5432}, "readiness": {"mode": "none"},
            }),
            [],
        )

    def test_volume_refused(self):
        # A `volume` interface (a shared named volume) has no anvil-side consumer
        # yet, so it remains refused.
        self.assertTrue(self._reasons(
            {"lifecycle": "shared", "image": "x",
             "interface": {"kind": "volume", "volume": "v", "mountpoint": "/m"},
             "readiness": {"mode": "none"}}))

    def test_mcp_tong_is_now_startable(self):
        # An `mcp` tong is reached via generated MCP config, so it is no longer
        # refused -- on either lifecycle.
        self.assertEqual(self._reasons(
            {"lifecycle": "shared", "image": "x",
             "interface": {"kind": "mcp", "name": "g", "port": 8080},
             "readiness": {"mode": "none"}}), [])
        self.assertEqual(self._reasons(
            {"lifecycle": "session", "image": "x",
             "interface": {"kind": "mcp", "name": "g", "port": 8080},
             "readiness": {"mode": "none"}}), [])

    def test_secret_tong_is_now_startable(self):
        # Secrets are resolved and delivered as env over a FIFO, so a tong that references
        # one (and is otherwise reachable over the network or has no surface) is no
        # longer refused -- on either lifecycle.
        self.assertEqual(self._reasons(
            {"lifecycle": "shared", "image": "x", "env": {"T": "${secret:op:r}"},
             "interface": {"kind": "none"}, "readiness": {"mode": "none"}}), [])
        self.assertEqual(self._reasons(
            {"lifecycle": "session", "image": "x", "env": {"T": "${secret:op:r}"},
             "interface": {"kind": "port", "port": 5432}, "readiness": {"mode": "none"}}), [])

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
        # The workspace-mount leak is a `shared`-reuse hazard, so a `session` tong
        # that mounts the workspace is legitimate (it is torn down with the anvil)
        # and must NOT be refused -- only a `shared` one is.
        self.assertEqual(
            self._reasons({
                "lifecycle": "session", "image": "x", "mounts": ["workspace:ro"],
                "interface": {"kind": "none"}, "readiness": {"mode": "none"},
            }),
            [],
        )


class FakeDocker:
    """In-process stand-in for DockerCLI that records calls and returns canned
    results, so orchestration is tested without a docker daemon."""

    def __init__(self, states=None, ready=True, anvil_rc=0,
                 image_config=(["app"], [], "")):
        self.calls = []
        self._states = states or {}      # container -> inspect_state dict
        self._ready = ready
        self._anvil_rc = anvil_rc
        self._image_config = image_config
        self.run_argvs = []              # detached `docker run` argvs
        self.inspected_images = []       # images whose exec config was read
        self.anvil_argv = None           # set when the anvil runs
        self.anvil_extra_networks = None  # extra networks the anvil joined

    def rm_force(self, container):
        self.calls.append(("rm_force", container))

    def run_detached(self, argv):
        self.run_argvs.append(argv)
        container = argv[argv.index("--name") + 1] if "--name" in argv else None
        self.calls.append(("run_detached", container))

    def image_exec_config(self, image):
        self.inspected_images.append(image)
        return self._image_config

    def ensure_network(self, name):
        self.calls.append(("ensure_network", name))

    def network_connect(self, network, container, alias=None):
        self.calls.append(("network_connect", network, container, alias))

    def network_disconnect(self, network, container):
        self.calls.append(("network_disconnect", network, container))

    def network_rm(self, network):
        self.calls.append(("network_rm", network))

    def run_foreground_multi(self, argv, extra_networks, container):
        self.anvil_argv = argv
        self.anvil_extra_networks = list(extra_networks)
        self.calls.append(("run_foreground_multi", argv, tuple(extra_networks), container))
        return self._anvil_rc

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


class FakeChannels:
    """A `make_channel` stand-in that records secret deliveries without a FIFO.

    Records the uid each channel is opened for, every payload delivered, and how
    many channels were cleaned up, so a test can assert what reached the tong (and
    that the secret never went through `-e`/argv) without touching the filesystem.
    `deliver_error`, if set, is raised from `deliver` to exercise the failure path.
    """

    def __init__(self, deliver_error=None):
        self.uids = []
        self.payloads = []
        self.cleanups = 0
        self._deliver_error = deliver_error

    def __call__(self, uid=None):
        self.uids.append(uid)
        return FakeChannels._Channel(self)

    class _Channel:
        host_path = "/fake/swarmforge-secret/secret-env"

        def __init__(self, owner):
            self._owner = owner

        def deliver(self, payload, **kwargs):
            self._owner.payloads.append(payload)
            if self._owner._deliver_error is not None:
                raise self._owner._deliver_error

        def cleanup(self):
            self._owner.cleanups += 1


# Tiny launcher options for driving run_with_tongs directly.
def _opts(workspace=None, anvil_image="anvil:img", harness="opencode"):
    return run_anvil.LauncherOptions(
        layer_dirs=[], workspace=workspace, approvals=None, providers=None,
        harness=harness, anvil_image=anvil_image, no_prompt=False,
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

# A credential-holding MCP tong: an HTTP MCP server the anvil reaches at its
# canonical alias (interface.name) on the session/base network.
SHARED_MCP = {
    "lifecycle": "shared",
    "image": "github-tong",
    "interface": {"kind": "mcp", "name": "github", "port": 8080},
    "readiness": {"mode": "none"},
}

# An org-owned credential-holding MCP tong: the user's reported case. Two orgs
# ship this same file with different credentials; each must run partitioned.
ORG_ASANA = {
    "lifecycle": "shared",
    "image": "asana-mcp:latest",
    "interface": {"kind": "mcp", "name": "asana-mcp", "port": 3000},
    "readiness": {"mode": "none"},
}

# A per-session network service (a throwaway fixture DB) reached by host+port.
SESSION_PORT = {
    "lifecycle": "session",
    "image": "fixture-pg",
    "interface": {"kind": "port", "port": 5432},
    "readiness": {"mode": "none"},
}

# A secret provider built on the test interpreter (so the suite needs no op/pass
# installed): it echoes the {ref} it is handed, so ${secret:echo:VALUE} resolves
# to "VALUE".
ECHO_PROVIDERS = {
    "echo": [sys.executable, "-c", "import sys; sys.stdout.write(sys.argv[1])", "{ref}"]
}

# A credential-holding shared tong: its token is a secret reference, delivered to
# the running container as env over a FIFO rather than passed as a docker env var.
SHARED_SECRET = {
    "lifecycle": "shared",
    "image": "github-tong",
    "env": {"GITHUB_TOKEN": "${secret:echo:s3cr3t}"},
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

    @staticmethod
    def _image_run(entrypoint_json, cmd_json, user_json, inspect_codes=(0,)):
        """A run() that answers `docker image inspect` with canned JSON.

        `inspect_codes` is the return code for each successive inspect call (so a
        test can fail the first and succeed after a pull); other commands return 0.
        """
        state = {"calls": 0}

        def run(argv, **kwargs):
            if argv[:3] == ["docker", "image", "inspect"]:
                idx = min(state["calls"], len(inspect_codes) - 1)
                code = inspect_codes[idx]
                state["calls"] += 1
                out = ("%s\n%s\n%s" % (entrypoint_json, cmd_json, user_json)).encode()
                return subprocess.CompletedProcess(argv, code, stdout=out)
            return subprocess.CompletedProcess(argv, 0)

        return run

    def test_image_exec_config_parses_entrypoint_cmd_user(self):
        cli = run_anvil.DockerCLI(run=self._image_run('["node"]', '["server.js"]', '"1000"'))
        self.assertEqual(cli.image_exec_config("img"), (["node"], ["server.js"], "1000"))

    def test_image_exec_config_treats_null_as_empty(self):
        cli = run_anvil.DockerCLI(run=self._image_run("null", "null", "null"))
        self.assertEqual(cli.image_exec_config("img"), ([], [], ""))

    def test_image_exec_config_pulls_when_absent_then_succeeds(self):
        rec_run = self._image_run('["app"]', "null", '""', inspect_codes=(1, 0))
        cli = run_anvil.DockerCLI(run=rec_run)
        self.assertEqual(cli.image_exec_config("img"), (["app"], [], ""))

    def test_image_exec_config_raises_when_still_missing(self):
        cli = run_anvil.DockerCLI(run=self._image_run("null", "null", "null",
                                                      inspect_codes=(1, 1)))
        with self.assertRaises(run_anvil.DockerError):
            cli.image_exec_config("img")


class UidOfTests(unittest.TestCase):
    def test_bare_uid_parses(self):
        self.assertEqual(run_anvil._uid_of("1000"), 1000)
        self.assertEqual(run_anvil._uid_of("1000:1000"), 1000)

    def test_name_or_empty_is_none(self):
        self.assertIsNone(run_anvil._uid_of("appuser"))
        self.assertIsNone(run_anvil._uid_of(""))
        self.assertIsNone(run_anvil._uid_of(None))


class SecretChannelTests(unittest.TestCase):
    def test_times_out_when_no_reader_opens(self):
        # No reader ever opens the FIFO, so the non-blocking write open keeps
        # getting ENXIO; once the (fake) clock passes the deadline it fails closed
        # rather than hanging the launcher.
        channel = run_anvil.open_secret_channel()
        try:
            clock = iter([0.0, 1.0, 2.0, 99.0])
            with self.assertRaises(run_anvil.OrchestrationError):
                channel.deliver(
                    "export X='y'\n", timeout=5.0, poll=0.0,
                    sleep=lambda _s: None, monotonic=lambda: next(clock),
                )
        finally:
            channel.cleanup()

    def test_delivers_payload_to_a_reader(self):
        # With a reader attached, the payload is written and the reader sees it
        # followed by EOF -- the real FIFO round-trip, no docker involved.
        channel = run_anvil.open_secret_channel()
        received = []

        def reader():
            with open(channel.host_path, "r") as handle:
                received.append(handle.read())

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            channel.deliver("export TOKEN='s3cr3t'\n")
        finally:
            thread.join(timeout=5)
            channel.cleanup()
        self.assertEqual(received, ["export TOKEN='s3cr3t'\n"])

    def test_delivers_payload_larger_than_pipe_buffer(self):
        # A payload bigger than the pipe capacity (~64 KiB) forces several writes
        # and a full buffer; every byte must still arrive (no silent truncation).
        channel = run_anvil.open_secret_channel()
        payload = "export BIG='" + ("x" * 200000) + "'\n"
        received = []

        def reader():
            with open(channel.host_path, "r") as handle:
                received.append(handle.read())

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            channel.deliver(payload)
        finally:
            thread.join(timeout=10)
            channel.cleanup()
        self.assertEqual(received, [payload])


class McpInjectionTests(unittest.TestCase):
    """_mcp_injection writes the generated config and shapes the anvil args."""

    FRAGMENT = {"mcp": {"github": {"type": "remote", "url": "http://github:8080/mcp"}}}

    def test_empty_fragment_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            pre, post = run_anvil._mcp_injection({}, "opencode", tmp)
            self.assertEqual((pre, post), ([], []))
            self.assertEqual(os.listdir(tmp), [])  # no file written

    def test_opencode_mounts_and_sets_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            pre, post = run_anvil._mcp_injection(self.FRAGMENT, "opencode", tmp)
            host_path = os.path.join(tmp, "tong-mcp.json")
            self.assertEqual(post, [])  # OpenCode reads it via the entrypoint
            self.assertEqual(
                pre,
                ["-v", "%s:%s:ro" % (host_path, run_anvil.MCP_CONFIG_CONTAINER_PATH),
                 "-e", "%s=%s" % (run_anvil.MCP_FILE_ENV, run_anvil.MCP_CONFIG_CONTAINER_PATH)],
            )
            with open(host_path, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), self.FRAGMENT)

    def test_claude_mounts_and_appends_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            pre, post = run_anvil._mcp_injection(self.FRAGMENT, "claude", tmp)
            host_path = os.path.join(tmp, "tong-mcp.json")
            self.assertEqual(
                pre, ["-v", "%s:%s:ro" % (host_path, run_anvil.MCP_CONFIG_CONTAINER_PATH)]
            )
            self.assertEqual(post, ["--mcp-config", run_anvil.MCP_CONFIG_CONTAINER_PATH])
            with open(host_path, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), self.FRAGMENT)


class RunWithTongsTests(unittest.TestCase):
    def _run(self, docker, merged, anvil=None, workspace=None, harness="opencode"):
        return run_anvil.run_with_tongs(
            merged, anvil or ANVIL_ARGV, _opts(workspace=workspace, harness=harness),
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

    def _mcp_mount_host_path(self, argv):
        """Host path of the read-only MCP-config bind mount in an anvil argv."""
        suffix = ":%s:ro" % run_anvil.MCP_CONFIG_CONTAINER_PATH
        for index, token in enumerate(argv):
            if token == "-v" and argv[index + 1].endswith(suffix):
                return argv[index + 1][: -len(suffix)]
        self.fail("no MCP-config mount found in anvil argv")

    def test_opencode_mcp_tong_mounts_config_and_sets_env(self):
        # An OpenCode session reaches an `mcp` tong via the entrypoint merge: the
        # generated config is bind-mounted read-only and pointed at by the env var.
        docker = FakeDocker()
        self._run(docker, _merged("github-creds", SHARED_MCP, source=tongs.REPO),
                  harness="opencode")
        argv = docker.anvil_argv
        self.assertIn("github", docker.run_argvs[0])  # tong started under its alias
        self.assertIn(
            "%s=%s" % (run_anvil.MCP_FILE_ENV, run_anvil.MCP_CONFIG_CONTAINER_PATH), argv
        )
        self._mcp_mount_host_path(argv)  # the read-only mount is present
        self.assertNotIn("--mcp-config", argv)  # OpenCode does not use the flag

    def test_claude_mcp_tong_mounts_config_and_appends_flag(self):
        # A Claude session reads the generated config directly via --mcp-config,
        # appended after the image so it reaches the harness binary.
        docker = FakeDocker()
        self._run(docker, _merged("github-creds", SHARED_MCP, source=tongs.REPO),
                  harness="claude")
        argv = docker.anvil_argv
        self.assertEqual(argv[-2:], ["--mcp-config", run_anvil.MCP_CONFIG_CONTAINER_PATH])
        self.assertNotIn("%s=%s" % (run_anvil.MCP_FILE_ENV, run_anvil.MCP_CONFIG_CONTAINER_PATH),
                         argv)
        self._mcp_mount_host_path(argv)  # the read-only mount is present

    def test_mcp_tong_with_unknown_harness_raises_before_docker(self):
        for harness in (None, "opencdoe"):
            with self.subTest(harness=harness):
                docker = FakeDocker()
                with self.assertRaisesRegex(run_anvil.OrchestrationError, "--harness"):
                    self._run(docker, _merged("github-creds", SHARED_MCP, source=tongs.REPO),
                              harness=harness)
                self.assertEqual(docker.calls, [])
                self.assertEqual(docker.run_argvs, [])
                self.assertIsNone(docker.anvil_argv)

    def test_mcp_config_tempfile_cleaned_up_after_run(self):
        # The generated config lives in a host temp dir bind-mounted into the
        # anvil; once the anvil exits the temp dir is removed.
        docker = FakeDocker()
        self._run(docker, _merged("github-creds", SHARED_MCP, source=tongs.REPO),
                  harness="opencode")
        host_path = self._mcp_mount_host_path(docker.anvil_argv)
        self.assertFalse(os.path.exists(host_path))

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

    # --- Secret resolution + FIFO env delivery ------------------------------

    def _run_secret(self, docker, merged, providers, channels=None):
        return run_anvil.run_with_tongs(
            merged, ANVIL_ARGV, _opts(), docker=docker, providers=providers,
            make_channel=channels or FakeChannels(),
            sleep=lambda _s: None, monotonic=_Clock(),
        )

    def test_secret_delivered_as_env_via_fifo_never_in_argv(self):
        # A resolved secret is handed to the tong over the FIFO (an `export`
        # script), never as a docker `-e` value; the run argv carries only the
        # entrypoint wrapper and the read-only FIFO bind, not the secret.
        docker = FakeDocker(image_config=(["node"], ["server.js"], ""))
        channels = FakeChannels()
        rc = self._run_secret(
            docker, _merged("gh", SHARED_SECRET, source=tongs.REPO), ECHO_PROVIDERS,
            channels=channels,
        )
        self.assertEqual(rc, 0)
        started = docker.run_argvs[0]
        # Entrypoint is wrapped and the FIFO is bind-mounted read-only.
        self.assertEqual(started[started.index("--entrypoint") + 1], "/bin/sh")
        self.assertIn(
            "/fake/swarmforge-secret/secret-env:/run/swarmforge/secret-env:ro", started
        )
        # The image's real argv is what the wrapper execs (after the image token).
        self.assertEqual(started[started.index("github-tong") + 1:],
                         ["-c", started[started.index("-c") + 1],
                          "swarmforge-tong", "node", "server.js"])
        # The secret is nowhere in the argv -- it only went through the channel.
        self.assertNotIn("s3cr3t", " ".join(started))
        self.assertNotIn("GITHUB_TOKEN=s3cr3t", started)
        self.assertEqual(channels.payloads, ["export GITHUB_TOKEN='s3cr3t'\n"])
        self.assertEqual(channels.cleanups, 1)  # FIFO cleaned up after delivery

    def test_secret_tong_reads_exec_target_from_image(self):
        docker = FakeDocker(image_config=(["entry"], ["arg"], ""))
        self._run_secret(
            docker, _merged("gh", SHARED_SECRET, source=tongs.REPO), ECHO_PROVIDERS
        )
        self.assertEqual(docker.inspected_images, ["github-tong"])

    def test_unresolvable_secret_stops_launch_before_anvil(self):
        # No provider for the referenced scheme => resolution fails before the tong
        # even starts, and the anvil never runs.
        docker = FakeDocker()
        with self.assertRaises(run_anvil.SecretResolutionError):
            self._run_secret(docker, _merged("gh", SHARED_SECRET, source=tongs.REPO), {})
        self.assertEqual(docker.run_argvs, [])  # never reached the start
        self.assertIsNone(docker.anvil_argv)

    def test_delivery_failure_removes_half_configured_container(self):
        # If delivery over the FIFO fails after the container started, the
        # container is removed before raising, so a `shared` tong is not left
        # stamped with its config-hash label (and reused) while missing its secret.
        docker = FakeDocker()
        channels = FakeChannels(deliver_error=run_anvil.DockerError("boom"))
        with self.assertRaises(run_anvil.DockerError):
            self._run_secret(
                docker, _merged("gh", SHARED_SECRET, source=tongs.REPO), ECHO_PROVIDERS,
                channels=channels,
            )
        # rm_force fires twice: clearing any leftover before start, then removing
        # the half-configured container after the failed delivery.
        self.assertEqual(docker.calls.count(("rm_force", "swarmforge-shared-gh")), 2)
        self.assertEqual(channels.cleanups, 1)  # FIFO still cleaned up
        self.assertIsNone(docker.anvil_argv)

    def test_interrupt_during_delivery_removes_half_configured_container(self):
        # Ctrl-C while delivering must still remove the container, or a `shared`
        # tong (stamped with its config-hash label and not tracked for session
        # teardown) would be reused next session with a missing secret.
        docker = FakeDocker()
        channels = FakeChannels(deliver_error=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self._run_secret(
                docker, _merged("gh", SHARED_SECRET, source=tongs.REPO), ECHO_PROVIDERS,
                channels=channels,
            )
        self.assertEqual(docker.calls.count(("rm_force", "swarmforge-shared-gh")), 2)
        self.assertEqual(channels.cleanups, 1)
        self.assertIsNone(docker.anvil_argv)

    def test_reused_shared_tong_never_resolves_or_delivers_secrets(self):
        # A running shared tong whose hash matches is reused untouched -- deciding
        # to reuse must never invoke a secret-provider CLI (which could prompt for
        # an unlock every session) or open a channel.
        states = {"swarmforge-shared-gh":
                  {"running": True, "label": tongs.config_hash(SHARED_SECRET)}}
        docker = FakeDocker(states=states)
        channels = FakeChannels()
        boom = {"echo": [sys.executable, "-c", "import sys; sys.exit(1)"]}
        rc = self._run_secret(
            docker, _merged("gh", SHARED_SECRET, source=tongs.REPO), boom,
            channels=channels,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(docker.run_argvs, [])   # reused, not restarted
        self.assertEqual(channels.payloads, [])  # no resolution, no delivery
        self.assertEqual(docker.inspected_images, [])  # no image inspect either

    def test_session_secret_tong_delivered_over_channel(self):
        defn = {
            "lifecycle": "session", "image": "creds",
            "env": {"TOKEN": "${secret:echo:abc}"},
            "interface": {"kind": "none"}, "readiness": {"mode": "none"},
        }
        docker = FakeDocker()
        channels = FakeChannels()
        rc = self._run_secret(
            docker, _merged("creds", defn, source=tongs.REPO), ECHO_PROVIDERS,
            channels=channels,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(channels.payloads, ["export TOKEN='abc'\n"])

    def test_secret_uid_passed_to_channel_factory(self):
        # The image's numeric user is passed to the channel factory so the FIFO can
        # be chowned to the uid that will read it.
        docker = FakeDocker(image_config=(["app"], [], "1000"))
        channels = FakeChannels()
        self._run_secret(
            docker, _merged("gh", SHARED_SECRET, source=tongs.REPO), ECHO_PROVIDERS,
            channels=channels,
        )
        self.assertEqual(channels.uids, [1000])

    # --- Session lifecycle + per-session networks ---------------------------

    def test_shared_only_keeps_base_network_and_plain_run(self):
        # No `session` tong => no per-session network is created and the anvil runs
        # on the base network through the plain (single-network) foreground path.
        docker = FakeDocker()
        self._run(docker, _merged("ollama", SHARED_OLLAMA, source=tongs.REPO))
        kinds = [c[0] for c in docker.calls]
        self.assertNotIn("ensure_network", kinds)
        self.assertNotIn("network_rm", kinds)
        self.assertNotIn("run_foreground_multi", kinds)
        self.assertIn("run_foreground", kinds)
        self.assertEqual(
            docker.anvil_argv[docker.anvil_argv.index("--network") + 1], "opencode-net"
        )
        self.assertIsNone(docker.anvil_extra_networks)

    def test_session_tong_creates_network_starts_on_it_and_tears_down(self):
        docker = FakeDocker()
        rc = self._run(docker, _merged("pg", SESSION_PORT, source=tongs.REPO))
        self.assertEqual(rc, 0)
        net = tongs.session_network_name("claude-myproject")
        self.assertIn(("ensure_network", net), docker.calls)
        # The session tong is started on the per-session network under its alias.
        self.assertEqual(len(docker.run_argvs), 1)
        started = docker.run_argvs[0]
        self.assertIn("claude-myproject-tong-pg", started)
        self.assertEqual(started[started.index("--network") + 1], net)
        self.assertEqual(started[started.index("--network-alias") + 1], "pg")
        # The anvil joined the session network (primary) and the base network
        # (extra) via the create -> connect -> start path, and got the port env.
        self.assertEqual(docker.anvil_argv[docker.anvil_argv.index("--network") + 1], net)
        self.assertEqual(docker.anvil_extra_networks, ["opencode-net"])
        self.assertIn("SWARMFORGE_TONG_PG_HOST=pg", docker.anvil_argv)
        # Teardown removes the session tong and the anvil, then the network -- the
        # network rm must come after its endpoints are gone or docker refuses it.
        self.assertIn(("rm_force", "claude-myproject-tong-pg"), docker.calls)
        self.assertIn(("rm_force", "claude-myproject"), docker.calls)
        self.assertIn(("network_rm", net), docker.calls)
        self.assertLess(
            docker.calls.index(("rm_force", "claude-myproject")),
            docker.calls.index(("network_rm", net)),
        )
        self.assertLess(
            docker.calls.index(("rm_force", "claude-myproject-tong-pg")),
            docker.calls.index(("network_rm", net)),
        )

    def test_shared_tong_connected_to_session_network_and_left_running(self):
        # A `shared` tong alongside a `session` tong is ensured on the base network,
        # then connected to the per-session network for the anvil to reach; on
        # teardown it is disconnected but never removed.
        docker = FakeDocker()
        merged = {
            "pg": {"source": tongs.REPO, "definition": SESSION_PORT},
            "ollama": {"source": tongs.REPO, "definition": SHARED_OLLAMA},
        }
        self._run(docker, merged)
        net = tongs.session_network_name("claude-myproject")
        self.assertIn(
            ("network_connect", net, "swarmforge-shared-ollama", "ollama"), docker.calls
        )
        self.assertIn(
            ("network_disconnect", net, "swarmforge-shared-ollama"), docker.calls
        )
        # The connect is idempotent against a reused network: a best-effort
        # disconnect precedes it (a no-op when the tong is not already attached).
        self.assertLess(
            docker.calls.index(("network_disconnect", net, "swarmforge-shared-ollama")),
            docker.calls.index(("network_connect", net, "swarmforge-shared-ollama", "ollama")),
        )
        # The shared tong is rm_force'd only once -- when (re)started to clear a
        # leftover -- never as part of teardown, so it is left running.
        self.assertEqual(
            docker.calls.count(("rm_force", "swarmforge-shared-ollama")), 1
        )

    def test_session_tong_readiness_probes_on_session_network(self):
        docker = FakeDocker()
        defn = {
            "lifecycle": "session", "image": "pg",
            "interface": {"kind": "port", "port": 5432}, "readiness": {"mode": "tcp"},
        }
        self._run(docker, _merged("pg", defn, source=tongs.REPO))
        net = tongs.session_network_name("claude-myproject")
        self.assertIn(("tcp_probe", net, "pg", 5432, "anvil:img"), docker.calls)

    def test_session_teardown_runs_on_keyboard_interrupt(self):
        # Ctrl-C mid-session must still tear down the session tong and network so an
        # interrupted run leaks neither.
        docker = FakeDocker()

        def interrupt(argv, extra_networks, container):
            docker.calls.append(("run_foreground_multi", argv, tuple(extra_networks), container))
            raise KeyboardInterrupt

        docker.run_foreground_multi = interrupt
        net = tongs.session_network_name("claude-myproject")
        with self.assertRaises(KeyboardInterrupt):
            self._run(docker, _merged("pg", SESSION_PORT, source=tongs.REPO))
        self.assertIn(("rm_force", "claude-myproject-tong-pg"), docker.calls)
        self.assertIn(("rm_force", "claude-myproject"), docker.calls)
        self.assertIn(("network_rm", net), docker.calls)

    def test_session_tong_without_anvil_name_raises_before_any_docker_call(self):
        docker = FakeDocker()
        anvil = ["docker", "run", "-it", "--rm", "--network", "opencode-net", "img"]
        with self.assertRaises(run_anvil.OrchestrationError):
            run_anvil.run_with_tongs(
                _merged("pg", SESSION_PORT, source=tongs.REPO), anvil, _opts(),
                docker=docker, sleep=lambda _s: None, monotonic=_Clock(),
            )
        self.assertEqual(docker.calls, [])  # nothing created => nothing to tear down

    # --- Per-org isolation of `shared` tongs --------------------------------

    _ACME = "/orgs/acme/.swarmforge/tongs"
    _GLOBEX = "/orgs/globex/.swarmforge/tongs"

    def _run_org(self, docker, merged, org_dir, harness="opencode", anvil=None):
        """Drive run_with_tongs with an org layer dir wired into the options."""
        opts = run_anvil.LauncherOptions(
            layer_dirs=[(tongs.ORG, org_dir)], workspace=None, approvals=None,
            providers=None, harness=harness, anvil_image="anvil:img", no_prompt=False,
        )
        return run_anvil.run_with_tongs(
            merged, anvil or ANVIL_ARGV, opts,
            docker=docker, sleep=lambda _s: None, monotonic=_Clock(),
        )

    def test_org_shared_tong_isolated_on_per_org_network(self):
        # An org-owned shared tong starts on its own per-org network (never the
        # shared base network), and the anvil joins that network as an extra.
        docker = FakeDocker()
        merged = {"asana": {"source": tongs.ORG, "definition": ORG_ASANA}}
        self._run_org(docker, merged, self._ACME)
        token = tongs.org_scope_token(self._ACME)
        net = tongs.shared_network_name(token)
        container = tongs.shared_container_name("asana", scope=token)
        self.assertIn(("ensure_network", net), docker.calls)
        started = docker.run_argvs[0]
        self.assertIn(container, started)
        self.assertEqual(started[started.index("--network") + 1], net)
        self.assertNotEqual(started[started.index("--network") + 1], "opencode-net")
        # The anvil keeps opencode-net as its primary (for the model backend) and
        # joins the org network as an extra via the multi-network path.
        self.assertEqual(docker.anvil_extra_networks, [net])
        self.assertEqual(
            docker.anvil_argv[docker.anvil_argv.index("--network") + 1], "opencode-net"
        )

    def test_org_shared_tong_readiness_probes_on_org_network(self):
        # A scoped shared tong with a tcp probe is checked on its org network --
        # the only network it lives on -- not on the anvil's base network.
        docker = FakeDocker()
        defn = {
            "lifecycle": "shared", "image": "asana-mcp:latest",
            "interface": {"kind": "mcp", "name": "asana-mcp", "port": 3000},
            "readiness": {"mode": "tcp"},
        }
        merged = {"asana": {"source": tongs.ORG, "definition": defn}}
        self._run_org(docker, merged, self._ACME)
        net = tongs.shared_network_name(tongs.org_scope_token(self._ACME))
        self.assertIn(("tcp_probe", net, "asana-mcp", 3000, "anvil:img"), docker.calls)

    def test_two_orgs_partition_into_distinct_containers_and_networks(self):
        # The crux: the same tong file in two orgs yields distinct containers and
        # distinct networks (so neither tears the other down, and neither is
        # reachable from the other), while the agent-facing MCP server name
        # (interface.name) stays identical in both.
        merged = {"asana": {"source": tongs.ORG, "definition": ORG_ASANA}}
        d1 = FakeDocker()
        self._run_org(d1, merged, self._ACME)
        d2 = FakeDocker()
        self._run_org(d2, merged, self._GLOBEX)

        s1, s2 = d1.run_argvs[0], d2.run_argvs[0]
        self.assertNotEqual(
            s1[s1.index("--name") + 1], s2[s2.index("--name") + 1]
        )
        self.assertNotEqual(d1.anvil_extra_networks, d2.anvil_extra_networks)
        # Same agent-facing MCP name on each org's isolated network.
        self.assertEqual(s1[s1.index("--network-alias") + 1], "asana-mcp")
        self.assertEqual(s2[s2.index("--network-alias") + 1], "asana-mcp")

    def test_non_org_shared_tong_stays_global_even_with_org_layer(self):
        # A repo-sourced shared tong keeps the base network and unscoped name even
        # when the launch also carries an org layer dir -- only org-owned shared
        # tongs are partitioned.
        docker = FakeDocker()
        merged = {"ollama": {"source": tongs.REPO, "definition": SHARED_OLLAMA}}
        self._run_org(docker, merged, self._ACME)
        started = docker.run_argvs[0]
        self.assertIn("swarmforge-shared-ollama", started)
        self.assertEqual(started[started.index("--network") + 1], "opencode-net")
        self.assertNotIn("ensure_network", [c[0] for c in docker.calls])
        self.assertIsNone(docker.anvil_extra_networks)

    def test_org_shared_network_pruned_best_effort_and_tong_left_running(self):
        # On teardown the org network is pruned best-effort (docker refuses while
        # the long-lived tong is attached, so it persists), and the shared tong is
        # force-removed only once -- at start, to clear a leftover -- never as a
        # teardown step.
        docker = FakeDocker()
        merged = {"asana": {"source": tongs.ORG, "definition": ORG_ASANA}}
        self._run_org(docker, merged, self._ACME)
        token = tongs.org_scope_token(self._ACME)
        net = tongs.shared_network_name(token)
        container = tongs.shared_container_name("asana", scope=token)
        self.assertIn(("network_rm", net), docker.calls)
        self.assertEqual(docker.calls.count(("rm_force", container)), 1)

    def test_org_shared_tong_without_anvil_name_raises_before_any_docker_call(self):
        docker = FakeDocker()
        anvil = ["docker", "run", "-it", "--rm", "--network", "opencode-net", "img"]
        merged = {"asana": {"source": tongs.ORG, "definition": ORG_ASANA}}
        with self.assertRaises(run_anvil.OrchestrationError):
            self._run_org(docker, merged, self._ACME, anvil=anvil)
        self.assertEqual(docker.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
