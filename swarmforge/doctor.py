"""Read-only host readiness checks, using the Makefile's resolved settings.

No check runs a provider command, fetches an image, changes approval state, or
starts a container. PASS records only the property named by that check.
"""

import contextlib
import dataclasses
import io
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from swarmforge import tongs
from swarmforge.tongs.model import printable
from swarmforge.tongs.secrets import (
    UnmappedSecretError, find_secret_refs, load_secret_providers,
    secret_provider_command,
)


STATUS_ICONS = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌", "SKIP": "⏭️"}


@dataclasses.dataclass(frozen=True)
class Check:
    """One independently actionable observation, with no secret values."""

    name: str
    status: str
    detail: str
    remedy: str = ""


@dataclasses.dataclass
class Report:
    """Checks plus resolved per-harness settings for the setup wizard."""

    checks: list = dataclasses.field(default_factory=list)
    settings: dict = dataclasses.field(default_factory=dict)
    workspace: str = ""

    def add(self, name, status, detail, remedy=""):
        self.checks.append(Check(name, status, detail, remedy))

    @property
    def exit_code(self):
        return int(any(check.status == "FAIL" for check in self.checks))


def probe(argv, timeout=15, **kwargs):
    """Capture a bounded diagnostic command; do not echo its output on failure."""
    try:
        return subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, **kwargs)
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(argv, 1, "", "")


def make_command(checkout, project, target):
    """Use this checkout's rules even when the project has its own Makefile."""
    return ["make", "--no-print-directory", "-s", "-f",
            str(Path(checkout) / "Makefile"), target,
            "PROJECT_DIR=" + str(project)]


def writable_directory(path):
    """Check an existing directory or its nearest existing creation parent."""
    candidate = Path(path)
    while not candidate.exists() and not candidate.is_symlink():
        parent = candidate.parent
        if parent == candidate:
            return False
        candidate = parent
    return candidate.is_dir() and os.access(candidate, os.W_OK | os.X_OK)


def tong_checks(report, settings, workspace):
    """Check effective definitions and provider executables without fetching secrets."""
    layers = [
        ("user", str(Path(settings["user_assets"]) / "tongs")),
        ("org", str(Path(settings["org_assets"]) / "tongs")
         if settings["org_assets"] else ""),
        ("repo", settings["repo_tongs"]),
        ("workspace", str(Path(workspace) / ".swarmforge/tongs")),
    ]
    diagnostics = io.StringIO()
    try:
        with contextlib.redirect_stderr(diagnostics):
            merged = tongs.merge_tongs(tongs.discover(layers))
        if diagnostics.getvalue():
            status = "FAIL" if "skipping " in diagnostics.getvalue() else "WARN"
            report.add("tongs.discovery", status, "Some definitions were skipped or overridden.",
                       "Run bin/tongs validate with your layer directories for details.")
        providers = {}
        if merged:
            providers = load_secret_providers(
                str(Path(settings["user_assets"]) / "secret-providers.yaml"))
        for name, entry in merged.items():
            definition = entry["definition"]
            errors = tongs.validate_tong(name, definition)
            if errors:
                report.add("tong." + name, "FAIL", "Definition does not match the tong schema.",
                           "Run bin/tongs validate with your layer directories for details.")
                continue
            report.add("tong." + name, "PASS", "Definition matches the tong schema.")
            if entry["source"] == "workspace":
                report.add("tong." + name + ".approval", "WARN",
                           "Workspace tong requires the launcher's approval check.",
                           "Review privileges at first launch; setup does not grant approval.")
            for provider, ref in find_secret_refs(definition):
                try:
                    argv = secret_provider_command(providers, provider, ref)
                except (KeyError, ValueError, UnmappedSecretError):
                    report.add("tong." + name + ".provider", "FAIL",
                               "A secret reference has no provider command.",
                               "Configure secret-providers.yaml in the user assets directory.")
                    continue
                available = shutil.which(argv[0]) is not None
                report.add("tong." + name + ".provider", "PASS" if available else "FAIL",
                           "Secret-provider executable found; authentication is untested."
                           if available else "Secret-provider executable is missing.",
                           "Install the configured provider CLI and sign in." if not available else "")
        report.add("tongs", "PASS", "%d effective definition(s) checked." % len(merged))
    except (OSError, ValueError, TypeError):
        report.add("tongs", "FAIL", "Cannot read tong definitions or secret-provider configuration.",
                   "Check layer permissions and secret-providers.yaml syntax.")


