#!/usr/bin/env python3
"""Tests for how the swarmforge package holds together.

These assert on the shape of the import graph rather than on what any module
does, so they catch the kind of regression that leaves every other test green:
a module reaching back into one that already depends on it works fine until the
import order changes, and then it fails at startup with a partially initialised
module rather than anywhere near the edge that caused it.

Run: python3 tests/test_package_layering.py
"""

import ast
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
PACKAGE_ROOT = os.path.join(REPO_ROOT, "swarmforge")

# Directory names that never hold python of ours wherever they turn up:
# version control, tool caches, and vendored dependencies.
NOT_OURS = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
    "venv",
}

# Stores the Makefile bind-mounts into containers, which come back holding
# whatever the container wrote. Matched by their place in the tree rather than
# by name: `ollama` in particular is a word this repo uses for its own things,
# and a package directory that happened to be called that should still be read.
NOT_OURS_PATHS = {
    os.path.join(REPO_ROOT, "ollama"),
    os.path.join(REPO_ROOT, "anvil", "data"),
    os.path.join(REPO_ROOT, ".opencode-test-data"),
}

# The harness package and the launcher layers it sits under, as the prefixes
# the one-way rule matches on. A module counts as inside one of these when it
# is the package itself or anything below it.
HARNESS_ROOT = "swarmforge.harness"
LAUNCHER_ROOTS = ("swarmforge.tongs", "swarmforge.anvil")

# importlib's load-a-module-from-a-file-path helper.
PATH_LOADER = "spec_from_file_location"

# The entry-point shims are the only files allowed to resolve a path: putting
# the checkout on sys.path is their whole job, and every module downstream of
# them imports by name.
SHIM_DIR = os.path.join(REPO_ROOT, "bin")

# Files outside bin/ that may still load python from a file path. Empty, and
# meant to stay that way: everything the repo ships is a module with a name to
# import it by. An entry here is a standing exception, so the test below fails
# on one that has gone stale rather than letting it sit.
PATH_LOADING_ALLOWED = set()


def is_python(path):
    """Whether `path` holds python: named .py, or run by a python shebang.

    The entry-point shims carry no extension, because they are commands
    rather than modules -- the shebang is the only thing that says what they
    are written in.

    Only regular files are considered, and one that will not open is not one
    of ours: a checkout can hold a dangling symlink, a fifo left by a test, or
    a root-owned file a container wrote, and none of those should decide
    whether the suite runs.
    """
    if not os.path.isfile(path):
        return False
    if path.endswith(".py"):
        return True
    try:
        with open(path, "rb") as handle:
            first = handle.readline(256)
    except OSError:
        return False
    return first.startswith(b"#!") and b"python" in first


def is_pruned(dirpath, name):
    """Whether the walk should skip the directory `name` sitting in `dirpath`."""
    return name in NOT_OURS or os.path.join(dirpath, name) in NOT_OURS_PATHS


def python_files(root):
    """Every python file under `root`, in a stable order, caches aside."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not is_pruned(dirpath, d))
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            if is_python(path):
                yield path


def module_name(path):
    """The dotted name a file under the repo root is imported by.

    A package's `__init__.py` is the package itself, so it names the
    directory rather than a module inside it.
    """
    parts = os.path.relpath(path, REPO_ROOT).split(os.sep)
    parts[-1] = parts[-1][: -len(".py")]
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def package_of(path):
    """The dotted package a file's relative imports count from."""
    return module_name(os.path.join(os.path.dirname(path), "__init__.py"))


def absolute_module(node, package):
    """An `ast.ImportFrom`'s module as an absolute name.

    Leading dots count up from the importing file's own package: one dot is
    that package, each further dot a level above it. `from .model import X`
    inside `swarmforge.tongs.argv` therefore reads `swarmforge.tongs.model`,
    and a bare `from . import x` reads `swarmforge.tongs`.
    """
    if not node.level:
        return node.module
    parts = package.split(".")
    base = ".".join(parts[: max(0, len(parts) - (node.level - 1))])
    return "%s.%s" % (base, node.module) if node.module else base


