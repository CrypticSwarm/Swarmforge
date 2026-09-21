#!/usr/bin/env python3
"""Tests for the docker-name tokens derived from a host directory.

Run: python3 tests/test_names.py

Two properties carry the module: a token is a function of the directory, or a
user cannot stop the session they started, and it is injective over
directories, or `docker rm -f` takes the wrong session down.
"""

import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)

# Standing in for the launcher's entry-point shim keeps this file runnable on its own.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from swarmforge import names


class SanitizeToken(unittest.TestCase):
    """What comes out is a name docker will accept."""

    def test_runs_of_rejected_characters_become_one_dash(self):
        self.assertEqual(names.sanitize_token("my project/v2"), "my-project-v2")

    def test_the_characters_docker_allows_are_kept(self):
        self.assertEqual(names.sanitize_token("a_b.c-1"), "a_b.c-1")

    def test_leading_and_trailing_separators_are_trimmed(self):
        # docker refuses a name that starts with a separator.
        self.assertEqual(names.sanitize_token("/opt/x/"), "opt-x")

    def test_a_name_of_nothing_but_separators_sanitizes_away(self):
        self.assertEqual(names.sanitize_token("///"), "")


class CanonicalPath(unittest.TestCase):
    """Spellings of one directory collapse before the digest is taken."""

    def test_trailing_slash_and_dot_segments_are_resolved(self):
        base = names.canonical_path("/home/me/repo/master")
        self.assertEqual(names.canonical_path("/home/me/repo/master/"), base)
        self.assertEqual(names.canonical_path("/home/me/repo/./master"), base)
        self.assertEqual(
            names.canonical_path("/home/me/repo/other/../master"), base)

    def test_a_relative_path_resolves_against_the_working_directory(self):
        self.assertEqual(
            names.canonical_path("."), os.path.normpath(os.getcwd()))


class PathDigest(unittest.TestCase):
    """The unique half of a token, and the thing a user must be able to repeat."""

    def test_the_digest_is_the_documented_prefix_of_the_path_sha256(self):
        # Computed here, so changing how the name is derived has to change this line too.
        path = "/home/me/repo1/master"
        expected = hashlib.sha256(path.encode("utf-8")).hexdigest()[:8]
        self.assertEqual(names.path_digest(path), expected)

    def test_the_digest_is_hex_of_the_declared_length(self):
        digest = names.path_digest("/home/me/repo1/master")
        self.assertEqual(len(digest), names.DIGEST_LENGTH)
        self.assertTrue(all(c in "0123456789abcdef" for c in digest))

    def test_the_same_directory_digests_the_same_every_call(self):
        self.assertEqual(
            names.path_digest("/home/me/repo1/master"),
            names.path_digest("/home/me/repo1/./master/"))

    def test_directories_sharing_a_basename_digest_differently(self):
        self.assertNotEqual(
            names.path_digest("/home/me/repo1/master"),
            names.path_digest("/home/me/repo2/master"))


class PathToken(unittest.TestCase):
    """The readable hint and the digest, joined."""

    def test_the_token_reads_as_the_directory_it_names(self):
        self.assertEqual(
            names.path_token("/home/me/repo1/master"),
            "master-%s" % names.path_digest("/home/me/repo1/master"))

    def test_two_worktrees_of_the_same_branch_name_do_not_collide(self):
        first = names.path_token("/home/me/repo1/master")
        second = names.path_token("/home/me/repo2/master")
        self.assertNotEqual(first, second)
        # ...and both still say which branch they are, which is what the hint is for.
        self.assertTrue(first.startswith("master-"))
        self.assertTrue(second.startswith("master-"))

    def test_a_hint_directory_may_be_named_separately_from_the_hashed_one(self):
        token = names.path_token(
            "/home/me/orgs/acme/.swarmforge/tongs", hint_from="/home/me/orgs/acme")
        self.assertEqual(
            token,
            "acme-%s" % names.path_digest("/home/me/orgs/acme/.swarmforge/tongs"))

    def test_a_hint_that_sanitizes_away_leaves_the_digest_alone(self):
        # The root has no basename, and a digest is a valid name on its own.
        token = names.path_token("/")
        self.assertEqual(token, names.path_digest("/"))

    def test_a_hint_is_sanitized_into_a_name_docker_accepts(self):
        token = names.path_token("/home/me/my project")
        self.assertEqual(
            token, "my-project-%s" % names.path_digest("/home/me/my project"))


