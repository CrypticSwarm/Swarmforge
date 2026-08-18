#!/usr/bin/env python3
"""Tests for merging and building layered JSON config files.

Run: python3 tests/test_merge_json.py

Two shapes share the deep merge. ``merge_files`` folds one layer into a
destination that is also an input; ``build_file`` treats the destination as an
output only and derives it from an ordered list of layers. The second is what
keeps a generated config from accumulating whatever earlier runs left in it,
so most of what is asserted below is about what does *not* survive a build.
"""

import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The image puts the swarmforge package on PYTHONPATH; standing in for that
# here keeps this file runnable on its own, not just under a discovery run
# that already set it.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from swarmforge.config import merge_json


def _write(path, value):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle)


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


class MergeFilesTests(unittest.TestCase):
    def test_normal_layers_deep_merge_mcp_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            dst = os.path.join(tmp, "dst.json")
            src = os.path.join(tmp, "src.json")
            _write(dst, {
                "mcp": {
                    "github": {
                        "type": "local",
                        "command": ["gh", "mcp"],
                        "enabled": True,
                    },
                },
                "permission": {"bash": {"*": "ask"}},
            })
            _write(src, {
                "mcp": {"github": {"enabled": False}},
                "permission": {"bash": {"git *": "allow"}},
            })

            merge_json.merge_files(dst, src)

            self.assertEqual(_read(dst), {
                "mcp": {
                    "github": {
                        "type": "local",
                        "command": ["gh", "mcp"],
                        "enabled": False,
                    },
                },
                "permission": {"bash": {"*": "ask", "git *": "allow"}},
            })

    def test_tong_mcp_merge_replaces_whole_server_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            dst = os.path.join(tmp, "dst.json")
            src = os.path.join(tmp, "src.json")
            _write(dst, {
                "mcp": {
                    "github": {
                        "type": "local",
                        "command": ["gh", "mcp"],
                        "cwd": "/workspace",
                        "enabled": False,
                    },
                    "filesystem": {"type": "local", "command": ["fs"]},
                },
                "permission": {"bash": {"*": "ask"}},
            })
            _write(src, {
                "mcp": {
                    "github": {
                        "type": "remote",
                        "url": "http://github:8080/mcp",
                        "enabled": True,
                    },
                },
                "permission": {"bash": {"git *": "allow"}},
            })

            merge_json.merge_files(dst, src, replace_mcp_entries=True)

            self.assertEqual(_read(dst), {
                "mcp": {
                    "github": {
                        "type": "remote",
                        "url": "http://github:8080/mcp",
                        "enabled": True,
                    },
                    "filesystem": {"type": "local", "command": ["fs"]},
                },
                "permission": {"bash": {"*": "ask", "git *": "allow"}},
            })


class BuildFileCase(unittest.TestCase):
    """A destination built from named layer files in a throwaway directory."""

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="swarmforge-build-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.tmp = tmp
        self.dst = os.path.join(tmp, "settings.json")
        self.err = io.StringIO()

    def layer(self, name, value):
        """A layer file holding `value`, returned as its path."""
        path = os.path.join(self.tmp, name)
        if isinstance(value, str):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(value)
        else:
            _write(path, value)
        return path

    def build(self, *src_paths):
        return merge_json.build_file(self.dst, src_paths, err=self.err)

    def warnings(self):
        return self.err.getvalue()


