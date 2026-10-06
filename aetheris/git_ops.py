"""Commit each AI edit on its own, so any of them can be taken back.

An assistant that edits files is only as usable as its undo. Without one the
honest advice is "commit before you let it touch anything", which nobody
follows, and a wrong edit three tools ago is unrecoverable. With one, a bad
edit costs a single command.

So every file a tool changes is committed by itself, under a message that says
which tool did it:

    ai(edit_file): context.py

`/undo` then takes the top commit back if - and only if - the harness made it.
A commit the user wrote is never touched, and neither is work they have not
committed: `undo` refuses rather than destroying anything it did not create.

Only the paths a tool actually named are committed. Anything else the user has
staged or changed is left exactly as it was, which is why `git commit` is given
those paths explicitly rather than being allowed to sweep up the index.

Stdlib only, and it shells out to `git` rather than taking a dependency: git is
already on any machine this matters on, and its command line is far more stable
than any binding for it.
"""

import os
import subprocess

MESSAGE_PREFIX = "ai("
_repo_root_cache: dict = {}


def _cfg(name, default):
    from aetheris import config
    return getattr(config, name, default)


def _git(*arguments, cwd: str = None) -> subprocess.CompletedProcess:
    """Run git. Never raises: no git, or a directory that has gone, is a "no".

    Everything here is called from inside a tool call, and a machine without
    git installed must still be able to write a file.
    """
    try:
        return subprocess.run(("git",) + arguments, cwd=cwd, capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
    except (OSError, ValueError) as error:
        return subprocess.CompletedProcess(arguments, 1, "", str(error))


def _resolved(path: str) -> str:
    """A path that can be compared with another one.

    `git rev-parse --show-toplevel` reports the working tree with its symlinks
    already resolved; `os.path.abspath` resolves nothing. Comparing the two
    said a file was outside its own repository whenever the project sat behind
    a link - which on macOS is every path under /tmp or /var, both of which are
    symlinks into /private. Auto-commit then returned "" and `/undo` had
    nothing to take back, silently. `normcase` folds the separator and the
    drive-letter case that Windows adds to the same problem.
    """
    return os.path.normcase(os.path.realpath(path))


def _is_inside(path: str, root: str) -> bool:
    """Whether `path` is within the working tree at `root`."""
    try:
        resolved, root = _resolved(path), _resolved(root)
        return os.path.commonpath([resolved, root]) == root
    except ValueError:
        return False        # different drives on Windows - not the same tree


def repo_root(path: str = ".") -> str:
    """The working tree `path` belongs to, or "" if it is not in one."""
    # Not `isfile`: `delete_file` asks about a path that has just stopped
    # existing, and treating that as a directory to run git in fails outright.
    absolute = os.path.realpath(path)
    directory = absolute if os.path.isdir(absolute) else os.path.dirname(absolute)
    if directory in _repo_root_cache:
        return _repo_root_cache[directory]
    result = _git("rev-parse", "--show-toplevel", cwd=directory)
    root = result.stdout.strip() if result.returncode == 0 else ""
    _repo_root_cache[directory] = root
    return root


def enabled() -> bool:
    return bool(_cfg("GIT_AUTO_COMMIT", True))


# ---------------------------------------------------------------------------
# committing
# ---------------------------------------------------------------------------

def auto_commit(paths: list, tool: str, summary: str = "") -> str:
    """Commit exactly `paths`. Returns a short sha, or "" if nothing happened.

    Silent about everything ordinary - not a repository, nothing changed, git
    not installed - because none of those are the user's problem in the middle
    of a tool call. A commit that fails for a real reason returns "" too; the
    edit itself already succeeded and is still on disk.
    """
    if not enabled() or not paths:
        return ""
    root = repo_root(paths[0])
    if not root:
        return ""

    inside = []
    for path in paths:
        absolute = os.path.realpath(path)
        if _is_inside(absolute, root):
            inside.append(os.path.relpath(absolute, os.path.realpath(root)))
    if not inside:
        return ""            # a file outside the repository is not ours to commit

    # `git add` first: a file the tool has just created is untracked, and
    # `git commit <path>` alone will not pick it up. `-A` on the paths given
    # covers a delete as well as a create.
    if _git("add", "-A", "--", *inside, cwd=root).returncode != 0:
        return ""
    if not _git("diff", "--cached", "--quiet", "--", *inside, cwd=root).returncode:
        return ""            # nothing actually changed; a rewrite of the same bytes

    message = f"{MESSAGE_PREFIX}{tool}): {summary or ', '.join(inside)}"
    # --no-verify: a hook that rejects work in progress would strand the edit
    # uncommitted, which is the one state this exists to avoid.
    committed = _git("commit", "--no-verify", "-m", message, "--", *inside, cwd=root)
    if committed.returncode != 0:
        return ""
    return _git("rev-parse", "--short", "HEAD", cwd=root).stdout.strip()


# ---------------------------------------------------------------------------
# undoing
# ---------------------------------------------------------------------------

def last_ai_commit(root: str = "") -> tuple:
    """(sha, subject) if the top commit is one of ours, else ("", "")."""
    root = root or repo_root()
    if not root:
        return "", ""
    result = _git("log", "-1", "--format=%h%x00%s", cwd=root)
    if result.returncode != 0 or "\x00" not in result.stdout:
        return "", ""
    sha, subject = result.stdout.strip().split("\x00", 1)
    return (sha, subject) if subject.startswith(MESSAGE_PREFIX) else ("", "")


def undo_last() -> tuple:
    """Take back the top commit if we made it. Returns (ok, message)."""
    root = repo_root()
    if not root:
        return False, "This is not a git repository, so there is nothing to undo."

    sha, subject = last_ai_commit(root)
    if not sha:
        newest = _git("log", "-1", "--format=%s", cwd=root).stdout.strip()
        if not newest:
            return False, "There are no commits yet."
        return False, (f"The last commit is not one of mine, so I will not touch it:\n"
                       f"  {newest}")

    paths = [line for line in _git("show", "--name-only", "--format=", sha,
                                   cwd=root).stdout.splitlines() if line.strip()]

    # Anything uncommitted in those files was written after the commit - by the
    # user, or by a tool that failed. Undoing would take it with us.
    if paths:
        dirty = _git("status", "--porcelain", "--", *paths, cwd=root).stdout
        if dirty.strip():
            # `XY path`, and X is a space for a change that is not staged - so
            # the line must not be stripped before the path is taken off it.
            changed = ", ".join(line.split(None, 1)[-1]
                                for line in dirty.splitlines() if line.strip())
            return False, (f"'{subject}' touched files that have since changed: "
                           f"{changed}.\nCommit or discard those first - undoing now "
                           f"would take them with it.")

    parent = _git("rev-parse", "--verify", f"{sha}^", cwd=root)
    if parent.returncode != 0:
        return False, (f"'{subject}' is the very first commit in this repository. "
                       "Undoing it would leave no history to go back to.")

    # --soft moves the branch and leaves the AI's version staged; each path is
    # then put back to what the parent commit had. Files the parent never had
    # are removed. Nothing outside `paths` is touched at any point.
    if _git("reset", "--soft", "HEAD~1", cwd=root).returncode != 0:
        return False, "git refused to move the branch; nothing was changed."

    restored, removed = [], []
    for path in paths:
        existed = _git("cat-file", "-e", f"HEAD:{path}", cwd=root).returncode == 0
        if existed:
            _git("checkout", "HEAD", "--", path, cwd=root)
            restored.append(path)
        else:
            _git("rm", "-f", "--quiet", "--cached", "--", path, cwd=root)
            try:
                os.remove(os.path.join(root, path))
            except OSError:
                pass
            removed.append(path)

    detail = []
    if restored:
        detail.append(f"restored {', '.join(restored)}")
    if removed:
        detail.append(f"removed {', '.join(removed)}")
    return True, f"Undid '{subject}'" + (f" - {'; '.join(detail)}." if detail else ".")


def head() -> str:
    """The current commit, or "" outside a repository."""
    root = repo_root()
    if not root:
        return ""
    result = _git("rev-parse", "HEAD", cwd=root)
    return result.stdout.strip() if result.returncode == 0 else ""


def diff_since(sha: str, max_chars: int = 12000) -> str:
    """Everything that has changed since `sha`, as a patch.

    This is what makes a review stage worth having: the model reads what it
    actually wrote rather than what it remembers writing, and those are not
    always the same thing.

    Compared against the working tree rather than HEAD, so a change a command
    made and nothing committed still shows up. A brand new *untracked* file is
    the one thing this misses - git will not diff what it has never seen.
    """
    root = repo_root()
    if not root or not sha:
        return ""
    result = _git("diff", sha, cwd=root)
    if result.returncode != 0:
        return ""
    patch = result.stdout
    if len(patch) > max_chars:
        patch = (patch[:max_chars]
                 + f"\n… (patch truncated at {max_chars} characters; "
                   "use read_file for the rest)")
    return patch


def recent_ai_commits(limit: int = 10) -> list:
    """The AI commits at the top of the branch, newest first."""
    root = repo_root()
    if not root:
        return []
    result = _git("log", f"-{limit}", "--format=%h%x00%s%x00%ar", cwd=root)
    commits = []
    for line in result.stdout.splitlines():
        parts = line.split("\x00")
        if len(parts) == 3 and parts[1].startswith(MESSAGE_PREFIX):
            commits.append({"sha": parts[0], "subject": parts[1], "when": parts[2]})
        elif len(parts) == 3:
            break        # stop at the first commit that is not ours
    return commits


if __name__ == "__main__":
    print("This file can not run directly.")