def _inspect_harness(report, checkout, project, harness, daemon_ok):
    """Check one harness using its resolved Make settings and workspace."""
    result = probe(make_command(checkout, project, "doctor-config_" + harness))
    try:
        if result.returncode:
            raise ValueError()
        settings = json.loads(result.stdout)
        required = {"image", "data_dir", "home_dir", "user_config", "org_config", "repo_config",
                    "user_assets", "org_assets", "repo_tongs", "org_root", "gitconfig", "env_file",
                    "skills", "commands"}
        if (not required <= settings.keys()
                or not all(isinstance(v, str) for v in settings.values())
                or not all(settings[key] for key in ("image", "data_dir", "user_config", "user_assets"))):
            raise ValueError()
    except (ValueError, AttributeError):
        report.add(harness + ".settings", "FAIL", "Could not resolve Makefile settings.",
                   "Run make doctor-config_%s to inspect the configuration error." % harness)
        return
    report.settings[harness] = settings
    report.add(harness + ".settings", "PASS", "Makefile settings resolved.")
    if daemon_ok:
        image = probe(["docker", "image", "inspect", "--format", "{{.Id}}", settings["image"]])
        report.add(harness + ".image", "PASS" if not image.returncode else "FAIL",
                   settings["image"] if not image.returncode else "Image is missing: " + settings["image"],
                   shlex.join(make_command(checkout, project, "build_" + harness)) if image.returncode else "")
    else:
        report.add(harness + ".image", "SKIP", "Image check requires a reachable Linux Docker daemon.")
    for key in ("data_dir", "home_dir", "user_config", "user_assets"):
        path = settings[key]
        if not path:
            continue
        writable = writable_directory(path)
        report.add(harness + "." + key, "PASS" if writable else "FAIL", path,
                   "Choose a writable directory using the documented environment overrides." if not writable else "")
    for key in ("org_root", "org_config", "repo_config"):
        path = settings[key]
        if path:
            exists = Path(path).is_dir() and os.access(path, os.R_OK | os.X_OK)
            report.add(harness + "." + key, "PASS" if exists else "WARN", path,
                       "This optional layer is absent or unreadable; check your overrides." if not exists else "")
    for key in ("skills", "commands"):
        path = settings[key]
        readable = Path(path).is_dir() and os.access(path, os.R_OK | os.X_OK)
        report.add(harness + "." + key, "PASS" if readable else "FAIL", path,
                   "Restore the shared asset directory or correct its override." if not readable else "")
    env_file = Path(settings["env_file"])
    if settings["env_file"] and env_file.exists():
        readable = env_file.is_file() and os.access(env_file, os.R_OK)
        report.add(harness + ".env_file", "PASS" if readable else "FAIL", str(env_file),
                   "ENV_FILE must be a readable file; its contents are not printed." if not readable else "")
    gitconfig = settings["gitconfig"]
    identity = False
    if Path(gitconfig).is_file():
        identity = all(
            probe(["git", "config", "--file", gitconfig, "--get", "user." + key]).stdout.strip()
            for key in ("name", "email"))
    report.add(harness + ".git_identity", "PASS" if identity else "WARN",
               "Mounted Git config has a name and email." if identity else "Mounted Git identity is incomplete.",
               "Set user.name and user.email in GITCONFIG_FILE; the container mounts this file." if not identity else "")
    report.add(harness + ".authentication", "WARN", "Harness login and model access have not been tested.",
               "Launch run_%s and complete the harness's own login flow." % harness)