class BuildFileTests(BuildFileCase):
    def test_later_layers_win_key_by_key(self):
        low = self.layer("low.json", {"model": "sonnet", "theme": "dark"})
        high = self.layer("high.json", {"theme": "light"})

        self.build(low, high)

        self.assertEqual(_read(self.dst), {"model": "sonnet", "theme": "light"})

    def test_nested_objects_merge_rather_than_replace(self):
        low = self.layer("low.json", {"env": {"A": "1", "B": "2"}})
        high = self.layer("high.json", {"env": {"B": "9"}})

        self.build(low, high)

        self.assertEqual(_read(self.dst), {"env": {"A": "1", "B": "9"}})

    def test_the_destination_is_not_one_of_the_layers(self):
        """A key an earlier run wrote, and no layer sets, does not survive.

        This is the whole point of building rather than merging: the
        destination outlives the layers that produced it, so anything read
        back out of it would make one run's config an input to the next.
        """
        _write(self.dst, {"stale": True, "model": "opus"})
        layer = self.layer("layer.json", {"model": "sonnet"})

        self.build(layer)

        self.assertEqual(_read(self.dst), {"model": "sonnet"})

    def test_no_layers_at_all_leaves_an_empty_object(self):
        _write(self.dst, {"stale": True})

        self.build()

        self.assertEqual(_read(self.dst), {})

    def test_the_destination_is_rewritten_in_place(self):
        """The destination is a bind-mounted file in the container.

        Building a temporary beside it and renaming over the path would
        detach the mount at best and fail outright at worst, and nothing in
        the result would show which way it was written -- so the inode is
        what gets asserted.
        """
        _write(self.dst, {})
        before = os.stat(self.dst).st_ino
        layer = self.layer("layer.json", {"model": "sonnet"})

        self.build(layer)

        self.assertEqual(os.stat(self.dst).st_ino, before)

    def test_a_layer_that_ships_no_file_contributes_nothing_quietly(self):
        """Most layers ship no file, and callers pass a path regardless."""
        present = self.layer("present.json", {"model": "sonnet"})
        absent = os.path.join(self.tmp, "nope", "settings.json")

        self.build(absent, present)

        self.assertEqual(_read(self.dst), {"model": "sonnet"})
        self.assertEqual(self.warnings(), "")

    def test_a_layer_that_is_not_json_is_reported_and_dropped(self):
        broken = self.layer("broken.json", "{ not json")
        good = self.layer("good.json", {"model": "sonnet"})

        self.build(broken, good)

        self.assertEqual(_read(self.dst), {"model": "sonnet"})
        self.assertIn(broken, self.warnings())

    def test_a_layer_that_is_not_an_object_is_reported_and_dropped(self):
        listy = self.layer("listy.json", [1, 2])
        good = self.layer("good.json", {"model": "sonnet"})

        self.build(listy, good)

        self.assertEqual(_read(self.dst), {"model": "sonnet"})
        self.assertIn("not a JSON object", self.warnings())

    def test_a_layer_that_cannot_be_read_is_reported_and_dropped(self):
        unreadable = self.layer("unreadable.json", {"model": "opus"})
        os.chmod(unreadable, 0)
        self.addCleanup(os.chmod, unreadable, stat.S_IRUSR | stat.S_IWUSR)
        if os.access(unreadable, os.R_OK):
            self.skipTest("cannot make a file unreadable as this user")
        good = self.layer("good.json", {"model": "sonnet"})

        self.build(unreadable, good)

        self.assertEqual(_read(self.dst), {"model": "sonnet"})
        self.assertIn(unreadable, self.warnings())

    def test_a_broken_top_layer_does_not_empty_the_destination(self):
        """The failure the run has to survive, stated on its own.

        A layer is read at the point its keys would win, so dropping one has
        to leave every other layer's contribution standing rather than
        abandoning the build partway through.
        """
        good = self.layer("good.json", {"model": "sonnet"})
        broken = self.layer("broken.json", "]]")

        self.build(good, broken)

        self.assertEqual(_read(self.dst), {"model": "sonnet"})


class BuildCommandLineTests(BuildFileCase):
    """The argv the container entrypoint hands the module."""

    def test_build_writes_the_destination_from_the_layers_that_follow(self):
        low = self.layer("low.json", {"model": "sonnet", "theme": "dark"})
        high = self.layer("high.json", {"theme": "light"})

        self.assertEqual(
            merge_json.main(["--build", self.dst, low, high], err=self.err), 0)

        self.assertEqual(_read(self.dst), {"model": "sonnet", "theme": "light"})

    def test_build_with_no_layers_still_writes_a_destination(self):
        self.assertEqual(merge_json.main(["--build", self.dst], err=self.err), 0)

        self.assertEqual(_read(self.dst), {})

    def test_build_without_a_destination_is_a_usage_error(self):
        self.assertEqual(merge_json.main(["--build"], err=self.err), 2)

    def test_a_misspelled_flag_is_rejected_rather_than_read_as_a_layer(self):
        """Layer paths are skipped when absent, and flags are not paths.

        Without this the two rules overlap: a mistyped option looks like a
        layer that happens not to exist, and is dropped as quietly as a real
        one -- leaving the caller with a destination built from less than it
        asked for and nothing on stderr.
        """
        layer = self.layer("layer.json", {"model": "sonnet"})
        self.assertEqual(
            merge_json.main(
                ["--build", self.dst, layer, "--replace-mcp-entries"],
                err=self.err),
            2,
        )
        self.assertFalse(os.path.exists(self.dst), "destination was written")

    def test_a_bare_merge_still_takes_two_paths(self):
        """The other mode keeps its argv: the entrypoint still merges a
        config file into a destination one layer at a time through it."""
        _write(self.dst, {"model": "opus"})
        src = self.layer("src.json", {"theme": "light"})

        self.assertEqual(merge_json.main([self.dst, src], err=self.err), 0)

        self.assertEqual(_read(self.dst), {"model": "opus", "theme": "light"})

    def test_wrong_argument_count_is_a_usage_error(self):
        self.assertEqual(merge_json.main([self.dst], err=self.err), 2)

    def test_an_unknown_third_argument_is_a_usage_error(self):
        _write(self.dst, {})
        src = self.layer("src.json", {})
        self.assertEqual(
            merge_json.main([self.dst, src, "--nope"], err=self.err), 2)


if __name__ == "__main__":
    unittest.main()
