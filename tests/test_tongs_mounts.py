#!/usr/bin/env python3
"""Unit tests for swarmforge.tongs.mounts. Run: python3 tests/test_tongs_mounts.py"""

import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Standing in for the launcher's entry-point shim keeps this file runnable on its own.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from swarmforge import tongs


LOCAL = (tongs.LOCAL_VOLUME_SCOPE, None)


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

    def test_parse_mount_keeps_a_volume_name_out_of_the_tuple(self):
        self.assertEqual(
            tongs.parse_mount("volume:models:/data"), ("volume", "/data", None)
        )
        self.assertEqual(
            tongs.parse_mount("volume:models:/data:ro"), ("volume", "/data", "ro")
        )
        self.assertEqual(tongs.mount_volume_name("volume:models:/data:ro"), "models")
        self.assertEqual(tongs.mount_volume_name("volume:v1.2-x:/data"), "v1.2-x")

    def test_mount_volume_name_is_none_for_other_words(self):
        self.assertIsNone(tongs.mount_volume_name("workspace:/code:ro"))
        self.assertIsNone(tongs.mount_volume_name("docker-socket"))

    def test_parse_mount_rejects_a_malformed_volume_name(self):
        # `_` is what splits the volume back off its docker name, so it cannot be in one.
        for mount in ("volume", "volume::/data", "volume:/data", "volume:my_models:/data",
                      "volume:-models:/data", "volume:.models:/data", "volume:mo dels:/data",
                      "volume:models\n:/data"):
            with self.assertRaisesRegex(ValueError, "volume:<name>:/target", msg=mount):
                tongs.parse_mount(mount)
            with self.assertRaises(ValueError, msg=mount):
                tongs.mount_volume_name(mount)

    def test_parse_mount_caps_a_volume_name(self):
        longest = "v" * tongs.VOLUME_NAME_MAX_LENGTH
        self.assertEqual(tongs.mount_volume_name("volume:%s:/data" % longest), longest)
        with self.assertRaisesRegex(ValueError, "at most 64 characters"):
            tongs.parse_mount("volume:%s:/data" % (longest + "v"))

    def test_parse_mount_requires_a_volume_target(self):
        for mount in ("volume:models", "volume:models:ro"):
            with self.assertRaisesRegex(ValueError, "needs an absolute target path", msg=mount):
                tongs.parse_mount(mount)

    def test_parse_mount_judges_a_volume_target_like_a_workspace_one(self):
        for mount, message in (
            ("volume:models:data", "neither an absolute target path"),
            ("volume:models:/", "not a usable target path"),
            ("volume:models:/a:/b", "more than one target path"),
            ("volume:models:ro:/data", "must be the last field"),
            ("volume:models:/da ta", "whitespace"),
        ):
            with self.assertRaisesRegex(ValueError, message, msg=mount):
                tongs.parse_mount(mount)

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

    def test_mount_destination_defaults_per_word(self):
        self.assertEqual(tongs.mount_destination("workspace", None), "/workspace")
        self.assertEqual(tongs.mount_destination("workspace", "//code/"), "/code")
        self.assertEqual(
            tongs.mount_destination("docker-socket", None), tongs.DEFAULT_DOCKER_SOCKET
        )
        self.assertEqual(
            tongs.mount_destination("docker-socket", None, "/run/d.sock"), "/run/d.sock"
        )
        self.assertEqual(tongs.mount_destination("volume", "//data/"), "/data")

    def test_mount_destination_refuses_a_volume_without_a_target(self):
        with self.assertRaisesRegex(ValueError, "no destination"):
            tongs.mount_destination("volume", None)

    def test_mount_target_error_holds_a_volume_to_the_reserved_paths(self):
        reserved = {"/run/x": "where the launcher delivers this tong's secrets"}
        self.assertIsNone(tongs.mount_target_error(
            "volume:v:/data", "volume", "/data", "/data", reserved))
        self.assertIn("overlaps /run/x", tongs.mount_target_error(
            "volume:v:/run", "volume", "/run", "/run", reserved))

    def test_tong_volume_name_is_a_hint_then_a_fixed_width_digest(self):
        name = tongs.tong_volume_name("my tong_x", "models", LOCAL)
        self.assertRegex(name, r"\Aswarmforge-volume-my-tong_x-[0-9a-f]{32}_models\Z")
        self.assertEqual(tongs.VOLUME_DIGEST_LENGTH, 32)
        self.assertEqual(name.rsplit("_", 1)[1], "models")
        self.assertEqual(name, tongs.tong_volume_name("my tong_x", "models", LOCAL))
        # A name that sanitizes to nothing keeps the digest alone.
        self.assertRegex(
            tongs.tong_volume_name("__", "v", LOCAL), r"\Aswarmforge-volume-[0-9a-f]{32}_v\Z"
        )

    def test_tong_volume_name_is_pinned(self):
        # Literal on purpose: any change to the formula orphans every existing volume.
        cases = (
            ("ollama", LOCAL,
             "swarmforge-volume-ollama-fbdbeb6d19743ef7c7724f97755c49d1_models"),
            ("ollama", (tongs.ORG_VOLUME_SCOPE, "/orgs/acme/.swarmforge/tongs"),
             "swarmforge-volume-ollama-a0afff5cc924fc93f5f25e86a52f5888_models"),
            ("ollama", (tongs.WORKSPACE_VOLUME_SCOPE, "/home/me/proj"),
             "swarmforge-volume-ollama-9d5322a3cc671ff97654fc52727d1dd3_models"),
            ("caf\u00e9", LOCAL,
             "swarmforge-volume-caf-a3195456dbda485c7f802622dae166e0_models"),
            # The hint is cut to 32 and loses the separator it would end on.
            ("a" * 31 + "-" + "b" * 10, LOCAL,
             "swarmforge-volume-" + "a" * 31 + "-c9719a32926cd73d826a8ed165eb2523_models"),
        )
        for tong_name, scope, expected in cases:
            self.assertEqual(tongs.tong_volume_name(tong_name, "models", scope), expected)

    def test_tong_volume_name_digests_the_raw_name_not_the_hint(self):
        # Both sanitize to `github`; the hint is for reading, never for identity.
        dotted = tongs.tong_volume_name("github.", "v", LOCAL)
        plain = tongs.tong_volume_name("github", "v", LOCAL)
        self.assertTrue(dotted.startswith("swarmforge-volume-github-"))
        self.assertTrue(plain.startswith("swarmforge-volume-github-"))
        self.assertNotEqual(dotted, plain)

    def test_tong_volume_name_differs_per_scope(self):
        names = {
            tongs.tong_volume_name("cache", "v", scope)
            for scope in (LOCAL, (tongs.ORG_VOLUME_SCOPE, "/orgs/acme/.swarmforge/tongs"),
                          (tongs.ORG_VOLUME_SCOPE, "/orgs/globex/.swarmforge/tongs"),
                          (tongs.WORKSPACE_VOLUME_SCOPE, "/orgs/acme/.swarmforge/tongs"),
                          (tongs.WORKSPACE_VOLUME_SCOPE, "/home/me/proj"))
        }
        self.assertEqual(len(names), 5)

    def test_volume_scope_follows_the_layer(self):
        self.assertEqual(tongs.volume_scope(tongs.USER), LOCAL)
        self.assertEqual(tongs.volume_scope(tongs.REPO, "/o/tongs", "/ws"), LOCAL)
        self.assertEqual(
            tongs.volume_scope(tongs.ORG, org_tongs_dir="/orgs//acme/./tongs/"),
            (tongs.ORG_VOLUME_SCOPE, "/orgs/acme/tongs"),
        )
        with self.assertRaisesRegex(ValueError, "org tongs directory"):
            tongs.volume_scope(tongs.ORG, workspace="/ws")
        with self.assertRaisesRegex(ValueError, "workspace path"):
            tongs.volume_scope(tongs.WORKSPACE, org_tongs_dir="/o/tongs")
        with self.assertRaisesRegex(ValueError, "unknown layer"):
            tongs.volume_scope("elsewhere")

    def test_volume_scope_resolves_a_symlinked_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = os.path.join(tmp, "checkout")
            os.mkdir(checkout)
            link = os.path.join(tmp, "link")
            os.symlink(checkout, link)
            self.assertEqual(
                tongs.volume_scope(tongs.WORKSPACE, workspace=link),
                tongs.volume_scope(tongs.WORKSPACE, workspace=checkout),
            )
            self.assertEqual(
                tongs.volume_scope(tongs.WORKSPACE, workspace=link)[1],
                os.path.realpath(checkout),
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
            "only the 'workspace' and 'volume' mounts take a target path",
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
                tongs.overlapping_mount_error("workspace", destination, placed),
            )

    def test_overlapping_mount_error_refuses_one_volume_mounted_twice(self):
        placed = [("volume:models:/a", "/a")]
        self.assertIsNone(tongs.overlapping_mount_error("volume:cache:/b", "/b", placed))
        self.assertIn(
            "volume 'models' is already mounted by 'volume:models:/a'",
            tongs.overlapping_mount_error("volume:models:/b:ro", "/b", placed),
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
        self.assertIn("only the 'workspace' and 'volume' mounts take a target path", str(caught.exception))

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

    def test_mount_specs_volume_names_a_docker_volume(self):
        defn = {"mounts": ["volume:models:/root/.ollama", "volume:cache:/cache:ro"]}
        scope = (tongs.ORG_VOLUME_SCOPE, "/orgs/acme/.swarmforge/tongs")
        self.assertEqual(
            tongs.tong_mount_specs(defn, None, tong_name="ollama", volume_scope=scope),
            [tongs.tong_volume_name("ollama", "models", scope) + ":/root/.ollama",
             tongs.tong_volume_name("ollama", "cache", scope) + ":/cache:ro"],
        )

    def test_mount_specs_volume_target_normalized(self):
        self.assertEqual(
            tongs.tong_mount_specs(
                {"mounts": ["volume:v://data/"]}, None, tong_name="t", volume_scope=LOCAL),
            [tongs.tong_volume_name("t", "v", LOCAL) + ":/data"],
        )

    def test_mount_specs_volume_without_a_tong_name_or_scope_raises(self):
        # Either would let tongs that must stay apart land on one volume.
        for kwargs in ({"volume_scope": LOCAL}, {"tong_name": "t"}):
            with self.assertRaisesRegex(ValueError, "name and scope", msg=kwargs):
                tongs.tong_mount_specs({"mounts": ["volume:v:/data"]}, None, **kwargs)

    def test_mount_specs_volume_overlapping_another_mount_raises(self):
        for mounts in (["workspace", "volume:v:/workspace/cache"],
                       ["volume:a:/data", "volume:b:/data"]):
            with self.assertRaisesRegex(ValueError, "overlaps", msg=mounts):
                tongs.tong_mount_specs(
                    {"mounts": mounts}, "/ws", tong_name="t", volume_scope=LOCAL)

    def test_mount_specs_no_mounts_is_empty(self):
        self.assertEqual(tongs.tong_mount_specs({}, "/ws"), [])

    def test_workspace_mount_placements_empty_without_workspace_mount(self):
        self.assertEqual(tongs.workspace_mount_placements({}), [])
        self.assertEqual(
            tongs.workspace_mount_placements({"mounts": ["docker-socket"]}), []
        )
        self.assertEqual(
            tongs.workspace_mount_placements({"mounts": ["volume:v:/data"]}), []
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
