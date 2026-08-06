#!/usr/bin/env python3
"""Unit tests for swarmforge.tongs.validate. Run: python3 tests/test_tongs_validate.py"""

import os
import sys
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

from swarmforge import tongs

from tongs_fixtures import GITHUB_TONG, PORT_TONG, def_of


class ValidationTests(unittest.TestCase):
    def test_valid_mcp_tong(self):
        self.assertEqual(tongs.validate_tong("github", def_of(GITHUB_TONG)), [])

    def test_missing_lifecycle_and_image(self):
        errors = tongs.validate_tong("t", {"interface": {"kind": "none"}, "readiness": {"mode": "none"}})
        joined = " ".join(errors)
        self.assertIn("lifecycle", joined)
        self.assertIn("image", joined)

    def test_bad_lifecycle(self):
        errors = tongs.validate_tong("t", {"lifecycle": "forever", "image": "x", "interface": {"kind": "none"}, "readiness": {"mode": "none"}})
        self.assertTrue(any("lifecycle" in e for e in errors))

    def test_mcp_requires_port_and_name(self):
        errors = tongs.validate_tong("t", {"lifecycle": "session", "image": "x", "interface": {"kind": "mcp"}})
        joined = " ".join(errors)
        self.assertIn("port", joined)
        self.assertIn("name", joined)

    def test_mcp_rejects_non_http_transport(self):
        errors = tongs.validate_tong("t", {"lifecycle": "session", "image": "x", "interface": {"kind": "mcp", "port": 80, "name": "n", "transport": "stdio"}})
        self.assertTrue(any("transport" in e for e in errors))

    def test_port_requires_port(self):
        errors = tongs.validate_tong("t", {"lifecycle": "session", "image": "x", "interface": {"kind": "port"}})
        self.assertTrue(any("port" in e for e in errors))

    def test_tcp_readiness_rejected_for_portless_kind(self):
        # A volume/none tong has no port, so a tcp probe could never succeed;
        # validation must reject the combination rather than time out at runtime.
        errors = tongs.validate_tong("t", {
            "lifecycle": "session", "image": "x",
            "interface": {"kind": "none"}, "readiness": {"mode": "tcp"},
        })
        self.assertTrue(any("tcp" in e for e in errors))

    def test_tcp_readiness_allowed_for_port_kind(self):
        self.assertEqual(
            tongs.validate_tong("t", def_of(PORT_TONG)), []
        )

    def _base(self, **extra):
        defn = {"lifecycle": "session", "image": "x",
                "interface": {"kind": "none"}, "readiness": {"mode": "none"}}
        defn.update(extra)
        return defn

    def test_bad_readiness_timeout_rejected(self):
        defn = self._base(interface={"kind": "port", "port": 5432},
                          readiness={"mode": "tcp", "timeout": "soon"})
        errors = tongs.validate_tong("t", defn)
        self.assertTrue(any("timeout" in e for e in errors))

    def test_bad_readiness_command_rejected(self):
        defn = self._base(readiness={"mode": "healthcheck", "command": "test -d /x"})
        errors = tongs.validate_tong("t", defn)
        self.assertTrue(any("command" in e for e in errors))

    def test_unknown_mount_word_rejected(self):
        errors = tongs.validate_tong("t", self._base(mounts=["/etc/passwd:/etc/passwd"]))
        self.assertTrue(any("unknown mount" in e for e in errors))

    def test_unknown_mount_word_reported_whatever_it_carries(self):
        # The word is what is wrong, so say so rather than complaining about the
        # suffix -- a mount nobody recognizes has no meaningful target or mode.
        errors = tongs.validate_tong("t", self._base(mounts=["gpu:all"]))
        self.assertTrue(any("unknown mount" in e for e in errors))

    def test_non_string_mount_rejected(self):
        errors = tongs.validate_tong("t", self._base(mounts=[123]))
        self.assertTrue(any("mount" in e for e in errors))

    def test_known_mounts_accepted(self):
        self.assertEqual(tongs.validate_tong("t", self._base(mounts=["workspace:ro", "docker-socket"])), [])

    def test_rw_mount_mode_accepted(self):
        self.assertEqual(tongs.validate_tong("t", self._base(mounts=["workspace:rw"])), [])

    def test_workspace_target_path_accepted(self):
        # A custom mountpoint lets an image that expects its sources elsewhere be
        # used unmodified, with or without a trailing access mode.
        self.assertEqual(tongs.validate_tong("t", self._base(mounts=["workspace:/work"])), [])
        self.assertEqual(tongs.validate_tong("t", self._base(mounts=["workspace:/work:ro"])), [])

    def test_relative_target_path_rejected(self):
        errors = tongs.validate_tong("t", self._base(mounts=["workspace:work"]))
        self.assertTrue(any("neither an absolute target path" in e for e in errors))

    def test_root_target_path_rejected(self):
        # Including the spellings that only normalize to root: docker collapses
        # them the same way, so accepting one would bury the image's own rootfs.
        for target in ("/", "//", "/.", "/..", "/opt/.."):
            errors = tongs.validate_tong("t", self._base(mounts=["workspace:" + target]))
            self.assertTrue(
                any("not a usable target path" in e for e in errors), target
            )

    def test_target_path_with_whitespace_rejected(self):
        errors = tongs.validate_tong("t", self._base(mounts=["workspace:/work "]))
        self.assertTrue(any("whitespace" in e for e in errors))

    def test_invalid_mount_mode_rejected(self):
        errors = tongs.validate_tong("t", self._base(mounts=["workspace:z"]))
        self.assertTrue(any("neither an absolute target path" in e for e in errors))

    def test_target_overlapping_the_secret_fifo_rejected(self):
        # A secret-bearing tong gets the FIFO bind-mounted in and its wrapper execs
        # /bin/sh; a target over either shadows it or has docker create the
        # launcher's mountpoint inside the user's workspace. `//run` is the same
        # destination as `/run`, so it goes the same way.
        for target in ("/run/swarmforge/secret-env", "/run", "//run", "/bin", "/bin/sh/x"):
            defn = self._base(mounts=["workspace:" + target])
            defn["env"] = {"TOKEN": "${secret:op:op://Work/t}"}
            errors = tongs.validate_tong("t", defn)
            self.assertTrue(any("overlaps" in e for e in errors), target)

    def test_target_overlapping_the_socket_mount_rejected(self):
        defn = self._base(mounts=["workspace:/var/run", "docker-socket"])
        errors = tongs.validate_tong("t", defn)
        self.assertTrue(any("overlaps" in e for e in errors))

    def test_target_only_reserved_for_the_tongs_that_use_it(self):
        # A tong with no secrets and no socket mount has none of that wiring, so
        # nothing is reserved and those paths are ordinary targets.
        for target in ("/run", "/bin", "/var/run"):
            self.assertEqual(
                tongs.validate_tong("t", self._base(mounts=["workspace:" + target])), [], target
            )

    def test_target_beside_a_launcher_path_accepted(self):
        # Only overlap is refused: a sibling of a reserved path is left alone.
        defn = self._base(mounts=["workspace:/run/swarmforge/other"])
        defn["env"] = {"TOKEN": "${secret:op:op://Work/t}"}
        self.assertEqual(tongs.validate_tong("t", defn), [])
        self.assertEqual(tongs.validate_tong("t", self._base(mounts=["workspace:/runner"])), [])

    def test_two_mounts_on_the_same_destination_rejected(self):
        # Docker refuses a duplicate destination outright and creates a nested one's
        # mountpoint inside the outer bind; both are caught before the launch.
        for mounts in (
            ["workspace", "workspace:/workspace"],
            ["workspace:/code", "workspace:/code:ro"],
            ["workspace:/code", "workspace:/code/sub"],
            ["docker-socket", "workspace:/var/run"],
        ):
            errors = tongs.validate_tong("t", self._base(mounts=mounts))
            self.assertTrue(any("overlaps" in e for e in errors), mounts)

    def test_two_mounts_on_separate_destinations_accepted(self):
        self.assertEqual(
            tongs.validate_tong(
                "t", self._base(mounts=["workspace:/code", "docker-socket"])
            ),
            [],
        )

    def test_mode_before_target_rejected(self):
        errors = tongs.validate_tong("t", self._base(mounts=["workspace:ro:/work"]))
        self.assertTrue(any("must be the last field" in e for e in errors))

    def test_two_target_paths_rejected(self):
        errors = tongs.validate_tong("t", self._base(mounts=["workspace:/a:/b"]))
        self.assertTrue(any("more than one target path" in e for e in errors))

    def test_socket_mount_target_rejected(self):
        # The socket has to keep its host path inside the container -- that is
        # where a docker client looks for it -- so only `workspace` takes a target.
        errors = tongs.validate_tong("t", self._base(mounts=["docker-socket:/run/d.sock"]))
        self.assertTrue(any("only the 'workspace' mount takes a target path" in e for e in errors))

    def test_extra_aliases_accepted_on_network_facing_kinds(self):
        # Dotted names are the point: a client that must match a certificate CN
        # dials the tong by that name, not by the canonical alias.
        defn = self._base(interface={
            "kind": "port", "port": 3000,
            "aliases": ["api", "console", "local.example.test"],
        })
        self.assertEqual(tongs.validate_tong("t", defn), [])

    def test_extra_aliases_rejected_without_a_listener(self):
        # volume/none tongs register no DNS name at all, so extra aliases there
        # would silently do nothing.
        errors = tongs.validate_tong("t", self._base(
            interface={"kind": "none", "aliases": ["api"]}))
        self.assertTrue(any("aliases" in e for e in errors))

    def test_non_list_aliases_rejected(self):
        errors = tongs.validate_tong("t", self._base(
            interface={"kind": "port", "port": 3000, "aliases": "api"}))
        self.assertTrue(any("aliases" in e for e in errors))

    def test_malformed_alias_rejected(self):
        for bad in ["-api", "api-", "ap i", "api..b", "under_score", "", 7, None]:
            errors = tongs.validate_tong("t", self._base(
                interface={"kind": "port", "port": 3000, "aliases": [bad]}))
            self.assertTrue(any("aliases" in e for e in errors), bad)

    def test_over_long_alias_rejected(self):
        # Per-label and total length are both bounded by what DNS accepts.
        errors = tongs.validate_tong("t", self._base(
            interface={"kind": "port", "port": 3000, "aliases": ["a" * 64]}))
        self.assertTrue(any("aliases" in e for e in errors))
        errors = tongs.validate_tong("t", self._base(
            interface={"kind": "port", "port": 3000,
                       "aliases": [".".join(["abcdefghij"] * 26)]}))
        self.assertTrue(any("aliases" in e for e in errors))

    def test_non_string_network_rejected(self):
        errors = tongs.validate_tong("t", self._base(networks=[{"name": "x"}]))
        self.assertTrue(any("network" in e for e in errors))

    def test_resources_must_be_mapping(self):
        errors = tongs.validate_tong("t", self._base(resources="512m"))
        self.assertTrue(any("resources" in e for e in errors))

    def test_resources_memory_type_checked(self):
        errors = tongs.validate_tong("t", self._base(resources={"memory": ["512m"]}))
        self.assertTrue(any("memory" in e for e in errors))
        self.assertEqual(tongs.validate_tong("t", self._base(resources={"memory": "512m"})), [])

    def test_volume_requires_volume_mountpoint_and_readiness_mode(self):
        errors = tongs.validate_tong("t", {"lifecycle": "session", "image": "x", "interface": {"kind": "volume"}})
        joined = " ".join(errors)
        self.assertIn("volume", joined)
        self.assertIn("mountpoint", joined)
        self.assertIn("readiness", joined)

    def test_valid_volume_tong(self):
        ok = tongs.validate_tong("cache", {
            "lifecycle": "session",
            "image": "x",
            "interface": {"kind": "volume", "volume": "build-cache", "mountpoint": "/cache"},
            "readiness": {"mode": "healthcheck", "command": ["test", "-d", "/cache"]},
        })
        self.assertEqual(ok, [])

    def test_none_requires_explicit_readiness_mode(self):
        errors = tongs.validate_tong("t", {"lifecycle": "session", "image": "x", "interface": {"kind": "none"}})
        self.assertTrue(any("readiness" in e for e in errors))
        # ...and is satisfied once a mode is declared.
        ok = tongs.validate_tong("t", {"lifecycle": "session", "image": "x", "interface": {"kind": "none"}, "readiness": {"mode": "none"}})
        self.assertEqual(ok, [])

    def test_bad_interface_kind(self):
        errors = tongs.validate_tong("t", {"lifecycle": "session", "image": "x", "interface": {"kind": "socket"}})
        self.assertTrue(any("interface.kind" in e for e in errors))

    def test_rejects_invalid_secret_env_name(self):
        errors = tongs.validate_tong("t", {
            "lifecycle": "session",
            "image": "x",
            "interface": {"kind": "none"},
            "readiness": {"mode": "none"},
            "env": {"a/b": "${secret:op:t}"},
        })
        self.assertTrue(any("a/b" in e for e in errors))

    def test_rejects_non_list_entrypoint_or_command(self):
        for field in ("entrypoint", "command"):
            errors = tongs.validate_tong("t", {
                "lifecycle": "session", "image": "x",
                "interface": {"kind": "none"}, "readiness": {"mode": "none"},
                field: "node server.js",  # must be a list of strings
            })
            self.assertTrue(any(field in e for e in errors), field)

    def test_accepts_list_entrypoint_and_command(self):
        errors = tongs.validate_tong("t", {
            "lifecycle": "session", "image": "x",
            "interface": {"kind": "none"}, "readiness": {"mode": "none"},
            "entrypoint": ["node"], "command": ["server.js"],
        })
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
