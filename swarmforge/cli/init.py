"""Create an empty bare repo with an unborn default branch as a worktree.

`swarmforge init PATH [--branch NAME]` creates `PATH/`, initializes the bare
repository `PATH/.git`, and adds `PATH/<branch>` as a linked worktree on a
branch with no commits yet. The worktree path is printed on success.
"""

from swarmforge import worktrees

from .args import nonempty


def configure(parser):
    parser.add_argument("path", metavar="PATH", type=nonempty,
                        help="directory to create")
    parser.add_argument(
        "--branch", metavar="NAME", type=nonempty,
        help="name for the default branch, instead of git's "
             "init.defaultBranch")


def run(options):
    print(worktrees.init(options.path, branch=options.branch))
    return 0
