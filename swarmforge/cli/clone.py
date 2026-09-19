"""Clone a remote into a bare repo with the default branch as a worktree.

`swarmforge clone URL [PATH] [--branch NAME]` creates `PATH/`, fetches the
remote into `PATH/.git`, and checks the branch out as the linked worktree
`PATH/<branch>`. The worktree path is printed on success.
"""

from swarmforge import worktrees

from .args import nonempty


def configure(parser):
    parser.add_argument("url", metavar="URL", type=nonempty,
                        help="repository to clone")
    parser.add_argument(
        "path", metavar="PATH", nargs="?", type=nonempty,
        help="directory to create; defaults to the name git would pick "
             "for URL")
    parser.add_argument(
        "--branch", metavar="NAME", type=nonempty,
        help="branch to check out, instead of the remote's default")


def run(options):
    dest = options.path or worktrees.guess_clone_dir(options.url)
    print(worktrees.clone(options.url, dest, branch=options.branch))
    return 0
