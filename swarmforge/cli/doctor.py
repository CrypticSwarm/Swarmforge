"""Check Swarmforge setup without changing the host.

Checks host tools, Docker, selected images, storage paths, config-layer paths,
Git identity, tong schemas, and secret-provider executables. Does not fetch
secrets, grant approvals, build images, or start containers.
"""

from swarmforge import doctor

from .args import configure_readiness


def configure(parser):
    """Options shared with the interactive setup wizard."""
    configure_readiness(parser, "Harness to check; repeat for several (default: opencode)")
    parser.add_argument("--json", action="store_true", help="Print machine-readable checks")


def run(options):
    """Return 1 for a failed requirement; warnings alone return 0."""
    report = doctor.inspect(options.checkout, options.project, options.harness or ["opencode"])
    doctor.display(report, options.json)
    return report.exit_code
