"""Auto-commit and /undo, against real repositories.

Everything here runs `git` for real in a throwaway repository, because the only
claims worth making are about what git actually does. Two of them matter more
than the rest: a commit takes *only* the files the tool named, and an undo
refuses rather than destroying work it did not create.
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aetheris import config
config.MCP_ENABLED = False
config.SAVE_CHAT_HISTORY = False
config.AUTO_ALLOW = True
config.GIT_AUTO_COMMIT = True

from aetheris import git_ops

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


def git(*args, cwd):
    return subprocess.run(("git",) + args, cwd=cwd, capture_output=True, text=True)


def new_repo():
    root = tempfile.mkdtemp(prefix="gitops-")
    git("init", "-q", "-b", "main", cwd=root)
    git("config", "user.email", "t@t", cwd=root)
    git("config", "user.name", "t", cwd=root)
    write(root, "seed.txt", "seed\n")
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "initial", cwd=root)
    git_ops._repo_root_cache.clear()
    return root


def write(root, name, text):
    path = os.path.join(root, name)
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(name) else None
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def read(root, name):
    with open(os.path.join(root, name), encoding="utf-8") as f:
        return f.read()


def subjects(root, n=5):
    return git("log", f"-{n}", "--format=%s", cwd=root).stdout.split("\n")


print("--- a commit per edit ---")
root = new_repo()
os.chdir(root)
sha = git_ops.auto_commit([write(root, "a.py", "print(1)\n")], "write_file")
check("a new file is committed", bool(sha), repr(sha))
check("the message names the tool", subjects(root)[0].startswith("ai(write_file):"),
      subjects(root)[0])
check("the working tree is clean afterwards",
      git("status", "--porcelain", cwd=root).stdout.strip() == "")

write(root, "a.py", "print(2)\n")
sha2 = git_ops.auto_commit([os.path.join(root, "a.py")], "edit_file")
check("an edit is committed too", bool(sha2) and sha2 != sha)
check("as a separate commit", subjects(root)[0].startswith("ai(edit_file):"))

os.remove(os.path.join(root, "a.py"))
check("a delete is committed", bool(git_ops.auto_commit([os.path.join(root, "a.py")],
                                                        "delete_file")))
check("and the file is gone from the tree",
      git("ls-files", "a.py", cwd=root).stdout.strip() == "")

check("an unchanged file commits nothing",
      git_ops.auto_commit([os.path.join(root, "seed.txt")], "write_file") == "")

print("\n--- it takes only what the tool named ---")
root = new_repo()
os.chdir(root)
write(root, "mine.txt", "the user is editing this\n")
git("add", "mine.txt", cwd=root)                     # staged by the user
write(root, "loose.txt", "and this one is not staged\n")
git_ops.auto_commit([write(root, "ai.txt", "written by a tool\n")], "write_file")

committed = git("show", "--name-only", "--format=", "HEAD", cwd=root).stdout.split()
check("only the tool's file is in the commit", committed == ["ai.txt"], str(committed))
check("the user's staged file is still staged",
      "A  mine.txt" in git("status", "--porcelain", cwd=root).stdout)
check("their unstaged file is still unstaged",
      "?? loose.txt" in git("status", "--porcelain", cwd=root).stdout)

print("\n--- undo ---")
root = new_repo()
os.chdir(root)
git_ops.auto_commit([write(root, "new.py", "created\n")], "write_file")
write(root, "seed.txt", "changed by the ai\n")
git_ops.auto_commit([os.path.join(root, "seed.txt")], "edit_file")

ok, message = git_ops.undo_last()
check("an edit is undone", ok, message)
check("the file is back to what it was", read(root, "seed.txt") == "seed\n",
      repr(read(root, "seed.txt")))
check("the commit is gone", not subjects(root)[0].startswith("ai(edit_file):"))

ok, message = git_ops.undo_last()
check("undoing again reaches the one before", ok, message)
check("a file the AI created is removed",
      not os.path.exists(os.path.join(root, "new.py")), str(os.listdir(root)))
check("and it is out of the index", git("ls-files", "new.py", cwd=root).stdout.strip() == "")
check("the tree is clean", git("status", "--porcelain", cwd=root).stdout.strip() == "",
      git("status", "--porcelain", cwd=root).stdout)

ok, message = git_ops.undo_last()
check("it stops at a commit it did not make", not ok, message)
check("and says whose it was", "not one of mine" in message)

print("\n--- undo refuses to destroy work it did not create ---")
root = new_repo()
os.chdir(root)
git_ops.auto_commit([write(root, "shared.py", "ai version\n")], "write_file")
write(root, "shared.py", "the user has since edited this\n")   # uncommitted
ok, message = git_ops.undo_last()
check("it refuses while that file is dirty", not ok, message)
# The commit subject in the message also ends in "shared.py", so the check has
# to look at the list of changed files specifically.
listed = message.split("since changed: ", 1)[-1].split(".\n")[0]
check("it names the file exactly", listed == "shared.py", repr(listed))
check("and the user's work is untouched",
      read(root, "shared.py") == "the user has since edited this\n")
check("the commit is still there", subjects(root)[0].startswith("ai(write_file):"))

print("\n--- the switch ---")
root = new_repo()
os.chdir(root)
config.GIT_AUTO_COMMIT = False
check("off means off", git_ops.enabled() is False)
check("nothing is committed",
      git_ops.auto_commit([write(root, "b.py", "x\n")], "write_file") == "")
check("the file is still written, just uncommitted",
      os.path.exists(os.path.join(root, "b.py"))
      and "?? b.py" in git("status", "--porcelain", cwd=root).stdout)
config.GIT_AUTO_COMMIT = True
check("on again commits it", bool(git_ops.auto_commit(
    [os.path.join(root, "b.py")], "write_file")))

print("\n--- outside a repository nothing happens ---")
plain = tempfile.mkdtemp(prefix="norepo-")
os.chdir(plain)
git_ops._repo_root_cache.clear()
check("no repository is detected", git_ops.repo_root() == "")
check("committing is a no-op",
      git_ops.auto_commit([write(plain, "c.py", "x\n")], "write_file") == "")
ok, message = git_ops.undo_last()
check("undo says so plainly", not ok and "not a git repository" in message, message)

print("\n--- a file outside the repository is not ours to commit ---")
root = new_repo()
os.chdir(root)
outside = write(plain, "elsewhere.py", "x\n")
check("it is left alone", git_ops.auto_commit([outside], "write_file") == "")

print("\n--- the dispatcher commits, and only on success ---")
root = new_repo()
os.chdir(root)
git_ops._repo_root_cache.clear()
from aetheris import tools

tools.dispatch_tool("write_file", {"filepath": os.path.join(root, "tool.py"),
                                   "content": "print('made by a tool')\n"})
check("a tool's write is committed", subjects(root)[0].startswith("ai(write_file):"),
      subjects(root)[0])
before = subjects(root)[0]
tools.dispatch_tool("read_file", {"filepath": os.path.join(root, "tool.py")})
check("reading commits nothing", subjects(root)[0] == before)
tools.dispatch_tool("write_file", {"filepath": "", "content": "x"})
check("a failed write commits nothing", subjects(root)[0] == before, subjects(root)[0])

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("git checks passed")
