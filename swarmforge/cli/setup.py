"""Walk through Swarmforge setup with confirmation before each change.

Choose harnesses, check prerequisites, optionally install shell integration,
build missing images, and smoke-test the project mount. Missing host packages
are reported with guidance; this wizard never runs sudo or logs into providers.
"""

import csv
import io
import shlex
import subprocess
import sys
import uuid
from pathlib import Path

from swarmforge import doctor, harness

from .args import configure_readiness


def configure(parser):
    """Register setup's checkout, project, and harness selection."""
    configure_readiness(parser, "Harness to set up; repeat for several")


def confirm(question):
    """Default every mutation to no, including end-of-input."""
    try:
        return input(question + " [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def execute(argv, timeout=None):
    """Show the exact action, then preserve its terminal output."""
    print("Running: " + shlex.join(argv), flush=True)
    try:
        result = subprocess.run(argv, timeout=timeout)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        print("Action failed or timed out. Check the command above.", file=sys.stderr)
        return False


def smoke_test(image, workspace, name):
    """Remove our own temporary container even after a timeout or interruption."""
    container = "swarmforge-setup-" + uuid.uuid4().hex
    mount = io.StringIO()
    csv.writer(mount, lineterminator="").writerow([
        "type=bind", "src=" + str(workspace), "dst=/workspace", "readonly"])
    try:
        return execute(["docker", "run", "--rm", "--name", container,
                        "--pull=never", "--network", "none", "--entrypoint", "/bin/sh",
                        "--mount", mount.getvalue(), image, "-c",
                        'test -d /workspace && command -v "$1"', "swarmforge-smoke", name], timeout=60)
    finally:
        doctor.probe(["docker", "rm", "-f", container])


def choose_harnesses():
    """Ask until selection is valid; EOF cancels setup."""
    names = harness.names()
    while True:
        try:
            value = input("Harnesses (%s, or all) [opencode]: " % ", ".join(names)).strip()
        except EOFError:
            return []
        selected = names if value == "all" else (value.replace(",", " ").split() or ["opencode"])
        if all(name in names for name in selected):
            return list(dict.fromkeys(selected))
        print("Choose names from the list.")


def run(options):
    """Check, offer explicit actions, and report the remaining requirements."""
    if not sys.stdin.isatty():
        print("Setup needs an interactive terminal. Use swarmforge doctor --json for unattended checks.",
              file=sys.stderr)
        return 2
    selected = options.harness or choose_harnesses()
    if not selected:
        return 130
    checkout = Path(options.checkout).absolute()
    project = Path(options.project).absolute()
    print("\nChecking setup for: " + ", ".join(selected))
    report = doctor.inspect(checkout, project, selected)
    doctor.display(report)
    blockers = [c for c in report.checks if c.status == "FAIL" and not c.name.endswith(".image")]
    if blockers:
        print("\nFix the failed requirements above, then run setup again.")
        return 1
    if confirm("Install the CLI link and oc alias? This runs install.sh and can update your shell rc file."):
        if not execute(["bash", str(checkout / "install.sh")]):
            return 1
        print("Open a new shell to load the alias. Add ~/.local/bin to PATH if the installer requests it.")
    for name in selected:
        settings = report.settings.get(name)
        if not settings:
            continue
        image_missing = any(c.name == name + ".image" and c.status == "FAIL" for c in report.checks)
        if image_missing and confirm("Build %s (%s)? This downloads dependencies and uses Docker disk space."
                                     % (name, settings["image"])):
            if not execute(doctor.make_command(checkout, project, "build_" + name)):
                return 1
        if confirm("Smoke-test %s? This starts a temporary container with a read-only project mount and no network."
                   % name):
            if not smoke_test(settings["image"], report.workspace or project, name):
                return 1
            print("%s PASS %s smoke test: image starts, project mounts, "
                  "harness executable is present." % (doctor.STATUS_ICONS["PASS"], name))
    print("\nFinal setup report:")
    report = doctor.inspect(checkout, project, selected)
    doctor.display(report)
    print("\nResolve failed checks before launching." if report.exit_code else
          "\nNext: launch from your project and complete the harness login.")
    for name in selected:
        print(shlex.join(doctor.make_command(checkout, project, "run_" + name)))
    return report.exit_code
