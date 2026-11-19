#!/usr/bin/env python3
"""Tests for swarmforge.doctor.

Host checks must stay read-only, honor Make overrides, and hide secrets.
Run: python3 tests/test_doctor.py
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).absolute().parent
REPO_ROOT = HERE.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from swarmforge import doctor, harness
from swarmforge.tongs.secrets import UnmappedSecretError


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = {key: "" for key in ("image", "data_dir", "home_dir", "user_config", "org_config",
                         "repo_config", "user_assets", "org_assets", "repo_tongs", "org_root", "gitconfig", "env_file")}
        self.settings.update(image="test:local", data_dir=str(self.root / "data"),
                             user_config=str(self.root / "config"), user_assets=str(self.root / "assets"),
                             skills=str(REPO_ROOT / "skills"), commands=str(REPO_ROOT / "commands"))
        self.commands = []

    def fake_probe(self, argv, **kwargs):
        self.commands.append(argv)
        output = ""
        if argv[:2] == ["make", "--version"]:
            output = "GNU Make 3.81"
        elif argv[:2] == ["make", "--no-print-directory"]:
            output = json.dumps(self.settings)
        elif argv[:2] == ["docker", "info"]:
            output = "linux\n"
        elif argv[0] == "git":
            return subprocess.CompletedProcess(argv, 1, "", "")
        return subprocess.CompletedProcess(argv, 0, output, "")

    def inspect(self, harnesses=None):
        with mock.patch.object(doctor, "probe", side_effect=self.fake_probe), \
                mock.patch.object(doctor.shutil, "which", return_value="/bin/tool"):
            return doctor.inspect(REPO_ROOT, self.root, harnesses or ["codex"])

    def test_checks_never_run_mutations_or_provider_commands(self):
        report = self.inspect()
        self.assertEqual(report.exit_code, 0)
        docker_commands = [cmd for cmd in self.commands if cmd[0] == "docker"]
        self.assertEqual([cmd[1] for cmd in docker_commands], ["info", "buildx", "image"])
        self.assertFalse((self.root / "data").exists())
        self.assertFalse((self.root / "config").exists())
        self.assertTrue(any(c.name == "codex.authentication" and c.status == "WARN" for c in report.checks))

    def test_shared_tong_layers_are_checked_once_across_harnesses(self):
        with mock.patch.object(doctor, "tong_checks", wraps=doctor.tong_checks) as checks:
            report = self.inspect(["codex", "claude"])
        self.assertEqual(set(report.settings), {"codex", "claude"})
        checks.assert_called_once()
        self.assertEqual(sum(c.name == "tongs" for c in report.checks), 1)
        for name in ("codex", "claude"):
            self.assertTrue(any(c.name == name + ".image" for c in report.checks))

    def test_distinct_tong_layers_are_each_checked(self):
        original = self.fake_probe

        def per_harness_settings(argv, **kwargs):
            result = original(argv, **kwargs)
            if "doctor-config_claude" in argv:
                settings = dict(self.settings, org_assets=str(self.root / "other-org"))
                result.stdout = json.dumps(settings)
            return result

        with mock.patch.object(doctor, "probe", side_effect=per_harness_settings), \
                mock.patch.object(doctor.shutil, "which", return_value="/bin/tool"), \
                mock.patch.object(doctor, "tong_checks", wraps=doctor.tong_checks) as checks:
            report = doctor.inspect(REPO_ROOT, self.root, ["codex", "claude"])
        self.assertEqual(checks.call_count, 2)
        self.assertEqual(sum(c.name == "tongs" for c in report.checks), 2)

    def test_missing_tools_fail_cleanly_and_skip_make(self):
        with mock.patch.object(doctor.shutil, "which", return_value=None), \
                mock.patch.object(doctor, "probe") as probe:
            report = doctor.inspect(REPO_ROOT, self.root, ["codex"])
        self.assertEqual(report.exit_code, 1)
        probe.assert_not_called()

    def test_failed_image_inspection_is_actionable(self):
        original = self.fake_probe

        def missing_image(argv, **kwargs):
            if argv[:3] == ["docker", "image", "inspect"]:
                return subprocess.CompletedProcess(argv, 1, "", "error")
            return original(argv, **kwargs)

        with mock.patch.object(doctor, "probe", side_effect=missing_image), \
                mock.patch.object(doctor.shutil, "which", return_value="/bin/tool"):
            report = doctor.inspect(REPO_ROOT, self.root, ["codex"])
        image = next(c for c in report.checks if c.name == "codex.image")
        self.assertEqual(image.status, "FAIL")
        self.assertIn("build_codex", image.remedy)

    def test_broken_directory_parent_fails_without_creating_anything(self):
        obstacle = self.root / "file"
        obstacle.write_text("file")
        self.assertFalse(doctor.writable_directory(obstacle / "child"))
        (self.root / "dangling").symlink_to(self.root / "absent")
        self.assertFalse(doctor.writable_directory(self.root / "dangling"))
        self.assertTrue(doctor.writable_directory(self.root / "new" / "nested"))
        self.assertFalse((self.root / "new").exists())

    def test_json_is_one_parseable_report(self):
        report = self.inspect()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            doctor.display(report, True)
        result = json.loads(out.getvalue())
        self.assertEqual(result["exit_code"], 0)
        self.assertIsInstance(result["checks"], list)

    def test_provider_is_checked_but_never_executed_or_disclosed(self):
        assets = self.root / "assets"
        (assets / "tongs").mkdir(parents=True)
        (assets / "tongs" / "service.yaml").write_text(
            'lifecycle: session\nimage: example:local\ninterface:\n  kind: none\n'
            'readiness:\n  mode: none\nenv:\n  TOKEN: "${secret:vault:private-reference}"\n')
        (assets / "secret-providers.yaml").write_text('providers:\n  vault: ["provider-tool", "{ref}"]\n')
        report = self.inspect()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            doctor.display(report, True)
        self.assertIn("Secret-provider executable found", out.getvalue())
        self.assertNotIn("private-reference", out.getvalue())
        self.assertNotIn("provider-tool", [cmd[0] for cmd in self.commands])

    def test_unmapped_structured_provider_fails_without_disclosing_reference(self):
        definition = {"lifecycle": "session", "image": "x", "interface": {"kind": "none"},
                      "readiness": {"mode": "none"}, "env": {"TOKEN": "${secret:vault:private}"}}
        with mock.patch.object(doctor.tongs, "merge_tongs", return_value={
                "service": {"source": "user", "definition": definition}}), \
                mock.patch.object(doctor, "secret_provider_command", side_effect=UnmappedSecretError("vault", "private")):
            report = self.inspect()
        self.assertEqual(report.exit_code, 1)
        self.assertFalse(any("private" in c.detail for c in report.checks))

    def test_missing_provider_mapping_fails(self):
        with mock.patch.object(doctor.tongs, "discover", return_value=[]), \
                mock.patch.object(doctor.tongs, "merge_tongs", return_value={
                    "service": {"source": "user", "definition": {
                        "lifecycle": "session", "image": "x", "interface": {"kind": "none"},
                        "readiness": {"mode": "none"}, "env": {"TOKEN": "${secret:missing:private}"}}}}):
            report = self.inspect()
        self.assertEqual(report.exit_code, 1)
        self.assertTrue(any(c.name == "tong.service.provider" and c.status == "FAIL" for c in report.checks))

    def test_timeouts_and_missing_executables_are_clean_failures(self):
        for error in (FileNotFoundError(), subprocess.TimeoutExpired("docker", 15)):
            with self.subTest(error=error), mock.patch.object(subprocess, "run", side_effect=error):
                self.assertEqual(doctor.probe(["docker", "info"]).returncode, 1)

    def test_make_exports_actual_defaults_for_every_harness(self):
        for name in harness.names():
            with self.subTest(harness=name):
                result = subprocess.run(doctor.make_command(REPO_ROOT, self.root, "doctor-config_" + name),
                                        capture_output=True, text=True, check=True)
                settings = json.loads(result.stdout)
                self.assertTrue(settings["image"])
                self.assertTrue(settings["data_dir"].endswith("/" + name))
                if settings["home_dir"]:
                    self.assertEqual(settings["home_dir"], settings["data_dir"] + "/home")

    def test_make_export_preserves_environment_overrides_and_shell_characters(self):
        strange_path = str(self.root / "space ' dollar $ backtick ` quote \"")
        env = dict(os.environ, CODEX_HOME_DIR=strange_path.replace("$", "$$"),
                   CODEX_IMG="custom:tag", SWARMFORGE_USER_CONFIG_DIR=strange_path.replace("$", "$$"))
        result = subprocess.run(doctor.make_command(REPO_ROOT, self.root, "doctor-config_codex"),
                                capture_output=True, text=True, check=True, env=env)
        settings = json.loads(result.stdout)
        self.assertEqual(settings["home_dir"], strange_path)
        self.assertEqual(settings["user_config"], strange_path)
        self.assertEqual(settings["image"], "custom:tag")

    def test_skipped_bad_tong_is_not_reported_as_success(self):
        def broken_discovery(layers):
            print("tongs: skipping malformed definition", file=sys.stderr)
            return []
        with mock.patch.object(doctor.tongs, "discover", side_effect=broken_discovery):
            report = self.inspect()
        self.assertEqual(report.exit_code, 1)


if __name__ == "__main__":
    unittest.main()
