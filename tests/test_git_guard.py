#!/usr/bin/env python3
"""Unit tests for swarmforge.gitguard. Run: python3 tests/test_git_guard.py"""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)

# Standing in for the guard's entry-point shim keeps this file runnable on its own.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from swarmforge import gitguard


# A developer's signing key or templateDir would otherwise change these repos.
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
        # The guard leaves worktree registrations in the temp dir.
        temp = mock.patch.object(tempfile, "tempdir", self.tmp)
        temp.start()
        self.addCleanup(temp.stop)

    def records_written(self):
        """Replacement `gitdir` files the guard has left on the host."""
        directory = os.path.dirname(
            gitguard.gitdir_record_source("workspace", "/target"))
        return sorted(os.listdir(directory)) if os.path.isdir(directory) else []

    def repo(self, name="repo", bare=False):
        path = os.path.join(self.tmp, name)
        os.makedirs(path)
        git(path, "init", "-q", *(["--bare"] if bare else []))
        if not bare:
            git(path, "commit", "-q", "--allow-empty", "-m", "root")
        return path

    def mounts(self, workspace, targets=("/workspace",)):
        return gitguard.build_mounts(workspace, list(targets))


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
        # A git dir that is a mount point cannot be renamed aside.
        repo = self.repo()
        self.assertIn("%s/.git:/workspace/.git" % repo, self.mounts(repo))

    def test_every_target_gets_its_own_copy_of_the_guard(self):
        # A read-only mount covers one path, not the file under a second mount.
        repo = self.repo()
        mounts = self.mounts(repo, ("/workspace", "/repos/me/repo"))
        for target in ("/workspace", "/repos/me/repo"):
            self.assertIn(
                "%s/.git/config:%s/.git/config:ro" % (repo, target), mounts)

    def test_missing_commondir_is_created_rather_than_left_writable(self):
        # A writable commondir could relocate config and hooks; "." pins them.
        repo = self.repo()
        commondir = os.path.join(repo, ".git", "commondir")
        self.assertFalse(os.path.exists(commondir))
        self.mounts(repo)
        with open(commondir) as handle:
            self.assertEqual(handle.read().strip(), ".")
        # The repo still behaves: git resolves the pointer back to the git dir.
        git(repo, "commit", "-q", "--allow-empty", "-m", "after")
        self.assertEqual(
            gitguard.common_git_dir(repo, lambda m: None),
            os.path.join(repo, ".git"))

    def test_missing_hooks_directory_is_created_rather_than_skipped(self):
        repo = self.repo()
        hooks = os.path.join(repo, ".git", "hooks")
        shutil.rmtree(hooks)
        mounts = self.mounts(repo)
        self.assertTrue(os.path.isdir(hooks))
        self.assertIn("%s:/workspace/.git/hooks:ro" % hooks, mounts)

    def test_missing_config_is_created_rather_than_left_writable(self):
        # An absent config is room for the container to write one.
        repo = self.repo()
        config = os.path.join(repo, ".git", "config")
        os.remove(config)
        self.assertIn("%s:/workspace/.git/config:ro" % config,
                      self.mounts(repo))
        with open(config) as handle:
            self.assertEqual(handle.read(), "")
        git(repo, "commit", "-q", "--allow-empty", "-m", "after")

    def test_the_pre_config_remote_files_are_read_only(self):
        # A planted remotes/ or branches/ file hijacks fetch without config.
        repo = self.repo()
        mounts = self.mounts(repo)
        for name in ("remotes", "branches"):
            path = os.path.join(repo, ".git", name)
            self.assertTrue(os.path.isdir(path))
            self.assertIn("%s:/workspace/.git/%s:ro" % (path, name), mounts)

    def test_a_path_docker_cannot_express_is_reported_not_mangled(self):
        # A docker `-v` value is colon-separated and read one per line.
        repo = self.repo()
        planted = os.path.join(repo, ".git", "modules", "a\nb")
        os.makedirs(planted)
        with open(os.path.join(planted, "HEAD"), "w") as handle:
            handle.write("ref: refs/heads/main\n")
        warnings = []
        mounts = gitguard.build_mounts(repo, ["/workspace"],
                                       warn=warnings.append)
        self.assertEqual([m for m in mounts if "\n" in m], [])
        self.assertEqual([t for t in warnings if "colon or newline" in t],
                         ["%s cannot be expressed as a docker mount (it "
                          "contains a colon or newline); leaving it writable "
                          "in the container" % planted])
        self.assertFalse(os.path.exists(os.path.join(planted, "commondir")))
        self.assertFalse(os.path.exists(os.path.join(planted, "hooks")))

    def test_a_symlinked_git_dir_pointer_is_reported(self):
        # A mount cannot hold the symlink itself in place.
        repo = self.repo()
        moved = os.path.join(self.tmp, "elsewhere.git")
        shutil.move(os.path.join(repo, ".git"), moved)
        os.symlink(moved, os.path.join(repo, ".git"))
        warnings = []
        gitguard.build_mounts(repo, ["/workspace"], warn=warnings.append)
        self.assertTrue(any("symlink" in text for text in warnings), warnings)

    @unittest.skipIf(os.geteuid() == 0, "root ignores the permission bits")
    def test_unwritable_git_dir_warns_instead_of_failing_the_launch(self):
        repo = self.repo()
        git_dir = os.path.join(repo, ".git")
        os.chmod(git_dir, 0o500)
        self.addCleanup(os.chmod, git_dir, 0o700)
        warnings = []
        mounts = gitguard.build_mounts(repo, ["/workspace"], warn=warnings.append)
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
        # The container reaches a separate git dir through the workspace mount.
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
        # Roots: the target mounts, plus a git dir bound at its own host path.
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

    def test_submodule_initialized_inside_a_linked_worktree(self):
        inner = self.repo("inner")
        top = self.repo("top")
        git(top, "-c", "protocol.file.allow=always", "submodule", "add", "-q",
            inner, "sub")
        git(top, "commit", "-q", "-m", "add submodule")
        worktree = os.path.join(self.tmp, "topwt")
        git(top, "worktree", "add", "-q", "-b", "wtb", worktree)
        git(worktree, "-c", "protocol.file.allow=always", "submodule",
            "update", "--init", "-q")
        self.assert_anchored(top)
        self.assert_anchored(worktree)

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
        mounts = self.mounts(self.super_repo)
        self.assertIn(
            "%s/worktrees/sub-wt/commondir:"
            "/workspace/.git/modules/sub/worktrees/sub-wt/commondir:ro"
            % self.module_dir, mounts)

    def test_the_checkout_pointer_of_a_submodule_worktree_is_guarded(self):
        mounts = self.mounts(self.super_repo)
        self.assertIn(
            "%s/.git:/workspace/sub-wt/.git:ro" % self.checkout, mounts)


