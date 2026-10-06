#!/usr/bin/env python3
"""Unit tests for swarmforge.tongs.mounts. Run: python3 tests/test_tongs_mounts.py"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Standing in for the launcher's entry-point shim keeps this file runnable on its own.
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

    def test_parse_mount_rejects_a_raw_host_path(self):
        with self.assertRaisesRegex(ValueError, "unknown mount"):
            tongs.parse_mount("/etc/passwd:/etc/passwd")

    def test_parse_mount_word_set_can_be_narrowed(self):
        # A known word refused by a narrowed set is a policy refusal, not a misspelling.
        narrowed = (tongs.WORKSPACE_MOUNT,)
        self.assertEqual(
            tongs.parse_mount("workspace:/code", words=narrowed), ("workspace", "/code", None)
        )
        with self.assertRaisesRegex(ValueError, "not allowed here"):
            tongs.parse_mount("docker-socket", words=narrowed)

    def test_mount_destination_refuses_a_word_with_no_default(self):
        # A word without its own default must not inherit the socket's destination.
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

    def test_tmux_socket_reserved_only_with_the_mount(self):
        self.assertEqual(
            tongs.reserved_mount_targets({"mounts": ["tmux-socket"]}),
            {tongs.TMUX_MOUNT_TARGET: "where the tmux socket directory is mounted"},
        )
        self.assertNotIn(
            tongs.TMUX_MOUNT_TARGET,
            tongs.reserved_mount_targets({"mounts": ["workspace", "docker-socket"]}),
        )

    def test_tmux_socket_destination_is_fixed_beside_the_secret_tmpfs(self):
        self.assertEqual(tongs.mount_destination("tmux-socket", None), "/run/swarmforge-tmux")
        self.assertEqual(tongs.TMUX_MOUNT_TARGET, "/run/swarmforge-tmux")
        # Under the secret tmpfs it would be buried by it.
        self.assertIsNone(tongs.overlapping_mount_error(
            "tmux-socket", tongs.TMUX_MOUNT_TARGET,
            [("secrets", tongs.SECRET_FIFO_DIR)],
        ))

    def test_tmux_socket_accepts_ro_and_refuses_rw(self):
        self.assertEqual(tongs.parse_mount("tmux-socket"), ("tmux-socket", None, None))
        self.assertIsNone(tongs.mount_mode_error("tmux-socket", "tmux-socket", None))
        self.assertIsNone(tongs.mount_mode_error("tmux-socket:ro", "tmux-socket", "ro"))
        self.assertIn(
            "always read-only",
            tongs.mount_mode_error("tmux-socket:rw", "tmux-socket", "rw"),
        )
        self.assertIsNone(tongs.mount_mode_error("workspace:rw", "workspace", "rw"))

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
        # docker cleans a bind destination, so it is handed the spelling the overlap checks judged.
        self.assertEqual(
            tongs.tong_mount_specs({"mounts": ["workspace://code"]}, "/ws"), ["/ws:/code"]
        )
        self.assertEqual(
            tongs.tong_mount_specs({"mounts": ["workspace:/opt/../code:ro"]}, "/ws"),
            ["/ws:/code:ro"],
        )

    def test_mount_specs_target_overlapping_the_socket_in_use_raises(self):
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

    def test_mount_specs_tmux_socket_binds_the_directory_read_only(self):
        for mount in ("tmux-socket", "tmux-socket:ro"):
            self.assertEqual(
                tongs.tong_mount_specs(
                    {"mounts": [mount]}, "/ws", tmux_socket_dir="/tmp/tmux-1000"),
                ["/tmp/tmux-1000:/run/swarmforge-tmux:ro"],
                mount,
            )

    def test_mount_specs_tmux_socket_rw_raises(self):
        with self.assertRaisesRegex(ValueError, "always read-only"):
            tongs.tong_mount_specs(
                {"mounts": ["tmux-socket:rw"]}, "/ws", tmux_socket_dir="/tmp/tmux-1000")

    def test_mount_specs_tmux_socket_target_raises(self):
        with self.assertRaisesRegex(ValueError, "only the 'workspace' mount takes a target"):
            tongs.tong_mount_specs(
                {"mounts": ["tmux-socket:/tmux"]}, "/ws", tmux_socket_dir="/tmp/tmux-1000")

    def test_mount_specs_tmux_socket_without_host_source_raises(self):
        with self.assertRaisesRegex(ValueError, "no tmux socket directory is known"):
            tongs.tong_mount_specs({"mounts": ["tmux-socket"]}, "/ws")

    def test_mount_specs_workspace_overlapping_the_tmux_directory_raises(self):
        for target in ("/run", "/run/swarmforge-tmux", "/run/swarmforge-tmux/sub"):
            with self.assertRaisesRegex(ValueError, "overlaps /run/swarmforge-tmux"):
                tongs.tong_mount_specs(
                    {"mounts": ["workspace:" + target, "tmux-socket"]}, "/ws",
                    tmux_socket_dir="/tmp/tmux-1000",
                )

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


class TmuxSocketTests(unittest.TestCase):
    """Resolving the launcher's `$TMUX` into the directory to bind and the tong's `TMUX`."""

    def test_resolves_the_directory_and_rewrites_the_value(self):
        self.assertEqual(
            tongs.resolve_tmux_socket("/tmp/tmux-1000/default,4242,3"),
            ("/tmp/tmux-1000", "/tmp/tmux-1000/default",
             "/run/swarmforge-tmux/default,4242,3"),
        )

    def test_honors_a_tmux_tmpdir_and_a_named_server(self):
        self.assertEqual(
            tongs.resolve_tmux_socket("/run/user/1000/tmux-1000/work,1,0"),
            ("/run/user/1000/tmux-1000", "/run/user/1000/tmux-1000/work",
             "/run/swarmforge-tmux/work,1,0"),
        )

    def test_relative_or_unnormalized_path_refused(self):
        for value in ("tmux-1000/default,1,0", "/tmp/tmux-1000/../tmux-1000/default,1,0",
                      "/tmp/tmux-1000/,1,0", "//tmux-1000/default,1,0"):
            with self.assertRaisesRegex(ValueError, "normalized absolute path"):
                tongs.resolve_tmux_socket(value)

    def test_wrong_field_count_refused(self):
        for value in ("", "/tmp/tmux-1000/default", "/tmp/tmux-1000/default,1"):
            with self.assertRaisesRegex(ValueError, "<socket-path>,<server-pid>,<session-id>"):
                tongs.resolve_tmux_socket(value)
        with self.assertRaises(ValueError):
            tongs.resolve_tmux_socket(None)

    def test_extra_field_refused(self):
        # Split from the right, an extra field ends up in the path as a comma.
        with self.assertRaisesRegex(ValueError, "contains"):
            tongs.resolve_tmux_socket("/tmp/tmux-1000/default,9,1,0")

    def test_non_decimal_pid_or_session_refused(self):
        for value in ("/tmp/tmux-1000/default,abc,0", "/tmp/tmux-1000/default,1,$0",
                      "/tmp/tmux-1000/default,,0", "/tmp/tmux-1000/default,1, 0"):
            with self.assertRaisesRegex(ValueError, "decimal"):
                tongs.resolve_tmux_socket(value)

    def test_colon_or_whitespace_in_path_refused(self):
        for value in ("/tmp/tmux-1000/a:b,1,0", "/tmp/tmux-1000/a b,1,0",
                      "/tmp/tmux-1000/a\tb,1,0"):
            with self.assertRaisesRegex(ValueError, "contains"):
                tongs.resolve_tmux_socket(value)

    def test_socket_outside_tmuxs_own_directory_refused(self):
        # Binding a -S socket's parent could mount an arbitrary host directory.
        for value in ("/home/me/my.sock,1,0", "/tmp/default,1,0", "/default,1,0",
                      "/tmp/tmux-me/default,1,0", "/tmp/tmux-1000/sub/default,1,0"):
            with self.assertRaisesRegex(ValueError, "default per-user socket directory"):
                tongs.resolve_tmux_socket(value)

    def test_pane_id_checked(self):
        self.assertEqual(tongs.check_tmux_pane("%12"), "%12")
        for pane in ("12", "%", "%1a", " %1", "%1\n", None):
            with self.assertRaisesRegex(ValueError, "pane id"):
                tongs.check_tmux_pane(pane)


if __name__ == "__main__":
    unittest.main(verbosity=2)
