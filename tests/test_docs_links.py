#!/usr/bin/env python3
"""Tests for how the documentation tree holds together.

The prose lives in one file per subject under `docs/`, which trades a single
scrollable README for a web of relative links -- and a relative link is the
kind of thing that rots quietly: a page renames, a section moves to a
neighbouring file, and the link still renders as a link. These assert on the
shape of that web rather than on any page's content: every relative target
resolves to a file that is there, every `#fragment` names a heading the target
file actually has, and every page is reachable by following links from the
index, so a page cannot be added and then left with nothing pointing at it.

Run: python3 tests/test_docs_links.py
"""

import os
import re
import shutil
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
DOCS_ROOT = os.path.join(REPO_ROOT, "docs")
INDEX = os.path.join(DOCS_ROOT, "README.md")
ROOT_README = os.path.join(REPO_ROOT, "README.md")

# An inline markdown link; the pattern skips targets holding whitespace or a nested paren.
LINK = re.compile(r"\[[^\]]*\]\(([^()\s]+)\)")
FENCE = re.compile(r"^\s{0,3}(```|~~~)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")

# A URI scheme in front means the target is not a file in this repo.
EXTERNAL = re.compile(r"^[a-z][a-z0-9+.-]*:")


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def pages(root):
    """Every markdown file under `root`, deepest-last and sorted."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            if name.endswith(".md"):
                found.append(os.path.join(dirpath, name))
    return found


def outside_fences(path):
    """(line, line number) for every line outside a fenced code block.

    Code blocks are skipped rather than parsed: the tong and agent examples
    quote YAML and JSON that a link regex is happy to find brackets in. A
    block closes on the marker that opened it, so a `~~~` block quoting a
    triple-backtick line does not read as two blocks.
    """
    opener = None
    for number, line in enumerate(read(path).splitlines(), 1):
        match = FENCE.match(line)
        if match:
            if opener is None:
                opener = match.group(1)
            elif match.group(1) == opener:
                opener = None
            continue
        if opener is None:
            yield line, number


def unclosed_fence(path):
    """Whether `path` ends inside a code block, which would hide the rest."""
    opener = None
    for line in read(path).splitlines():
        match = FENCE.match(line)
        if not match:
            continue
        if opener is None:
            opener = match.group(1)
        elif match.group(1) == opener:
            opener = None
    return opener is not None


def links(path):
    """(target, line number) for every link outside a fenced code block."""
    found = []
    for line, number in outside_fences(path):
        for target in LINK.findall(line):
            found.append((target, number))
    return found


def slug(heading):
    """The fragment a markdown renderer derives from a heading."""
    text = heading.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s", "-", text.strip())


def fragments(path):
    """Every fragment the headings of `path` answer to."""
    found = set()
    for line, _ in outside_fences(path):
        match = HEADING.match(line)
        if match:
            found.add(slug(match.group(2)))
    return found


def dangling(path):
    """Links from `path` that point at no file, as (target, line) pairs."""
    broken = []
    for target, number in links(path):
        if EXTERNAL.match(target):
            continue
        relative = target.split("#", 1)[0]
        if not relative:
            continue
        resolved = os.path.normpath(
            os.path.join(os.path.dirname(path), relative))
        if not os.path.isfile(resolved):
            broken.append((target, number))
    return broken


def missing_fragments(path):
    """Links from `path` naming a heading the target file does not have."""
    broken = []
    for target, number in links(path):
        if EXTERNAL.match(target) or "#" not in target:
            continue
        relative, fragment = target.split("#", 1)
        resolved = os.path.normpath(
            os.path.join(os.path.dirname(path), relative or path))
        if not resolved.endswith(".md") or not os.path.isfile(resolved):
            continue
        if fragment not in fragments(resolved):
            broken.append((target, number))
    return broken


def reachable(index, root):
    """The pages under `root` a reader reaches by following links from `index`."""
    seen = set()
    queue = [os.path.normpath(index)]
    while queue:
        path = queue.pop()
        if path in seen or not os.path.isfile(path):
            continue
        seen.add(path)
        for target, _ in links(path):
            if EXTERNAL.match(target):
                continue
            relative = target.split("#", 1)[0]
            if not relative.endswith(".md"):
                continue
            resolved = os.path.normpath(
                os.path.join(os.path.dirname(path), relative))
            if resolved.startswith(root + os.sep):
                queue.append(resolved)
    return seen


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


class DocsTreeTest(unittest.TestCase):
    """The documentation tree as it stands in the repo."""

    def test_the_tree_is_there(self):
        """A rename that empties docs/ fails here rather than silently."""
        self.assertTrue(os.path.isfile(INDEX))
        self.assertGreater(len(pages(DOCS_ROOT)), 1)

    def test_every_relative_link_resolves(self):
        for path in [ROOT_README] + pages(DOCS_ROOT):
            broken = dangling(path)
            self.assertEqual(
                broken, [],
                "%s links at nothing: %s"
                % (os.path.relpath(path, REPO_ROOT), broken))

    def test_every_fragment_names_a_heading(self):
        """A link into a page's section, after the section moved pages."""
        for path in [ROOT_README] + pages(DOCS_ROOT):
            broken = missing_fragments(path)
            self.assertEqual(
                broken, [],
                "%s names a heading that is not there: %s"
                % (os.path.relpath(path, REPO_ROOT), broken))

    def test_no_page_links_to_a_section_of_itself_that_moved(self):
        """The split left no bare `#anchor` behind in a moved page."""
        for path in pages(DOCS_ROOT):
            bare = [(t, n) for t, n in links(path) if t.startswith("#")]
            self.assertEqual(
                bare, [],
                "%s keeps a same-page anchor: %s"
                % (os.path.relpath(path, REPO_ROOT), bare))

    def test_every_page_closes_its_code_fences(self):
        unclosed = [os.path.relpath(p, REPO_ROOT)
                    for p in [ROOT_README] + pages(DOCS_ROOT)
                    if unclosed_fence(p)]
        self.assertEqual(unclosed, [], "ends inside a code block: %s" % unclosed)

    def test_every_page_is_reachable_from_the_index(self):
        found = reachable(INDEX, DOCS_ROOT)
        orphans = sorted(
            os.path.relpath(p, REPO_ROOT)
            for p in pages(DOCS_ROOT) if p not in found)
        self.assertEqual(orphans, [], "nothing links to: %s" % orphans)

    def test_the_root_readme_points_into_the_tree(self):
        targets = {t.split("#", 1)[0] for t, _ in links(ROOT_README)}
        self.assertIn("docs/README.md", targets)