class SubmoduleInsideAWorktree(GuardCase):
    """git puts it under the worktree's git dir, not the repository's."""

    def setUp(self):
        super().setUp()
        self.inner = self.repo("inner")
        self.super_repo = self.repo("super")
        git(self.super_repo, "-c", "protocol.file.allow=always", "submodule",
            "add", "-q", self.inner, "sub")
        git(self.super_repo, "commit", "-q", "-m", "add submodule")
        self.worktree = os.path.join(self.tmp, "superwt")
        git(self.super_repo, "worktree", "add", "-q", "-b", "wtb",
            self.worktree)
        git(self.worktree, "-c", "protocol.file.allow=always", "submodule",
            "update", "--init", "-q")
        self.module_dir = os.path.join(
            self.super_repo, ".git", "worktrees", "superwt", "modules", "sub")

    def test_its_git_dir_is_guarded_from_the_main_checkout(self):
        self.assertTrue(os.path.isdir(self.module_dir))
        mounts = self.mounts(self.super_repo)
        for name in ("config", "hooks", "commondir"):
            self.assertIn(
                "%s/%s:/workspace/.git/worktrees/superwt/modules/sub/%s:ro"
                % (self.module_dir, name, name),
                mounts,
            )

    def test_its_git_dir_is_guarded_from_the_worktree(self):
        # From this side the common git dir is bound at its own host path.
        mounts = self.mounts(self.worktree)
        self.assertIn(
            "%s/config:%s/config:ro" % (self.module_dir, self.module_dir),
            mounts)

    def test_its_checkout_pointer_is_guarded(self):
        self.assertIn(
            "%s/sub/.git:/workspace/sub/.git:ro" % self.worktree,
            self.mounts(self.worktree))