def is_type_checking_block(node):
    """Whether `node` is an `if TYPE_CHECKING:` guard.

    `typing.TYPE_CHECKING` is false at runtime, so the body only ever runs
    under a type checker. Matched by name, qualified or not, because that is
    all the guard ever is.
    """
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return isinstance(test, ast.Name) and test.id == "TYPE_CHECKING"


def import_time_nodes(tree):
    """Every node of `tree` that runs when the module is imported.

    Two things are skipped, and they are the two ways to keep a name without
    importing it at import time -- the standard fixes for a cycle. Counting
    either would report a ring that no longer exists, failing the very fix it
    asked for:

    - function bodies, because the module is fully initialised by the time
      the call happens;
    - `if TYPE_CHECKING:` bodies, because they never run at all.

    Class bodies, `try` blocks, and every other module-level `if` do run at
    import, so they are walked.

    Only sound for finding statements. A skipped function still has its
    decorators and its argument defaults evaluated at import, and those are
    not visited -- an import cannot appear in either, but an expression can.
    """
    stack = list(ast.iter_child_nodes(tree))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if is_type_checking_block(node):
            # Only the body is skipped: an `else` on such a guard holds the
            # runtime half, and that does run.
            stack.extend(node.orelse)
            continue
        stack.extend(ast.iter_child_nodes(node))


def reached_by(target, importer, modules):
    """`target` plus every package importing it runs on the way there.

    Reaching into a package imports its `__init__` first, so depending on
    `pkg.leaf` is also depending on `pkg`. Packages the importer already sits
    inside are left out: their `__init__` imports their own children by
    design, and the interpreter resolves that partial state without trouble.
    Across a package boundary there is no such tolerance -- that is where a
    ring actually strands a half-initialised module -- so those count.
    """
    reached = {target}
    parts = target.split(".")
    for depth in range(1, len(parts)):
        ancestor = ".".join(parts[:depth])
        inside = importer == ancestor or importer.startswith(ancestor + ".")
        if not inside and ancestor in modules:
            reached.add(ancestor)
    return reached


def all_nodes(tree):
    """Every node of `tree`, the ones only a call ever runs included."""
    return ast.walk(tree)


def imported_modules(tree, importer, package, modules, nodes=import_time_nodes):
    """The modules in `modules` that this syntax tree imports.

    `from pkg import name` reaches `pkg.name` when that name is a submodule
    and `pkg` itself when it is a function or a constant, so only the more
    specific of the two is taken as the target. `nodes` picks which part of
    the tree counts: the import-time statements by default, or every node
    for a rule that must also catch an import deferred into a call.
    """
    found = set()
    for node in nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in modules:
                    found |= reached_by(alias.name, importer, modules)
        elif isinstance(node, ast.ImportFrom):
            base = absolute_module(node, package)
            for alias in node.names:
                for candidate in ("%s.%s" % (base, alias.name), base):
                    if candidate in modules:
                        found |= reached_by(candidate, importer, modules)
                        break
    return found


def import_graph():
    """Module name -> the package modules it imports, for the whole package.

    Only imports a statement spells out, and only ones landing inside the
    package: the stdlib is not part of the layering under test.
    """
    # Only files named .py: a module is reached by a dotted name, and that
    # name comes from the filename. An extensionless command is not one.
    paths = {
        module_name(path): path
        for path in python_files(PACKAGE_ROOT)
        if path.endswith(".py")
    }
    graph = {}
    for name, path in paths.items():
        # No guard on the parse: every file here is one the repo owns, and one
        # that will not parse is a broken module, not a file to skip over.
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), path)
        edges = imported_modules(tree, name, package_of(path), paths)
        graph[name] = edges - {name}
    return graph


def is_within(name, package):
    """Whether the module `name` is `package` itself or something inside it.

    The dot matters: `swarmforge.tongsmith` starts with `swarmforge.tongs`
    as text and is a different package entirely.
    """
    return name == package or name.startswith(package + ".")


