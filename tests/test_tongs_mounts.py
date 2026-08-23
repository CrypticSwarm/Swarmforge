#!/usr/bin/env python3
"""Unit tests for swarmforge.tongs.mounts. Run: python3 tests/test_tongs_mounts.py"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The launcher's entry-point shim puts the repo root on the path; standing in
# for it here keeps this file runnable on its own, not just under a discovery
# run that already set it.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from swarmforge import tongs


class MountGrammarTests(unittest.TestCase):
    """The mount grammar and target policy, exercised directly -- `validate_tong`
    and `tong_mount_specs` both delegate here."""

    def test_parse_mount_splits_the_optional_fields(self):
        self.assertEqual(tongs.parse_mount("workspace"), ("workspace", None, None))
        self.assertEqual(tongs.parse_mount("workspace:ro"), ("workspace", None, "ro"))
        self.assertEqual(tongs.parse_mount("workspace:/code"), ("workspace", "/code", None))
        self.assertEqual(
            tongs.parse_mount("workspace:/code:rw"), ("workspace", "/code", "rw")
        )
        self.assertEqual(
            tongs.parse_mount("docker-socket:ro"), ("docker-socket", None, "ro")
        )

    def test_parse_mount_rejects_an_unrecognized_word(self):
        # The default word set is the point of the opt-in mount vocabulary: a raw
        # host path is not a magic word, whatever it carries.
        with self.assertRaisesRegex(ValueError, "unknown mount"):
            tongs.parse_mount("/etc/passwd:/etc/passwd")

    def test_parse_mount_word_set_can_be_narrowed(self):
        # A real magic word refused by a narrowed set is a policy refusal, so the
        # message must not read like a spelling mistake.
        narrowed = (tongs.WORKSPACE_MOUNT,)
        self.assertEqual(
            tongs.parse_mount("workspace:/code", words=narrowed), ("workspace", "/code", None)
        )
        with self.assertRaisesRegex(ValueError, "not allowed here"):
            tongs.parse_mount("docker-socket", words=narrowed)

    def test_mount_destination_refuses_a_word_with_no_default(self):
        # Guards the next magic word: without its own default it must not silently
        # inherit the socket's destination.
        with self.assertRaisesRegex(ValueError, "no destination"):
            tongs.mount_destination("cache", None)

    def test_normalize_mount_target_matches_dockers_cleanup(self):
        for spelling in ("/code", "//code", "/code/", "/opt/../code", "/./code"):
            self.assertEqual(tongs.normalize_mount_target(spelling), "/code", spelling)
        self.assertEqual(tongs.normalize_mount_target("/"), "/")
        self.assertEqual(tongs.normalize_mount_target("//"), "/")

    def test_reserved_targets_follow_the_definitions_wiring(self):
        self.assertEqual(tongs.reserved_mount_targets({}), {})
        secret_bearing = tongs.reserved_mount_targets({"env": {"T": "${secret:op:r}"}})
        self.assertEqual(
            sorted(secret_bearing), [tongs.SECRET_INJECT_SHELL, tongs.SECRET_FIFO_DIR]
        )
        self.assertEqual(
            list(tongs.reserved_mount_targets({"mounts": ["docker-socket"]})),
            [tongs.DEFAULT_DOCKER_SOCKET],
        )
        self.assertEqual(
            list(tongs.reserved_mount_targets({"mounts": ["docker-socket"]}, "/run/d.sock")),
            ["/run/d.sock"],
        )

    def test_mount_destination_defaults_per_word(self):
        self.assertEqual(tongs.mount_destination("workspace", None), "/workspace")
        self.assertEqual(tongs.mount_destination("workspace", "//code/"), "/code")
        self.assertEqual(
            tongs.mount_destination("docker-socket", None), tongs.DEFAULT_DOCKER_SOCKET
        )
        self.assertEqual(
            tongs.mount_destination("docker-socket", None, "/run/d.sock"), "/run/d.sock"
        )

    def test_mount_target_error_names_the_mount_and_the_reason(self):
        reserved = {"/run/x": "where the launcher delivers this tong's secrets"}

        def error(mount, word, target):
            return tongs.mount_target_error(
                mount, word, target, tongs.mount_destination(word, target), reserved
            )

        self.assertIsNone(error("workspace", "workspace", None))
        self.assertIsNone(error("workspace:/code", "workspace", "/code"))
        self.assertIsNone(error("docker-socket", "docker-socket", None))
        self.assertIn(
            "only the 'workspace' mount takes a target path",
            error("docker-socket:/s", "docker-socket", "/s"),
        )
        # Overlap in both directions: the target above the reserved path and under it.
        for target in ("/run", "/run/x/deeper"):
            self.assertIn("overlaps /run/x", error("workspace:" + target, "workspace", target))

    def test_mount_target_error_judges_the_default_destination_too(self):
        # A mount that names no target still lands somewhere, so a reserved path
        # under /workspace is caught even though the definition declares no target.
        self.assertIn(
            "overlaps /workspace/x",
            tongs.mount_target_error(
                "workspace", "workspace", None, "/workspace", {"/workspace/x": "why"}
            ),
        )

    def test_overlapping_mount_error_catches_duplicates_and_nesting(self):
        placed = [("workspace:/code", "/code")]
        self.assertIsNone(tongs.overlapping_mount_error("workspace:/src", "/src", placed))
        for destination in ("/code", "/code/sub", "/"):
            self.assertIn(
                "overlaps mount 'workspace:/code'",
                tongs.overlapping_mount_error("m", destination, placed),
            )


class MountSpecTests(unittest.TestCase):
    """The mount specs a tong contributes to its own `docker run` argv, and the
    workspace placements the anvil re-mounts from them."""

    def test_mount_specs_workspace_and_socket(self):
        defn = {"mounts": ["workspace:ro", "docker-socket"]}
        specs = tongs.tong_mount_specs(defn, "/ws")
        self.assertEqual(specs, ["/ws:/workspace:ro", "/var/run/docker.sock:/var/run/docker.sock"])

    def test_mount_specs_workspace_without_mode(self):
        self.assertEqual(tongs.tong_mount_specs({"mounts": ["workspace"]}, "/ws"), ["/ws:/workspace"])

    def test_mount_specs_workspace_custom_target(self):
        self.assertEqual(
            tongs.tong_mount_specs({"mounts": ["workspace:/work"]}, "/ws"), ["/ws:/work"]
        )
        self.assertEqual(
            tongs.tong_mount_specs({"mounts": ["workspace:/work:ro"]}, "/ws"), ["/ws:/work:ro"]
        )

    def test_mount_specs_socket_target_raises(self):
        with self.assertRaises(ValueError) as caught:
            tongs.tong_mount_specs({"mounts": ["docker-socket:/run/d.sock"]}, "/ws")
        self.assertIn("only the 'workspace' mount takes a target path", str(caught.exception))

    def test_mount_specs_normalize_the_target(self):
        # docker cleans a bind destination, so the launcher hands it the spelling
        # its own overlap checks judged -- `//code` is not an empty first field.
        self.assertEqual(
            tongs.tong_mount_specs({"mounts": ["workspace://code"]}, "/ws"), ["/ws:/code"]
        )
        self.assertEqual(
            tongs.tong_mount_specs({"mounts": ["workspace:/opt/../code:ro"]}, "/ws"),
            ["/ws:/code:ro"],
        )

    def test_mount_specs_target_overlapping_the_socket_raises(self):
        # Checked against the socket path actually in use, not just the default.
        with self.assertRaises(ValueError) as caught:
            tongs.tong_mount_specs(
                {"mounts": ["workspace:/opt/sock", "docker-socket"]},
                "/ws",
                socket_path="/opt/sock/d.sock",
            )
        self.assertIn("overlaps", str(caught.exception))

    def test_mount_specs_socket_honors_custom_path(self):
        specs = tongs.tong_mount_specs({"mounts": ["docker-socket:ro"]}, "/ws", socket_path="/run/d.sock")
        self.assertEqual(specs, ["/run/d.sock:/run/d.sock:ro"])

    def test_mount_specs_no_mounts_is_empty(self):
        self.assertEqual(tongs.tong_mount_specs({}, "/ws"), [])

    def test_workspace_mount_placements_empty_without_workspace_mount(self):
        self.assertEqual(tongs.workspace_mount_placements({}), [])
        self.assertEqual(
            tongs.workspace_mount_placements({"mounts": ["docker-socket"]}), []
        )

    def test_workspace_mount_placements_default_target_and_mode(self):
        self.assertEqual(
            tongs.workspace_mount_placements({"mounts": ["workspace"]}),
            [("/workspace", None)],
        )

    def test_workspace_mount_placements_custom_target_and_mode(self):
        self.assertEqual(
            tongs.workspace_mount_placements({"mounts": ["workspace:/code:ro"]}),
            [("/code", "ro")],
        )

    def test_workspace_mount_placements_normalizes_and_keeps_order(self):
        self.assertEqual(
            tongs.workspace_mount_placements(
                {"mounts": ["workspace://a:ro", "docker-socket", "workspace:/b"]}
            ),
            [("/a", "ro"), ("/b", None)],
        )

    def test_workspace_mount_placements_malformed_entry_raises(self):
        with self.assertRaises(ValueError):
            tongs.workspace_mount_placements({"mounts": [42]})

    def test_mount_specs_workspace_without_workspace_path_raises(self):
        with self.assertRaises(ValueError):
            tongs.tong_mount_specs({"mounts": ["workspace"]}, "")

    def test_mount_specs_unknown_word_raises(self):
        with self.assertRaises(ValueError):
            tongs.tong_mount_specs({"mounts": ["/etc/passwd:/etc/passwd"]}, "/ws")

    def test_mount_specs_non_string_raises(self):
        with self.assertRaises(ValueError):
            tongs.tong_mount_specs({"mounts": [123]}, "/ws")


if __name__ == "__main__":
    unittest.main(verbosity=2)