class ProjectToken(unittest.TestCase):
    """The token the Makefile names a session's container with."""

    def setUp(self):
        # realpath: a symlinked TMPDIR (the macOS default) would look resolved when it is not.
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="swarmforge-names-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_a_symlink_and_its_target_are_one_session(self):
        # The recipe mounts the resolved worktree root, so both spellings are one workspace.
        target = os.path.join(self.tmp, "real", "master")
        os.makedirs(target)
        os.symlink(os.path.join(self.tmp, "real"), os.path.join(self.tmp, "link"))
        through_link = os.path.join(self.tmp, "link", "master")
        self.assertNotEqual(through_link, target)
        self.assertEqual(
            names.project_token(through_link), names.project_token(target))

    def test_two_real_directories_sharing_a_basename_stay_distinct(self):
        first = os.path.join(self.tmp, "repo1", "master")
        second = os.path.join(self.tmp, "repo2", "master")
        os.makedirs(first)
        os.makedirs(second)
        self.assertNotEqual(
            names.project_token(first), names.project_token(second))

    def test_a_long_directory_name_is_cut_to_the_hint_limit(self):
        # Nothing downstream truncates, and a session tong's name becomes a DNS label.
        branch = "feature-really-long-branch-name-that-people-do-write"
        token = names.project_token(os.path.join(self.tmp, branch))
        hint, _, digest = token.rpartition("-")
        self.assertEqual(hint, branch[:names.PROJECT_HINT_LIMIT])
        self.assertEqual(len(digest), names.DIGEST_LENGTH)
        longest = "opencode-%s-tong-github" % token
        self.assertLessEqual(len(longest), 63, longest)

    def test_the_cut_leaves_room_for_the_names_built_on_the_container(self):
        """The budget the limit is picked against, as arithmetic not prose."""
        container = "opencode-%s-%s" % (
            "x" * names.PROJECT_HINT_LIMIT, "0" * names.DIGEST_LENGTH)
        self.assertLessEqual(
            len("%s-tong-%s" % (container, "x" * 15)), 63, container)

    def test_a_cut_hint_never_ends_on_a_separator(self):
        # docker would take `name--digest`, but a cosmetic cut should not read as a typo.
        branch = "release-" + "x" * (names.PROJECT_HINT_LIMIT - 9) + "-tail"
        self.assertEqual(branch[names.PROJECT_HINT_LIMIT - 1], "-")
        token = names.project_token(os.path.join(self.tmp, branch))
        self.assertNotIn("--", token)

    def test_a_path_that_is_not_valid_utf_8_still_names_a_container(self):
        # make dies on an empty token, so a latin-1 filename must not raise.
        raw = os.path.join(os.fsencode(self.tmp), b"caf\xe9")
        os.mkdir(raw)
        token = names.project_token(os.fsdecode(raw))
        self.assertTrue(token.startswith("caf"))
        self.assertEqual(len(token.rpartition("-")[2]), names.DIGEST_LENGTH)


class Command(unittest.TestCase):
    """`bin/project-name`, which the Makefile calls while reading variables."""

    class Out:
        def __init__(self):
            self.text = ""

        def write(self, text):
            self.text += text

    def run_main(self, argv):
        out, err = self.Out(), self.Out()
        stdout, stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            code = names.main(argv)
        finally:
            sys.stdout, sys.stderr = stdout, stderr
        return code, out.text, err.text

    def setUp(self):
        # patch.dict restores the mapping wholesale, leaving nothing behind for the next test.
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(names.PROJECT_DIR_ENV, None)

    def test_it_prints_the_token_for_the_directory_it_is_given(self):
        code, out, err = self.run_main(["/home/me/repo1/master"])
        self.assertEqual(code, 0)
        self.assertEqual(
            out, "%s\n" % names.project_token("/home/me/repo1/master"))
        self.assertEqual(err, "")

    def test_it_reads_the_directory_make_exports_when_given_none(self):
        # make exports it rather than writing a path a shell would reparse into `$(shell ...)`.
        os.environ[names.PROJECT_DIR_ENV] = "/home/me/repo1/master"
        code, out, err = self.run_main([])
        self.assertEqual(code, 0)
        self.assertEqual(
            out, "%s\n" % names.project_token("/home/me/repo1/master"))
        self.assertEqual(err, "")

    def test_an_argument_wins_over_the_environment(self):
        os.environ[names.PROJECT_DIR_ENV] = "/home/me/repo1/master"
        code, out, _ = self.run_main(["/home/me/repo2/master"])
        self.assertEqual(code, 0)
        self.assertEqual(
            out, "%s\n" % names.project_token("/home/me/repo2/master"))

    def test_naming_no_directory_is_a_usage_error(self):
        # A quiet default would bind a name for somewhere else and only show it in `docker ps`.
        os.environ[names.PROJECT_DIR_ENV] = ""
        for argv in ([], [""], ["   "], ["a", "b"]):
            code, out, err = self.run_main(argv)
            self.assertEqual(code, 2, argv)
            self.assertEqual(out, "", argv)
            self.assertIn("usage", err, argv)


if __name__ == "__main__":
    unittest.main(verbosity=2)
