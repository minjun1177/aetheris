"""A git worktree of its own for a sub-agent: `spawn_agent` with `isolation`.

A sub-agent works through the same tools on the same disk, so one sent to try
a refactor changes the files the conversation is standing in - and two ideas
cannot be tried side by side. With `isolation="worktree"` it is given a
checkout of its own instead: a new branch from `HEAD`, checked out under
`~/.aetheris/worktrees/<project>/`, and the sub-agent's working directory for
as long as it works. Its edits are committed there exactly as they would be
here, so `/undo`-style per-edit commits end up on that branch.

When it reports back, what it left is put where the conversation can reach it
and the checkout is taken away:

* nothing changed - the branch and the checkout are both removed, and the
  report says so;
* something changed - anything left uncommitted is committed on the branch,
  the checkout is removed and **the branch is kept**, so `git diff`, `git
  merge` or `git cherry-pick` from the main working tree is how it is used.
  The report names the branch and what it touched.

What the sub-agent does *not* see is anything uncommitted here: a worktree is
made from a commit. The brief is told so, in `note_for_brief`.

Everything goes through `git_ops._git`, which never raises; a failure is a
message, and the sub-agent is then not started at all - a sub-agent asked to
stay apart must not be let loose on the main tree because git said no.
"""

import os
import time

from aetheris import git_ops, paths

BRANCH_PREFIX = "aetheris/agent-"
COMMIT_MESSAGE = "aetheris(spawn_agent): work left uncommitted by the sub-agent"


class Worktree:
    def __init__(self, root: str, path: str, branch: str, base: str):
        self.root = root            # the main working tree
        self.path = path            # the sub-agent's checkout
        self.branch = branch
        self.base = base            # the commit it started from


def create() -> tuple:
    """(Worktree, "") - or (None, why not)."""
    root = git_ops.repo_root(os.getcwd())
    if not root:
        return None, "this directory is not in a git repository, so there is nothing to make a worktree of"
    base = git_ops._git("rev-parse", "HEAD", cwd=root)
    if base.returncode != 0 or not base.stdout.strip():
        return None, "the repository has no commit yet to start a worktree from"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}-{os.getpid()}"
    branch = f"{BRANCH_PREFIX}{name}"
    parent = paths.state("worktrees", paths.workspace_slug(os.path.realpath(root)))
    try:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    except OSError as error:
        return None, f"could not make {parent}: {error}"
    path = os.path.join(parent, name)
    suffix = 1

    def taken() -> bool:
        # The branch as well as the directory: a sub-agent that changed
        # something leaves its branch behind when its checkout is gone, and
        # the next one hired within the same second would be refused by git.
        known = git_ops._git("rev-parse", "--verify", "--quiet",
                             f"refs/heads/{branch}", cwd=root)
        return os.path.exists(path) or known.returncode == 0

    while taken():
        suffix += 1
        path = os.path.join(parent, f"{name}-{suffix}")
        branch = f"{BRANCH_PREFIX}{name}-{suffix}"
    made = git_ops._git("worktree", "add", "-b", branch, path, "HEAD", cwd=root)
    if made.returncode != 0:
        return None, (made.stderr or made.stdout).strip() or "git worktree add failed"
    return Worktree(root, path, branch, base.stdout.strip()), ""


def note_for_brief(tree: Worktree) -> str:
    return (f"You are working in a git worktree of your own at {tree.path}, on "
            f"branch {tree.branch}, made from the last commit. Anything not yet "
            f"committed in the main working tree is not here. Change files here "
            f"freely; the AI that hired you decides afterwards what to take.")


def finish(tree: Worktree) -> str:
    """Put away the checkout and say what it left, for the end of the report."""
    def git(*arguments):
        return git_ops._git(*arguments, cwd=tree.path)

    status = git("status", "--porcelain")
    if status.returncode == 0 and status.stdout.strip():
        git("add", "-A")
        # As `git_ops.auto_commit` commits: unsigned and past the hooks, since
        # this is a checkpoint on a branch nobody has looked at yet.
        committed = git("-c", "commit.gpgsign=false", "commit", "--no-verify", "-q",
                        "-m", COMMIT_MESSAGE)
        # Checked by what is still there, not only by the exit code: the
        # removal below is `--force`, so anything left uncommitted - no git
        # identity on this machine is enough - would go with the checkout.
        after = git("status", "--porcelain")
        if after.returncode != 0 or after.stdout.strip():
            why = (committed.stderr or committed.stdout).strip().splitlines()
            return (f"[Worktree] The sub-agent's changes could not be committed"
                    f"{': ' + why[0] if why else ''}. They were left as they are in "
                    f"{tree.path} on branch {tree.branch}, uncommitted - nothing was "
                    f"deleted. Commit them there, or remove it with: "
                    f"git worktree remove --force {tree.path}")
    counted = git_ops._git("rev-list", "--count", f"{tree.base}..{tree.branch}", cwd=tree.root)
    commits = int(counted.stdout.strip() or 0) if counted.returncode == 0 else 0
    stat = ""
    if commits:
        stat = git_ops._git("diff", "--stat", f"{tree.base}..{tree.branch}",
                            cwd=tree.root).stdout.rstrip()

    removed = git_ops._git("worktree", "remove", "--force", tree.path, cwd=tree.root)
    if removed.returncode != 0:
        git_ops._git("worktree", "prune", cwd=tree.root)
    if not commits:
        git_ops._git("branch", "-D", tree.branch, cwd=tree.root)
        return "[Worktree] The sub-agent changed no files; its worktree and branch were removed."
    lines = [f"[Worktree] The sub-agent's changes are on branch {tree.branch} "
             f"({commits} commit{'s' if commits != 1 else ''} on top of {tree.base[:10]}). "
             f"Nothing in this working tree was changed.",
             f"See them with: git diff {tree.base[:10]}...{tree.branch}",
             f"Take them with: git merge {tree.branch}  (or cherry-pick); "
             f"drop them with: git branch -D {tree.branch}"]
    if stat:
        lines.append(stat)
    return "\n".join(lines)