class BareRepo(GuardCase):
    def test_guarded_paths_sit_directly_under_the_workspace_mount(self):
        # A bare repo is its own git dir, already mounted at the target.
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
        # The pointer sits in a directory the user works in.
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


class WorktreeRegistration(GuardCase):
    """`--worktree-at` restates a worktree's `gitdir` in the container's terms."""

    def setUp(self):
        super().setUp()
        self.repo_path = self.repo()
        self.common = os.path.join(self.repo_path, ".git")
        self.worktree = os.path.join(self.tmp, "wt")
        git(self.repo_path, "worktree", "add", "-q", "-b", "topic",
            self.worktree)
        self.record = os.path.join(self.common, "worktrees", "wt", "gitdir")

    def guarded(self, workspace=None, targets=("/workspace",),
                worktree_at="/workspace", warn=None):
        return gitguard.build_mounts(
            workspace or self.worktree, list(targets),
            warn=warn, worktree_at=worktree_at)

    def replacements(self, mounts):
        """The specs mounting something over a `gitdir` record."""
        return [spec for spec in mounts
                if spec.split(":")[1].endswith("/gitdir")]

    def guard_mounts(self, target="/workspace"):
        """What the workspace gets with no registration mount at all."""
        worktree_dir = os.path.join(self.common, "worktrees", "wt")
        return [
            "%s:%s" % (self.common, self.common),
            "%s/config:%s/config:ro" % (self.common, self.common),
            "%s/commondir:%s/commondir:ro" % (self.common, self.common),
            "%s/hooks:%s/hooks:ro" % (self.common, self.common),
            "%s/remotes:%s/remotes:ro" % (self.common, self.common),
            "%s/branches:%s/branches:ro" % (self.common, self.common),
            "%s/worktrees:%s/worktrees" % (self.common, self.common),
            "%s:%s" % (worktree_dir, worktree_dir),
            "%s/commondir:%s/commondir:ro" % (worktree_dir, worktree_dir),
            "%s/.git:%s/.git:ro" % (self.worktree, target),
        ]

    def test_the_option_adds_one_mount_and_changes_nothing_else(self):
        source = gitguard.gitdir_record_source(self.worktree, "/workspace")
        self.assertEqual(
            self.guarded(),
            self.guard_mounts() + ["%s:%s:ro" % (source, self.record)])

    def test_without_the_option_the_registration_is_left_alone(self):
        self.assertEqual(self.guarded(worktree_at=None), self.guard_mounts())
        self.assertEqual(self.records_written(), [])

    def test_the_replacement_names_the_checkout_the_container_has(self):
        mounts = self.guarded(targets=("/repos/me/proj",),
                              worktree_at="/repos/me/proj")
        specs = self.replacements(mounts)
        self.assertEqual(len(specs), 1, mounts)
        source, destination, mode = specs[0].split(":")
        self.assertEqual(destination, self.record)
        self.assertEqual(mode, "ro")
        with open(source) as handle:
            self.assertEqual(handle.read(), "/repos/me/proj/.git\n")

    def test_one_record_mount_however_many_targets(self):
        # The record lives in the common git dir, mounted only at its host path.
        mounts = self.guarded(targets=("/workspace", "/repos/me/proj"))
        specs = self.replacements(mounts)
        self.assertEqual(len(specs), 1, mounts)
        self.assertEqual(specs[0].split(":")[1], self.record)

    def test_the_replacement_is_somewhere_the_container_cannot_reach(self):
        source = self.replacements(self.guarded())[0].split(":")[0]
        for reachable in (self.worktree, self.common):
            self.assertFalse(gitguard.is_inside(source, reachable), source)

    def test_a_rerun_writes_the_same_file_rather_than_another_one(self):
        # Nothing ever removes these files.
        first = self.guarded()
        written = self.records_written()
        self.assertEqual(len(written), 1, written)
        self.assertEqual(self.guarded(), first)
        self.assertEqual(self.records_written(), written)

    def test_the_hosts_own_record_is_left_as_it_was(self):
        # The host's `git worktree prune` would read a container path as gone.
        with open(self.record) as handle:
            before = handle.read()
        self.guarded()
        with open(self.record) as handle:
            self.assertEqual(handle.read(), before)

    def test_a_plain_checkout_has_no_registration_to_restate(self):
        # And nothing to say about the option either, however it is spelled.
        plain = self.repo("plain")
        warnings = []
        self.assertEqual(
            gitguard.build_mounts(plain, ["/workspace"], warn=warnings.append,
                                  worktree_at="/repos/a:b"),
            gitguard.build_mounts(plain, ["/workspace"]))
        self.assertEqual(warnings, [])
        self.assertEqual(self.records_written(), [])

    def test_the_repo_the_worktree_belongs_to_is_left_alone(self):
        self.assertEqual(
            self.replacements(self.guarded(workspace=self.repo_path)), [])
        self.assertEqual(self.records_written(), [])

    def assert_refused(self, warning, mounts, warnings):
        self.assertEqual(self.replacements(mounts), [])
        self.assertEqual(self.records_written(), [])
        self.assertIn(gitguard.registration_refused(warning), warnings)

    def test_a_symlinked_record_is_refused(self):
        elsewhere = os.path.join(self.tmp, "elsewhere")
        with open(elsewhere, "w") as handle:
            handle.write("/somewhere/.git\n")
        os.remove(self.record)
        os.symlink(elsewhere, self.record)
        warnings = []
        mounts = self.guarded(warn=warnings.append)
        self.assert_refused("%s is a symlink" % self.record, mounts, warnings)
        with open(elsewhere) as handle:
            self.assertEqual(handle.read(), "/somewhere/.git\n")

    def test_a_missing_record_is_reported(self):
        os.remove(self.record)
        warnings = []
        mounts = self.guarded(warn=warnings.append)
        self.assert_refused(
            "%s has no gitdir record" % os.path.dirname(self.record),
            mounts, warnings)

    def test_a_symlinked_worktrees_directory_is_reported(self):
        # Kept inside the common dir so the relative `commondir` still resolves.
        moved = os.path.join(self.common, "worktree-store")
        shutil.move(os.path.join(self.common, "worktrees"), moved)
        os.symlink(moved, os.path.join(self.common, "worktrees"))
        warnings = []
        mounts = self.guarded(warn=warnings.append)
        self.assert_refused(
            "%s is a linked worktree whose git dir is not under %s/worktrees"
            % (self.worktree, self.common), mounts, warnings)

    def test_a_container_path_the_workspace_is_not_mounted_at_is_refused(self):
        warnings = []
        mounts = self.guarded(worktree_at="/repos/x", warn=warnings.append)
        self.assert_refused(
            "/repos/x is not one of the paths the workspace is mounted at "
            "(/workspace)", mounts, warnings)

    def test_a_container_path_docker_cannot_express_creates_nothing(self):
        warnings = []
        mounts = self.guarded(targets=("/repos/a:b",),
                              worktree_at="/repos/a:b", warn=warnings.append)
        self.assert_refused(
            "/repos/a:b cannot be expressed as a docker mount (it contains a "
            "colon or newline)", mounts, warnings)

    def test_a_record_path_docker_cannot_express_creates_nothing(self):
        # A colon anywhere above the repository reaches the record's host path.
        odd = os.path.join(self.tmp, "od:d")
        repo = os.path.join(odd, "repo")
        os.makedirs(repo)
        git(repo, "init", "-q")
        git(repo, "commit", "-q", "--allow-empty", "-m", "root")
        worktree = os.path.join(odd, "wt")
        git(repo, "worktree", "add", "-q", "-b", "other", worktree)
        warnings = []
        mounts = gitguard.build_mounts(worktree, ["/workspace"],
                                       warn=warnings.append,
                                       worktree_at="/workspace")
        self.assertEqual(self.replacements(mounts), [])
        self.assertEqual(self.records_written(), [])
        self.assertTrue([w for w in warnings if "colon or newline" in w],
                        warnings)

    def test_a_temp_dir_inside_the_workspace_is_refused(self):
        inside = os.path.join(self.worktree, ".tmp")
        os.makedirs(inside)
        warnings = []
        with mock.patch.object(tempfile, "tempdir", inside):
            mounts = self.guarded(warn=warnings.append)
            self.assertEqual(self.records_written(), [])
        self.assertEqual(self.replacements(mounts), [])
        self.assertTrue(
            [w for w in warnings if "which the container mounts" in w],
            warnings)

    def test_a_shared_temp_dir_is_refused(self):
        # A predictable name in a shared directory can be pre-filled.
        directory = os.path.dirname(
            gitguard.gitdir_record_source(self.worktree, "/workspace"))
        os.makedirs(directory, mode=0o700)
        os.chmod(directory, 0o777)
        warnings = []
        mounts = self.guarded(warn=warnings.append)
        self.assertEqual(self.replacements(mounts), [])
        self.assertEqual(os.listdir(directory), [])
        self.assertTrue(
            [w for w in warnings if "this user alone can write" in w], warnings)

    def test_a_symlinked_temp_dir_is_refused(self):
        directory = os.path.dirname(
            gitguard.gitdir_record_source(self.worktree, "/workspace"))
        target = os.path.join(self.tmp, "planted")
        os.makedirs(target, mode=0o700)
        os.symlink(target, directory)
        warnings = []
        mounts = self.guarded(warn=warnings.append)
        self.assertEqual(self.replacements(mounts), [])
        self.assertEqual(os.listdir(target), [])
        self.assertTrue(
            [w for w in warnings if "this user alone can write" in w], warnings)


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
        # It has its own config and hooks, which the host runs in the submodule.
        mounts = self.mounts(self.super_repo)
        for name in ("config", "hooks", "commondir"):
            self.assertIn(
                "%s/%s:/workspace/.git/modules/libs/nested/%s:ro"
                % (self.module_dir, name, name),
                mounts,
            )

    def test_submodule_checkout_pointer_is_read_only(self):
        # Guarding the git dir is moot if its pointer can be repointed.
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
        # A plain directory renamed aside leaves its path writable again.
        mounts = self.mounts(self.super_repo)
        for relative in (".git/modules", ".git/modules/libs",
                         ".git/modules/libs/nested", "libs", "libs/nested"):
            self.assertIn(
                "%s/%s:/workspace/%s" % (self.super_repo, relative, relative),
                mounts,
            )

    def test_a_planted_git_dir_cannot_pull_host_files_in_through_a_symlink(self):
        # .git/modules is writable, so a faked git dir can name any host file.
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
        # Self-binds are harmless; a symlink source resolves to the host file.
        self.assertEqual(
            [m for m in mounts if m.endswith("/planted/config:ro")
             or m.endswith("/planted/hooks:ro")],
            [],
        )
        with open(secret) as handle:
            self.assertEqual(handle.read(), "private")

    def test_a_symlinked_modules_directory_is_not_walked(self):
        # Following it would create files in git dirs the container names.
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
        # A linked worktree's git dir has no config to enable the extension.
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
        # git reads a boolean as the bool words or a nonzero integer; reading
        # a true value as off would leave config.worktree writable regardless.
        repo = self.repo()
        config = os.path.join(repo, ".git", "config")
        for value, enabled in (("true", True), ("yes", True), ("on", True),
                               ("1", True), ("2", True), ("-1", True),
                               # git's integers: C spellings plus a suffix.
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
        # The extension is per-repository: a submodule can have it on alone.
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


class BareRepair(unittest.TestCase):
    """The repair the warning prints is the one `swarmforge.worktrees` runs.

    Their order matters: `git config --worktree` refuses to write until
    `extensions.worktreeConfig` is on.
    """

    def test_the_three_commands_in_order(self):
        self.assertEqual(gitguard.bare_repair("/g"), [
            ["git", "-C", "/g", "config", "extensions.worktreeConfig", "true"],
            ["git", "-C", "/g", "config", "--unset", "core.bare"],
            ["git", "-C", "/g", "config", "--worktree", "core.bare", "true"],
        ])

    def test_the_warning_spells_out_every_command(self):
        warning = gitguard.bare_flip_warning("/g")
        for command in gitguard.bare_repair("/g"):
            self.assertIn("\n  %s" % " ".join(command), warning)


class BareRootWithSiblingWorktrees(GuardCase):
    """A bare `repo/.git` whose checkouts are linked worktrees beside it.

    Git takes a git dir holding a `commondir` for a linked worktree's and
    ignores `core.bare` there, so the guard's placeholder makes `repo/` read
    as a checkout. Moving `core.bare` into `config.worktree` keeps it bare,
    since git reads that file after deciding.
    """

    def setUp(self):
        super().setUp()
        seed = self.repo("seed")
        self.root = os.path.join(self.tmp, "repo")
        self.common = os.path.join(self.root, ".git")
        os.makedirs(self.root)
        git(self.tmp, "clone", "-q", "--bare", seed, self.common)
        self.worktree = os.path.join(self.root, "wt1")
        git(self.common, "worktree", "add", "-q", self.worktree, "master")
        self.workspaces = (self.worktree, self.root)

    def repair(self):
        git(self.common, "config", "extensions.worktreeConfig", "true")
        git(self.common, "config", "--unset", "core.bare")
        git(self.common, "config", "--worktree", "core.bare", "true")

    def is_bare(self, path):
        return subprocess.run(
            ["git", "-C", path, "rev-parse", "--is-bare-repository"],
            check=True, capture_output=True, text=True, env=GIT_ENV,
        ).stdout.strip() == "true"

    def test_warns_when_core_bare_is_in_the_shared_config(self):
        for workspace in self.workspaces:
            with self.subTest(workspace=workspace):
                warnings = []
                gitguard.build_mounts(workspace, ["/workspace"],
                                      warn=warnings.append)
                self.assertFalse(self.is_bare(self.root))
                bare = [w for w in warnings if w.startswith(self.common)]
                self.assertEqual(len(bare), 1, warnings)
                self.assertIn(
                    "git -C %s config --worktree core.bare true" % self.common,
                    bare[0])

    def test_the_worktrees_registration_is_restated_for_the_container(self):
        # The layout `swarmforge init` builds.
        self.repair()
        mounts = gitguard.build_mounts(
            self.worktree, ["/repos/me/repo"], worktree_at="/repos/me/repo")
        record = os.path.join(self.common, "worktrees", "wt1", "gitdir")
        spec, = [m for m in mounts if m.split(":")[1] == record]
        self.assertTrue(spec.endswith(":ro"), spec)
        with open(spec.split(":")[0]) as handle:
            self.assertEqual(handle.read(), "/repos/me/repo/.git\n")

    def test_repaired_repo_stays_bare_and_its_worktrees_stay_checkouts(self):
        self.repair()
        for workspace in self.workspaces:
            with self.subTest(workspace=workspace):
                warnings = []
                mounts = gitguard.build_mounts(workspace, ["/workspace"],
                                               warn=warnings.append)
                self.assertTrue(os.path.isfile(
                    os.path.join(self.common, "commondir")))
                self.assertTrue(self.is_bare(self.root))
                self.assertFalse(self.is_bare(self.worktree))
                self.assertFalse(
                    [w for w in warnings if w.startswith(self.common)],
                    warnings)
                self.assertTrue(
                    any("/config.worktree:" in spec and spec.endswith(":ro")
                        and spec.startswith(self.common + "/config.worktree:")
                        for spec in mounts),
                    mounts)


class CommandLine(GuardCase):
    def _main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        code = gitguard.main(argv, out=out, err=err)
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

    def test_worktree_at_reaches_the_guard(self):
        repo = self.repo()
        worktree = os.path.join(self.tmp, "wt")
        git(repo, "worktree", "add", "-q", "-b", "topic", worktree)
        record = os.path.join(repo, ".git", "worktrees", "wt", "gitdir")
        code, out, _ = self._main(
            ["--workspace", worktree, "--target", "/workspace",
             "--worktree-at", "/workspace"])
        self.assertEqual(code, 0)
        spec, = [line for line in out.splitlines()
                 if line.split(":")[1] == record]
        with open(spec.split(":")[0]) as handle:
            self.assertEqual(handle.read(), "/workspace/.git\n")

    def test_missing_arguments_are_a_usage_error(self):
        for argv in ([], ["--workspace", "/x"], ["--target", "/workspace"],
                     ["--workspace"], ["--bogus"],
                     ["--workspace", "/x", "--target", "/w", "--worktree-at"]):
            code, out, err = self._main(argv)
            self.assertEqual(code, 2, argv)
            self.assertEqual(out, "")
            self.assertTrue(err)


if __name__ == "__main__":
    if shutil.which("git") is None:
        sys.stderr.write("git is required for these tests\n")
        sys.exit(1)
    unittest.main(verbosity=2)