class CheckTest(unittest.TestCase):
    """The checks themselves, against a tree built to fail each one."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.docs = os.path.join(self.root, "docs")

    def test_a_link_to_a_missing_file_is_reported(self):
        page = os.path.join(self.docs, "README.md")
        write(page, "# Index\n\n[gone](nowhere.md)\n[here](here.md)\n")
        write(os.path.join(self.docs, "here.md"), "# Here\n")
        self.assertEqual(dangling(page), [("nowhere.md", 3)])

    def test_a_link_to_a_missing_heading_is_reported(self):
        page = os.path.join(self.docs, "README.md")
        write(page, "# Index\n\n[a](one.md#gone)\n[b](one.md#kept-heading)\n")
        write(os.path.join(self.docs, "one.md"), "# One\n\n## Kept heading\n")
        self.assertEqual(missing_fragments(page), [("one.md#gone", 3)])

    def test_a_link_inside_a_code_block_is_not_followed(self):
        page = os.path.join(self.docs, "README.md")
        write(page, "# Index\n\n```\n[sample](nowhere.md)\n```\n")
        self.assertEqual(dangling(page), [])

    def test_a_fence_closes_only_on_the_marker_that_opened_it(self):
        page = os.path.join(self.docs, "README.md")
        write(page, "# Index\n\n~~~\n```\n[sample](nowhere.md)\n~~~\n"
                    "[real](nowhere.md)\n")
        self.assertEqual(dangling(page), [("nowhere.md", 7)])
        self.assertFalse(unclosed_fence(page))

    def test_a_page_that_never_closes_a_fence_is_reported(self):
        page = os.path.join(self.docs, "README.md")
        write(page, "# Index\n\n```\n[sample](nowhere.md)\n")
        self.assertTrue(unclosed_fence(page))

    def test_a_link_to_a_directory_is_reported(self):
        page = os.path.join(self.docs, "README.md")
        write(page, "# Index\n\n[dir](sub)\n")
        write(os.path.join(self.docs, "sub", "two.md"), "# Two\n")
        self.assertEqual(dangling(page), [("sub", 3)])

    def test_an_unlinked_page_is_reported(self):
        write(os.path.join(self.docs, "README.md"), "# Index\n\n[a](one.md)\n")
        write(os.path.join(self.docs, "one.md"), "# One\n")
        write(os.path.join(self.docs, "sub", "two.md"), "# Two\n")
        found = reachable(os.path.join(self.docs, "README.md"), self.docs)
        self.assertEqual(
            sorted(os.path.relpath(p, self.docs) for p in found),
            ["README.md", "one.md"])

    def test_a_page_reached_through_another_page_counts(self):
        write(os.path.join(self.docs, "README.md"), "# Index\n\n[a](one.md)\n")
        write(os.path.join(self.docs, "one.md"), "# One\n\n[b](sub/two.md)\n")
        write(os.path.join(self.docs, "sub", "two.md"), "# Two\n")
        found = reachable(os.path.join(self.docs, "README.md"), self.docs)
        self.assertEqual(len(found), 3)

    def test_headings_slug_the_way_a_renderer_slugs_them(self):
        self.assertEqual(slug("The config directory"), "the-config-directory")
        self.assertEqual(slug("Quick start: run a tong"),
                         "quick-start-run-a-tong")
        self.assertEqual(slug("`settings.json`"), "settingsjson")
        self.assertEqual(slug("Multiple aliases (work/personal)"),
                         "multiple-aliases-workpersonal")
        self.assertEqual(slug("Tongs — sidecar processes"),
                         "tongs--sidecar-processes")


if __name__ == "__main__":
    unittest.main()
