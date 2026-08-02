#!/usr/bin/env python3
"""Unit tests for scripts/git_guard.py. Run: python3 scripts/test_git_guard.py"""

import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE_PATH = os.path.join(HERE, "git_guard.py")
spec = importlib.util.spec_from_file_location("git_guard", MODULE_PATH)
git_guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(git_guard)


# git runs with the developer's global config otherwise, where a signing key or
# a templateDir would change what these repos come out looking like.
GIT_ENV = dict(
    os.environ,
    GIT_CONFIG_GLOBAL="/dev/null",
    GIT_CONFIG_SYSTEM="/dev/null",
    GIT_AUTHOR_NAME="Test",
    GIT_AUTHOR_EMAIL="test@example.com",
    GIT_COMMITTER_NAME="Test",
    GIT_COMMITTER_EMAIL="test@example.com",
)


def git(cwd, *args):
    subprocess.run(["git", "-C", cwd] + list(args), check=True,
                   capture_output=True, text=True, env=GIT_ENV)


class GuardCase(unittest.TestCase):
    def setUp(self):
        # realpath: git reports resolved paths, and the guard compares them.
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="git-guard-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def repo(self, name="repo", bare=False):
        path = os.path.join(self.tmp, name)
        os.makedirs(path)
        git(path, "init", "-q", *(["--bare"] if bare else []))
        if not bare:
            git(path, "commit", "-q", "--allow-empty", "-m", "root")
        return path

    def mounts(self, workspace, targets=("/workspace",)):
        return git_guard.build_mounts(workspace, list(targets))