def inspect(checkout, project, harnesses):
    """Return a report without changing the host, containers, or credential state."""
    report = Report()
    checkout, project = Path(checkout).absolute(), Path(project).absolute()
    supported = platform.system() in ("Darwin", "Linux")
    report.add("platform", "PASS" if supported else "FAIL", platform.system(),
               "Run inside Linux, macOS, or WSL 2." if not supported else "")
    python_ok = sys.version_info >= (3, 9)
    report.add("python", "PASS" if python_ok else "FAIL", platform.python_version(),
               "Install Python 3.9 or newer." if not python_ok else "")
    tools = {}
    for name in ("git", "make", "bash", "docker"):
        tools[name] = shutil.which(name)
        report.add(name, "PASS" if tools[name] else "FAIL", tools[name] or "Not on PATH.",
                   "Install %s and add it to PATH; see docs/installation.md." % name
                   if not tools[name] else "")
    if not project.is_dir():
        report.add("project", "FAIL", "Project directory does not exist.", "Choose an existing --project directory.")
        return report
    if not all((checkout / item).is_file() for item in ("Makefile", "install.sh", "bin/swarmforge")):
        report.add("checkout", "FAIL", "Swarmforge checkout is incomplete.", "Use --checkout to point at the Swarmforge repository.")
        return report
    report.add("checkout", "PASS", str(checkout))
    command = shutil.which("swarmforge")
    linked = bool(command and Path(command).resolve() == (checkout / "bin/swarmforge").resolve())
    report.add("shell.command", "PASS" if linked else "WARN",
               "PATH resolves to this checkout." if linked else "This checkout's CLI is not on PATH.",
               "Run setup's shell integration step, then add ~/.local/bin to PATH and open a new shell."
               if not linked else "")
    if tools["make"]:
        version = probe(["make", "--version"])
        if version.returncode or "GNU Make" not in version.stdout:
            report.add("make.gnu", "FAIL", "GNU Make is required.", "Install GNU Make and expose it as make.")
            tools["make"] = None
    daemon_ok = False
    if tools["docker"]:
        info = probe(["docker", "info", "--format", "{{.OSType}}"])
        daemon_ok = info.returncode == 0 and info.stdout.strip() == "linux"
        report.add("docker.daemon", "PASS" if daemon_ok else "FAIL",
                   "Linux daemon is reachable." if daemon_ok else "Cannot reach a Linux Docker daemon.",
                   "Start Docker, select a Linux context, and check docker info. On WSL, enable Docker Desktop integration."
                   if not daemon_ok else "")
        buildx = probe(["docker", "buildx", "version"])
        report.add("docker.buildkit", "PASS" if not buildx.returncode else "FAIL",
                   "Buildx is available." if not buildx.returncode else "Buildx is unavailable.",
                   "Install Docker's Buildx plugin for BuildKit builds." if buildx.returncode else "")
        report.add("docker.mounts", "WARN", "Doctor does not exercise host bind mounts.",
                   "Use setup's optional smoke test to check the project mount on the selected daemon.")
    if not all(tools[name] for name in ("make", "git", "bash")) or not python_ok:
        return report
    root = probe(["git", "-C", str(project), "rev-parse", "--show-toplevel"])
    workspace = root.stdout.strip() if root.returncode == 0 else str(project)
    report.workspace = workspace
    report.add("project", "PASS", workspace)
    for harness in harnesses:
        _inspect_harness(report, checkout, project, harness, daemon_ok)
    checked_layers = set()
    for settings in report.settings.values():
        layers = tuple(settings[key] for key in ("user_assets", "org_assets", "repo_tongs"))
        if layers not in checked_layers:
            tong_checks(report, settings, workspace)
            checked_layers.add(layers)
    return report


def display(report, as_json=False):
    """Print observations with stable status words, or a machine-readable report."""
    if as_json:
        print(json.dumps({"checks": [dataclasses.asdict(c) for c in report.checks],
                          "exit_code": report.exit_code}, indent=2))
        return
    for check in report.checks:
        print("%s %-4s %-28s %s" % (
            STATUS_ICONS[check.status], check.status,
            printable(check.name), printable(check.detail)))
        if check.remedy:
            print("        " + printable(check.remedy))
    counts = {status: sum(c.status == status for c in report.checks)
              for status in ("PASS", "WARN", "FAIL", "SKIP")}
    summary = ", ".join("%s %d %s" % (STATUS_ICONS[status], count, status)
                        for status, count in counts.items())
    print("\n" + summary)
    print("Login, config contents, network access, and runtime readiness require a real harness session.")