def path_loading_sites(path):
    """The lines in `path` that reach the path loader, however it is spelled.

    Read out of the syntax tree rather than the text, so naming the helper in
    a docstring -- or in a rule about it, as here -- is not itself a use. Both
    the qualified call and a bare import of the name count; an aliased import
    is caught at the import.

    A file that will not read or parse yields nothing. This scan covers the
    whole checkout, which can hold a script written for another interpreter or
    a data file that only looks like source. What keeps that from hiding a
    violation is that the linter parses everything the repo owns and fails on
    what it cannot read -- so python the linter does not cover, were any
    added, would need covering there too.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), path)
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
        return []
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == PATH_LOADER:
            lines.append(node.lineno)
        elif isinstance(node, ast.Name) and node.id == PATH_LOADER:
            lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if any(alias.name == PATH_LOADER for alias in node.names):
                lines.append(node.lineno)
    return sorted(set(lines))


def is_allowed_to_path_load(path):
    return path in PATH_LOADING_ALLOWED or os.path.dirname(path) == SHIM_DIR


def find_cycle(graph):
    """One import cycle as the modules around it, or None if there is none.

    The whole ring is reported rather than the edge that closed it: an edge
    on its own does not say which module in the ring is the one that should
    not have reached back.
    """
    visiting = []
    settled = set()

    def walk(node):
        if node in settled:
            return None
        if node in visiting:
            return visiting[visiting.index(node):] + [node]
        visiting.append(node)
        for other in sorted(graph[node]):
            cycle = walk(other)
            if cycle:
                return cycle
        visiting.pop()
        settled.add(node)
        return None

    for node in sorted(graph):
        cycle = walk(node)
        if cycle:
            return cycle
    return None


class ImportGraphIsAcyclic(unittest.TestCase):
    """Every module in the package must sit at a level, and stay there.

    The layering is what lets the same package serve the host launcher and
    the container: `yamlite` is a leaf both sides import, the tongs modules
    build on each other in one direction, and the anvil modules sit on top
    of tongs. A cycle means some module has stopped having a level.
    """

    def test_the_graph_reads_every_spelling_the_package_uses(self):
        """A resolver that matched nothing would find no cycle either.

        The three spellings below are the ones the package imports with, so
        each must produce an edge -- otherwise the check passes on an empty
        graph and reports the layering clean because it never looked.
        """
        graph = import_graph()
        # from .model import ... -- relative, inside a subpackage.
        self.assertIn("swarmforge.tongs.model", graph["swarmforge.tongs.argv"])
        # from swarmforge.yamlite import ... -- absolute, naming a module.
        self.assertIn("swarmforge.yamlite", graph["swarmforge.agents.translate"])
        # from swarmforge import tongs -- absolute, naming a subpackage.
        self.assertIn("swarmforge.tongs", graph["swarmforge.anvil.docker"])

    def test_reaching_into_a_package_depends_on_that_package(self):
        """The edge a ring across a package boundary is made of.

        A module naming `tongs.model` from outside depends on `tongs` too --
        python runs the package's `__init__` on the way in, and that file
        imports every tongs module. Miss it and the ring closes through an
        `__init__` the graph never drew.
        """
        modules = set(import_graph())
        reached = reached_by("swarmforge.tongs.model", "swarmforge.yamlite", modules)
        self.assertIn("swarmforge.tongs", reached)
        # ...but a module already inside the package does not: an `__init__`
        # importing its own children is the arrangement, not a cycle.
        inside = reached_by(
            "swarmforge.tongs.model", "swarmforge.tongs.argv", modules)
        self.assertNotIn("swarmforge.tongs", inside)

    def test_only_the_imports_that_run_at_import_are_edges(self):
        """Which imports count, on a module written to hold all the shapes.

        The two that must not count are the two standard ways out of a cycle,
        so getting either backwards turns this check into one that fails the
        fix it demanded.
        """
        module = ast.parse(
            "from typing import TYPE_CHECKING\n"
            "from runs import a\n"
            "if TYPE_CHECKING:\n"
            "    from deferred_by_the_guard import b\n"
            "else:\n"
            "    from runs_as_the_runtime_half import c\n"
            "try:\n"
            "    from runs_in_a_try import d\n"
            "except ImportError:\n"
            "    from runs_in_the_handler import e\n"
            "class K:\n"
            "    from runs_in_a_class_body import f\n"
            "def fn():\n"
            "    from deferred_into_a_call import g\n"
        )
        counted = {
            node.module for node in import_time_nodes(module)
            if isinstance(node, ast.ImportFrom)
        }
        self.assertEqual(counted, {
            "typing",
            "runs",
            "runs_as_the_runtime_half",
            "runs_in_a_try",
            "runs_in_the_handler",
            "runs_in_a_class_body",
        })

    def test_no_module_in_the_package_imports_in_a_circle(self):
        cycle = find_cycle(import_graph())
        self.assertIsNone(cycle, "import cycle: %s" % " -> ".join(cycle or []))


class HarnessModulesStayBelowTheLaunchers(unittest.TestCase):
    """The harness modules describe a harness; they do not drive one.

    Each of them covers a single harness and ships into the container image,
    so the only package code they reach for is leaf helpers -- the agent
    emitters, the config readers, `yamlite`. The launcher layers go the other
    way and dispatch through them by name. An edge from a harness module back
    into tongs or anvil would leave the two halves of the package depending on
    each other, and would drag launcher-only code onto the path the image
    imports.
    """

    def test_the_scan_sees_the_harness_modules_and_the_edge_into_them(self):
        """A rule matching no modules would report no violation either.

        These are the three pieces the rule is built out of: a harness module
        in the graph with the leaf import it is allowed, the registry edge
        that gathers the harnesses up, and the launcher edge that gives the
        rule a direction to be one-way in.
        """
        graph = import_graph()
        self.assertIn(
            "swarmforge.agents.emit", graph["swarmforge.harness.claude"])
        self.assertIn("swarmforge.harness.claude", graph["swarmforge.harness"])
        self.assertIn("swarmforge.harness", graph["swarmforge.tongs.mcp"])

    def test_no_harness_module_imports_tongs_or_anvil(self):
        offenders = []
        for name, edges in sorted(import_graph().items()):
            if not is_within(name, HARNESS_ROOT):
                continue
            for edge in sorted(edges):
                if any(is_within(edge, root) for root in LAUNCHER_ROOTS):
                    offenders.append("%s -> %s" % (name, edge))
        self.assertEqual(
            offenders, [],
            "harness modules import launcher code at import time: %s"
            % ", ".join(offenders),
        )

    def test_the_lazy_scan_sees_what_the_import_time_scan_skips(self):
        """The two scans differ on exactly the deferred-import shape.

        The call-time rule below exists because a harness module already
        defers one import into a function body on purpose; a scan that
        skipped those bodies would wave the same trick through for tongs
        and anvil.
        """
        module = ast.parse("def fn():\n    from deferred import x\n")
        lazy = {
            node.module for node in all_nodes(module)
            if isinstance(node, ast.ImportFrom)
        }
        eager = {
            node.module for node in import_time_nodes(module)
            if isinstance(node, ast.ImportFrom)
        }
        self.assertIn("deferred", lazy)
        self.assertNotIn("deferred", eager)
        # And on the real files: the registry lookup the harness modules
        # defer into their functions is visible to the lazy scan.
        paths = {
            module_name(path): path
            for path in python_files(PACKAGE_ROOT)
            if path.endswith(".py")
        }
        claude = paths["swarmforge.harness.claude"]
        with open(claude, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), claude)
        edges = imported_modules(
            tree, "swarmforge.harness.claude", package_of(claude), paths,
            nodes=all_nodes)
        self.assertIn("swarmforge.harness", edges)

    def test_no_harness_module_reaches_the_launchers_even_from_a_call(self):
        """Deferring the import must not smuggle the dependency in.

        The one-way rule above reads import-time edges, which is what the
        acyclic check needs -- but a `from swarmforge import tongs` inside a
        function body would pass it while still tying the harness half to
        the launcher half the first time the function ran. Harness modules
        get the stricter reading: no import of tongs or anvil anywhere in
        the file.
        """
        paths = {
            module_name(path): path
            for path in python_files(PACKAGE_ROOT)
            if path.endswith(".py")
        }
        offenders = []
        for name, path in sorted(paths.items()):
            if not is_within(name, HARNESS_ROOT):
                continue
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), path)
            edges = imported_modules(
                tree, name, package_of(path), paths, nodes=all_nodes)
            for edge in sorted(edges):
                if any(is_within(edge, root) for root in LAUNCHER_ROOTS):
                    offenders.append("%s -> %s" % (name, edge))
        self.assertEqual(
            offenders, [],
            "harness modules reach launcher code from a call: %s"
            % ", ".join(offenders),
        )


class PathLoadingStaysInTheShims(unittest.TestCase):
    """Only the entry-point shims may load python out of a file path.

    Loading a module by path is what the flat layout did, and it costs the
    thing a package buys: a module loaded that way is a second copy under a
    name nothing else imports, invisible to a linter and unmockable through
    the usual seams. Every consumer also has to re-derive the path, so the
    same block gets copied to each new one.
    """

    def sites(self):
        """Every reachable use of the path loader, as `path:line` strings."""
        found = []
        for path in python_files(REPO_ROOT):
            for line in path_loading_sites(path):
                found.append("%s:%d" % (os.path.relpath(path, REPO_ROOT), line))
        return sorted(found)

    def test_the_scan_reaches_the_files_the_rule_is_about(self):
        """The rule is only as wide as the walk that feeds it.

        Nothing in the repo loads by path today outside the shims, so an
        empty result reads exactly like a clean one -- a pruned directory or
        a file the shebang check stopped recognising would pass silently.
        These three stand for the three shapes the walk has to keep finding.
        """
        scanned = {
            os.path.relpath(path, REPO_ROOT) for path in python_files(REPO_ROOT)
        }
        for expected in (
            os.path.join("bin", "run-anvil"),  # a command, no extension
            os.path.join("swarmforge", "tongs", "discovery.py"),
            os.path.join("tests", "test_merge_json.py"),
        ):
            self.assertIn(expected, scanned)

    def test_container_stores_are_pruned_by_place_and_not_by_name(self):
        """The distinction the path-keyed prune list exists to make.

        `ollama` names a directory the Makefile fills with container state,
        and it is also a word this repo uses for its own things. Pruning the
        name everywhere would quietly drop a package directory out of both
        checks; pruning the path drops only the store.
        """
        self.assertTrue(is_pruned(REPO_ROOT, "ollama"))
        self.assertFalse(is_pruned(PACKAGE_ROOT, "ollama"))
        # Names with no meaning of their own still go wherever they appear.
        self.assertTrue(is_pruned(PACKAGE_ROOT, "__pycache__"))

    def test_nothing_outside_the_shims_loads_python_by_path(self):
        offenders = [
            site for site in self.sites()
            if not is_allowed_to_path_load(os.path.join(REPO_ROOT, site.rsplit(":", 1)[0]))
        ]
        self.assertEqual(
            offenders, [],
            "%s outside bin/: import the module instead" % PATH_LOADER,
        )

    def test_every_standing_exception_still_needs_one(self):
        """An exemption nobody needs is a hole in the rule, not a comment.

        Whatever forced a file onto the list is the kind of thing that gets
        fixed elsewhere -- a script moving into the package, a test being
        rewritten -- and nothing else would notice the entry going stale.
        """
        for path in sorted(PATH_LOADING_ALLOWED):
            self.assertTrue(
                os.path.isfile(path),
                "%s is exempt from the %s rule but does not exist"
                % (os.path.relpath(path, REPO_ROOT), PATH_LOADER),
            )
            self.assertTrue(
                path_loading_sites(path),
                "%s no longer loads by path; drop its exemption"
                % os.path.relpath(path, REPO_ROOT),
            )


if __name__ == "__main__":
    unittest.main()
