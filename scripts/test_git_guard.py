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
            git_guard.common_git_dir(repo, lambda m: None),
            os.path.join(repo, ".git"))

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

    def test_a_path_docker_cannot_express_is_reported_not_mangled(self):
        # Directories inside a git dir are the container's to name, and a `-v`
        # value is colon-separated and read one per line.
        repo = self.repo()
        planted = os.path.join(repo, ".git", "modules", "a\nb")
        os.makedirs(planted)
        with open(os.path.join(planted, "HEAD"), "w") as handle:
            handle.write("ref: refs/heads/main\n")
        warnings = []
        mounts = git_guard.build_mounts(repo, ["/workspace"],
                                        warn=warnings.append)
        self.assertEqual([m for m in mounts if "\n" in m], [])
        self.assertEqual([t for t in warnings if "colon or newline" in t],
                         ["%s cannot be expressed as a docker mount (it "
                          "contains a colon or newline); leaving it writable "
                          "in the container" % planted])
        self.assertFalse(os.path.exists(os.path.join(planted, "commondir")))
        self.assertFalse(os.path.exists(os.path.join(planted, "hooks")))

    def test_a_symlinked_git_dir_pointer_is_reported(self):
        # The resolved git dir is still guarded, but the symlink itself is not
        # something a mount can hold in place.
        repo = self.repo()
        moved = os.path.join(self.tmp, "elsewhere.git")
        shutil.move(os.path.join(repo, ".git"), moved)
        os.symlink(moved, os.path.join(repo, ".git"))
        warnings = []
        git_guard.build_mounts(repo, ["/workspace"], warn=warnings.append)
        self.assertTrue(any("symlink" in text for text in warnings), warnings)

    @unittest.skipIf(os.geteuid() == 0, "root ignores the permission bits")
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
        self.assertIn("%s/config:/workspace/.git/config:ro" % git_dir, mounts)

    def test_non_git_directory_gets_nothing(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        self.assertEqual(self.mounts(plain), [])

    def test_repeated_runs_are_stable(self):
        repo = self.repo()
        self.assertEqual(self.mounts(repo), self.mounts(repo))


class SeparateGitDir(GuardCase):
    def test_a_git_dir_inside_the_workspace_is_guarded_under_each_target(self):
        # `git init --separate-git-dir` puts the git dir somewhere else, which
        # may still be inside the workspace -- and then the container reaches
        # it through the workspace mount, not through its host path.
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(repo)
        git(repo, "init", "-q", "--separate-git-dir",
            os.path.join(repo, ".realgit"))
        mounts = self.mounts(repo)
        self.assertIn(
            "%s/.realgit/config:/workspace/.realgit/config:ro" % repo, mounts)
        self.assertIn("%s/.git:/workspace/.git:ro" % repo, mounts)


class Anchoring(GuardCase):
    """Every read-only path is reached only through mount points.

    A plain directory containing a mount point can be renamed aside and the
    path recreated writable, so a single unmounted directory anywhere on the
    way down undoes the guard below it. This checks the whole chain for each
    repo shape instead of naming paths one shape at a time.
    """

    TARGETS = ("/workspace", "/repos/me/proj")

    def assert_anchored(self, workspace):
        mounts = self.mounts(workspace, self.TARGETS)
        self.assertTrue(mounts, "no mounts emitted for %s" % workspace)
        destinations = {spec.split(":")[1] for spec in mounts}
        # Mount points that exist without the guard: the workspace mounts, and
        # a git dir the guard binds at its own host path.
        roots = set(self.TARGETS)
        for spec in mounts:
            source, destination = spec.split(":")[:2]
            if source == destination:
                roots.add(destination)
        for destination in sorted(destinations - roots):
            root = max((r for r in roots if destination.startswith(r + "/")),
                       key=len, default=None)
            self.assertIsNotNone(
                root, "%s hangs off no mount point" % destination)
            parts = os.path.relpath(destination, root).split(os.sep)
            for depth in range(1, len(parts)):
                ancestor = os.path.join(root, *parts[:depth])
                self.assertIn(
                    ancestor, destinations,
                    "%s is reached through %s, which is not a mount point"
                    % (destination, ancestor))

    def test_plain_checkout(self):
        self.assert_anchored(self.repo())

    def test_bare_repo(self):
        self.assert_anchored(self.repo("bare.git", bare=True))

    def test_submodules_including_a_slashed_name_and_nesting(self):
        leaf = self.repo("leaf")
        middle = self.repo("middle")
        git(middle, "-c", "protocol.file.allow=always", "submodule", "add",
            "-q", leaf, "deep/leaf")
        git(middle, "commit", "-q", "-m", "nest")
        top = self.repo("top")
        git(top, "-c", "protocol.file.allow=always", "submodule", "add", "-q",
            middle, "libs/middle")
        git(top, "-c", "protocol.file.allow=always", "submodule", "update",
            "--init", "--recursive", "-q")
        self.assert_anchored(top)

    def test_worktree_inside_the_workspace(self):
        repo = self.repo()
        git(repo, "worktree", "add", "-q", "-b", "topic",
            os.path.join(repo, "inside"))
        self.assert_anchored(repo)

    def test_workspace_is_a_linked_worktree(self):
        repo = self.repo()
        worktree = os.path.join(self.tmp, "wt")
        git(repo, "worktree", "add", "-q", "-b", "topic", worktree)
        self.assert_anchored(worktree)

    def test_separate_git_dir_inside_and_outside_the_workspace(self):
        for name, git_dir in (("inside", None), ("outside", "elsewhere")):
            repo = os.path.join(self.tmp, name)
            os.makedirs(repo)
            location = os.path.join(self.tmp, git_dir) if git_dir \
                else os.path.join(repo, ".realgit")
            git(repo, "init", "-q", "--separate-git-dir", location)
            self.assert_anchored(repo)


class SubmoduleWorktrees(GuardCase):
    def setUp(self):
        super().setUp()
        self.inner = self.repo("inner")
        self.super_repo = self.repo("super")
        git(self.super_repo, "-c", "protocol.file.allow=always", "submodule",
            "add", "-q", self.inner, "sub")
        git(self.super_repo, "commit", "-q", "-m", "add submodule")
        self.checkout = os.path.join(self.super_repo, "sub-wt")
        git(os.path.join(self.super_repo, "sub"), "worktree", "add", "-q",
            "-b", "topic", self.checkout)
        self.module_dir = os.path.join(
            self.super_repo, ".git", "modules", "sub")

    def test_a_submodules_own_worktrees_are_guarded(self):
        # A submodule is a repository too, so its worktrees need the same
        # treatment as the superproject's.
        mounts = self.mounts(self.super_repo)
        self.assertIn(
            "%s/worktrees/sub-wt/commondir:"
            "/workspace/.git/modules/sub/worktrees/sub-wt/commondir:ro"
            % self.module_dir, mounts)

    def test_the_checkout_pointer_of_a_submodule_worktree_is_guarded(self):
        mounts = self.mounts(self.super_repo)
        self.assertIn(
            "%s/.git:/workspace/sub-wt/.git:ro" % self.checkout, mounts)


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

    def test_a_worktree_inside_the_workspace_has_its_pointer_guarded(self):
        # `git worktree add wt` leaves a pointer at <workspace>/wt/.git, in a
        # directory the user works in. Rewriting it sends the host's git to a
        # git dir none of these mounts cover.
        nested = os.path.join(self.repo_path, "inside")
        git(self.repo_path, "worktree", "add", "-q", "-b", "other", nested)
        mounts = self.mounts(self.repo_path)
        self.assertIn("%s/.git:/workspace/inside/.git:ro" % nested, mounts)
        self.assertIn(
            "%s/inside:/workspace/inside" % self.repo_path, mounts)

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

    def test_directories_on_the_way_down_are_mount_points_too(self):
        # A plain directory containing a mount point can still be renamed --
        # the mounts follow it and the vacated path comes back writable -- so
        # every directory between the workspace and a guarded path is bound
        # onto itself as well.
        mounts = self.mounts(self.super_repo)
        for relative in (".git/modules", ".git/modules/libs",
                         ".git/modules/libs/nested", "libs", "libs/nested"):
            self.assertIn(
                "%s/%s:/workspace/%s" % (self.super_repo, relative, relative),
                mounts,
            )

    def test_a_planted_git_dir_cannot_pull_host_files_in_through_a_symlink(self):
        # `.git/modules` is writable, so the container can fabricate what looks
        # like a submodule git dir and point its config at a host file. Mounting
        # that would hand the next session whatever it named.
        planted = os.path.join(self.super_repo, ".git", "modules", "planted")
        os.makedirs(planted)
        with open(os.path.join(planted, "HEAD"), "w") as handle:
            handle.write("ref: refs/heads/main\n")
        secret = os.path.join(self.tmp, "secret")
        with open(secret, "w") as handle:
            handle.write("private")
        os.symlink(secret, os.path.join(planted, "config"))
        os.symlink(self.tmp, os.path.join(planted, "hooks"))
        mounts = self.mounts(self.super_repo)
        # The planted dir is inside the workspace and mounting parts of it back
        # onto itself is harmless; what must not happen is a mount whose source
        # is the symlink, which docker would resolve to the host file behind it.
        self.assertEqual(
            [m for m in mounts if m.endswith("/planted/config:ro")
             or m.endswith("/planted/hooks:ro")],
            [],
        )
        with open(secret) as handle:
            self.assertEqual(handle.read(), "private")

    def test_a_symlinked_modules_directory_is_not_walked(self):
        # Following it would guard -- and create files in -- git dirs anywhere
        # on the host the container chose to name.
        elsewhere = self.repo("elsewhere")
        shutil.rmtree(os.path.join(self.super_repo, ".git", "modules"))
        os.symlink(os.path.join(elsewhere, ".git", "modules"),
                   os.path.join(self.super_repo, ".git", "modules"))
        self.assertEqual(
            [m for m in self.mounts(self.super_repo) if elsewhere in m], [])


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

    def test_a_linked_worktrees_config_is_guarded_from_the_repos_setting(self):
        # `$GIT_DIR/config.worktree` for a linked worktree lives in its own git
        # dir, which has no `config` to read the extension from -- the
        # repository's says whether git reads it.
        repo = self.repo()
        git(repo, "config", "extensions.worktreeConfig", "true")
        worktree = os.path.join(self.tmp, "wt")
        git(repo, "worktree", "add", "-q", "-b", "topic", worktree)
        expected = "%s/.git/worktrees/wt/config.worktree:" \
            "/workspace/.git/worktrees/wt/config.worktree:ro" % repo
        self.assertIn(expected, self.mounts(repo))
        self.assertIn(expected.replace("/workspace/.git", "%s/.git" % repo),
                      self.mounts(worktree))

    def test_every_value_git_calls_true_enables_the_guard(self):
        # git reads a boolean as the bool words or an integer, so `2` is on --
        # and a value the guard read as off would leave config.worktree
        # writable while git still obeyed it.
        repo = self.repo()
        config = os.path.join(repo, ".git", "config")
        for value, enabled in (("true", True), ("yes", True), ("on", True),
                               ("1", True), ("2", True), ("-1", True),
                               # git's integers carry C's spellings: hex, a
                               # leading zero for octal, and a size suffix.
                               ("0x10", True), ("007", True), ("1k", True),
                               ("-2K", True), ("1m", True),
                               ("false", False), ("no", False), ("off", False),
                               ("0", False), ("0x0", False), ("000", False),
                               ("0k", False), ("", False), ("garbage", False)):
            git(repo, "config", "--file", config,
                "extensions.worktreeConfig", value)
            self.assertEqual(
                bool([m for m in self.mounts(repo) if "config.worktree" in m]),
                enabled, "%r should be %s" % (value, enabled))

    def test_a_valueless_key_counts_as_on(self):
        repo = self.repo()
        with open(os.path.join(repo, ".git", "config"), "a") as handle:
            handle.write("[extensions]\n\tworktreeConfig\n")
        self.assertTrue(
            [m for m in self.mounts(repo) if "config.worktree" in m])

    def test_read_from_each_git_dir_rather_than_inherited(self):
        # The extension is per-repository: a submodule someone has run
        # `git sparse-checkout` in has it on while the superproject does not.
        inner = self.repo("inner")
        super_repo = self.repo("super")
        git(super_repo, "-c", "protocol.file.allow=always", "submodule",
            "add", "-q", inner, "sub")
        module_dir = os.path.join(super_repo, ".git", "modules", "sub")
        git(module_dir, "config", "--file",
            os.path.join(module_dir, "config"),
            "extensions.worktreeConfig", "true")
        mounts = self.mounts(super_repo)
        self.assertIn(
            "%s/config.worktree:/workspace/.git/modules/sub/config.worktree:ro"
            % module_dir, mounts)
        self.assertEqual(
            [m for m in mounts if m.endswith("/.git/config.worktree:ro")], [])

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
