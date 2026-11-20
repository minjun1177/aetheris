"""`/diff`: what the AI committed this session - listed, one at a time, and net.

Against a real repository: the AI's commits are made by `dispatch_tool` as in
a session, the person's own commit in between is not the AI's to show, and
`/undo` takes a commit out of the list.
"""
import contextlib
import io
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AETHERIS_HOME"] = tempfile.mkdtemp(prefix="diff-home-")
for variable, value in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@example.com"),
                        ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@example.com")):
    os.environ[variable] = value
from aetheris import config
config.SAVE_CHAT_HISTORY = False
config.AUTO_ALLOW = True
config.GIT_AUTO_COMMIT = True
config.AUTO_VERIFY = False
config.CHANNEL_ENABLED = False
config.HOOKS_ENABLED = False

from aetheris import app, git_ops, tools   # noqa: E402

failures = []
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


def shown(argument):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        app._show_diff(argument)
    return ANSI.sub("", out.getvalue())


def git(*arguments):
    subprocess.run(["git", *arguments], check=True, capture_output=True)


origin = os.getcwd()
work = tempfile.mkdtemp(prefix="diff-work-")
os.chdir(work)
git("init", "-q")
with open("base.txt", "w", encoding="utf-8") as handle:
    handle.write("one\n")
git("add", "base.txt")
git("commit", "-q", "-m", "the person's first commit")
git_ops._repo_root_cache.clear()
app._session_start_head = git_ops.head()

print("--- nothing yet ---")
check("a session with no AI commit says so", "committed nothing" in shown(""))

with contextlib.redirect_stdout(io.StringIO()):
    tools.dispatch_tool("write_file", {"filepath": "new.py", "content": "print('a')\n"})
with open("mine.txt", "w", encoding="utf-8") as handle:
    handle.write("the person's\n")
git("add", "mine.txt")
git("commit", "-q", "-m", "the person's second commit")
with contextlib.redirect_stdout(io.StringIO()):
    tools.dispatch_tool("write_file", {"filepath": "base.txt", "content": "one\ntwo\n"})

print("\n--- the list ---")
commits = git_ops.session_ai_commits(app._session_start_head)
check("only the AI's commits, oldest first",
      [c["files"][0][0] for c in commits] == ["new.py", "base.txt"], str(commits))
check("each with its line counts", commits[1]["files"] == [("base.txt", 1, 0)],
      str(commits[1]["files"]))
listing = shown("")
check("/diff lists them numbered", "1." in listing and "2." in listing
      and "new.py" in listing and "+1 -0" in listing, listing)
check("and not the person's commit", "mine.txt" not in listing)

print("\n--- one commit, and the net change ---")
one = shown("2")
check("/diff 2 is that commit's patch", "+two" in one and "new.py" not in one, one)
check("/diff 9 is refused with the range", "1-2" in shown("9"))
net = shown("all")
check("/diff all covers every file the AI touched",
      "new.py" in net and "+two" in net and "+print('a')" in net, net)
check("and only those", "mine.txt" not in net)

print("\n--- /undo takes one out ---")
with contextlib.redirect_stdout(io.StringIO()):
    git_ops.undo_last()
commits = git_ops.session_ai_commits(app._session_start_head)
check("the undone commit is no longer listed",
      [c["files"][0][0] for c in commits] == ["new.py"], str(commits))

print("\n--- outside a repository ---")
plain = tempfile.mkdtemp(prefix="diff-plain-")
os.chdir(plain)
git_ops._repo_root_cache.clear()
check("it says why there is nothing", "not a git repository" in shown(""))

os.chdir(origin)
print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("diff checks passed")