class PlainCheckout(GuardCase):
    def test_config_hooks_and_commondir_are_read_only(self):
        repo = self.repo()
        mounts = self.mounts(repo)
        for name in ("config", "hooks", "commondir"):
            self.assertIn(
                "%s/.git/%s:/workspace/.git/%s:ro" % (repo, name, name),
                mounts,
            )

    def test_git_dir_is_anchored_by_a_mount_of_its_own(self):
        # Without this the read-only mounts are sidestepped by renaming the
        # git dir aside and copying it back; renaming a mount point fails.
        repo = self.repo()
        self.assertIn("%s/.git:/workspace/.git" % repo, self.mounts(repo))

    def test_every_target_gets_its_own_copy_of_the_guard(self):
        # A read-only mount covers one path; the same host file reached through
        # a second mount of the workspace is still writable without its own.
        repo = self.repo()
        mounts = self.mounts(repo, ("/workspace", "/repos/me/repo"))
        for target in ("/workspace", "/repos/me/repo"):
            self.assertIn(
                "%s/.git/config:%s/.git/config:ro" % (repo, target), mounts)

    def test_missing_commondir_is_created_rather_than_left_writable(self):
        # git reads commondir in every repository, so a writable one relocates
        # config and hooks and defeats the mounts above. `.` names the git dir
        # holding it, which is where git looks by default.
        repo = self.repo()
        commondir = os.path.join(repo, ".git", "commondir")
        self.assertFalse(os.path.exists(commondir))
        self.mounts(repo)
        with open(commondir) as handle:
            self.assertEqual(handle.read().strip(), ".")
        # The repo still behaves: git resolves the pointer back to the git dir.
        git(repo, "commit", "-q", "--allow-empty", "-m", "after")
        self.assertEqual(
            git_guard.common_git_dir(repo), os.path.join(repo, ".git"))

    def test_missing_hooks_directory_is_created_rather_than_skipped(self):
        repo = self.repo()
        hooks = os.path.join(repo, ".git", "hooks")
        shutil.rmtree(hooks)
        mounts = self.mounts(repo)
        self.assertTrue(os.path.isdir(hooks))
        self.assertIn("%s:/workspace/.git/hooks:ro" % hooks, mounts)

    def test_config_is_never_invented(self):
        # A file the repo does not have is not a file to fabricate; only the
        # pointers whose absence is itself the hole get created.
        repo = self.repo()
        os.remove(os.path.join(repo, ".git", "config"))
        self.assertEqual(
            [m for m in self.mounts(repo) if m.endswith("/config:ro")], [])

    def test_unwritable_git_dir_warns_instead_of_failing_the_launch(self):
        repo = self.repo()
        git_dir = os.path.join(repo, ".git")
        os.chmod(git_dir, 0o500)
        self.addCleanup(os.chmod, git_dir, 0o700)
        warnings = []
        mounts = git_guard.build_mounts(repo, ["/workspace"], warn=warnings.append)
        self.assertTrue(any("commondir" in text for text in warnings), warnings)
        self.assertNotIn(
            "%s/commondir:/workspace/.git/commondir:ro" % git_dir, mounts)
        # The rest of the guard still applies.
        self.assertIn("%s/config:/workspace/.git/config:ro" % git_dir, mounts)

    def test_non_git_directory_gets_nothing(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        self.assertEqual(self.mounts(plain), [])

    def test_repeated_runs_are_stable(self):
        repo = self.repo()
        self.assertEqual(self.mounts(repo), self.mounts(repo))


class BareRepo(GuardCase):
    def test_guarded_paths_sit_directly_under_the_workspace_mount(self):
        # A bare repo is its own git dir, and the workspace mount is already a
        # mount point, so the read-only paths hang off the target itself.
        repo = self.repo("bare.git", bare=True)
        mounts = self.mounts(repo)
        self.assertIn("%s/config:/workspace/config:ro" % repo, mounts)
        self.assertIn("%s/hooks:/workspace/hooks:ro" % repo, mounts)
        self.assertNotIn("%s:/workspace" % repo, mounts)


class LinkedWorktree(GuardCase):
    def setUp(self):
        super().setUp()
        self.repo_path = self.repo()
        self.worktree = os.path.join(self.tmp, "wt")
        git(self.repo_path, "worktree", "add", "-q", "-b", "topic",
            self.worktree)
        self.common = os.path.join(self.repo_path, ".git")

    def test_common_dir_is_guarded_at_its_own_host_path(self):
        mounts = self.mounts(self.worktree)
        self.assertIn("%s:%s" % (self.common, self.common), mounts)
        self.assertIn(
            "%s/config:%s/config:ro" % (self.common, self.common), mounts)
        self.assertIn(
            "%s/hooks:%s/hooks:ro" % (self.common, self.common), mounts)

    def test_gitdir_pointer_file_is_read_only(self):
        # Rewriting it would send git to a git dir none of these mounts cover.
        mounts = self.mounts(self.worktree)
        self.assertIn("%s/.git:/workspace/.git:ro" % self.worktree, mounts)

    def test_per_worktree_commondir_is_read_only(self):
        # It names where config and hooks live for that worktree.
        for workspace in (self.worktree, self.repo_path):
            mounts = self.mounts(workspace)
            self.assertTrue(
                any("/worktrees/wt/commondir" in spec and spec.endswith(":ro")
                    for spec in mounts),
                mounts,
            )

    def test_per_worktree_commondir_is_never_rewritten(self):
        # Unlike a repository's, its contents are a real relative path.
        pointer = os.path.join(self.common, "worktrees", "wt", "commondir")
        with open(pointer) as handle:
            before = handle.read()
        self.mounts(self.worktree)
        with open(pointer) as handle:
            self.assertEqual(handle.read(), before)


class Submodules(GuardCase):
    def setUp(self):
        super().setUp()
        self.inner = self.repo("inner")
        self.super_repo = self.repo("super")
        git(self.super_repo, "-c", "protocol.file.allow=always", "submodule",
            "add", "-q", self.inner, "libs/nested")
        git(self.super_repo, "commit", "-q", "-m", "add submodule")
        self.module_dir = os.path.join(
            self.super_repo, ".git", "modules", "libs", "nested")

    def test_submodule_git_dir_is_guarded(self):
        # It has its own config and hooks, which the host runs whenever the
        # user works in the submodule.
        mounts = self.mounts(self.super_repo)
        for name in ("config", "hooks", "commondir"):
            self.assertIn(
                "%s/%s:/workspace/.git/modules/libs/nested/%s:ro"
                % (self.module_dir, name, name),
                mounts,
            )

    def test_submodule_checkout_pointer_is_read_only(self):
        # Guarding the submodule's git dir is moot if the pointer naming it
        # can be repointed somewhere unguarded.
        mounts = self.mounts(self.super_repo)
        self.assertIn(
            "%s/libs/nested/.git:/workspace/libs/nested/.git:ro"
            % self.super_repo,
            mounts,
        )

    def test_submodule_guard_reaches_every_target(self):
        mounts = self.mounts(self.super_repo, ("/workspace", "/repos/x"))
        self.assertIn(
            "%s/config:/repos/x/.git/modules/libs/nested/config:ro"
            % self.module_dir, mounts)


class WorktreeConfig(GuardCase):
    def test_guarded_only_where_the_extension_is_enabled(self):
        repo = self.repo()
        self.assertEqual(
            [m for m in self.mounts(repo) if "config.worktree" in m], [])
        git(repo, "config", "extensions.worktreeConfig", "true")
        self.assertIn(
            "%s/.git/config.worktree:/workspace/.git/config.worktree:ro" % repo,
            self.mounts(repo),
        )

    def test_enabled_extension_creates_the_file_it_guards(self):
        # git reads it when the extension is on, so an absent one is a hole.
        repo = self.repo()
        git(repo, "config", "extensions.worktreeConfig", "true")
        self.mounts(repo)
        self.assertTrue(
            os.path.isfile(os.path.join(repo, ".git", "config.worktree")))


class CommandLine(GuardCase):
    def _main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        code = git_guard.main(argv, out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def test_prints_one_mount_per_line(self):
        repo = self.repo()
        code, out, _ = self._main(["--workspace", repo, "--target", "/workspace"])
        self.assertEqual(code, 0)
        self.assertIn("%s/.git/config:/workspace/.git/config:ro" % repo,
                      out.splitlines())

    def test_trailing_slash_on_a_target_does_not_double_up(self):
        repo = self.repo()
        _, out, _ = self._main(["--workspace", repo, "--target", "/workspace/"])
        self.assertIn("%s/.git/config:/workspace/.git/config:ro" % repo,
                      out.splitlines())

    def test_missing_arguments_are_a_usage_error(self):
        for argv in ([], ["--workspace", "/x"], ["--target", "/workspace"],
                     ["--workspace"], ["--bogus"]):
            code, out, err = self._main(argv)
            self.assertEqual(code, 2, argv)
            self.assertEqual(out, "")
            self.assertTrue(err)


if __name__ == "__main__":
    if shutil.which("git") is None:
        sys.stderr.write("git is required for these tests\n")
        sys.exit(1)
    unittest.main(verbosity=2)
