"""`/init` and `/add-dir`: what the project is, and where it reaches.

What is checked: `/init` asks the model to study the project and write the
instructions file - the one this directory already has, so it is improved
rather than shadowed, or AGENTS.md - and has the system prompt read again once
that turn is over; `/add-dir` makes a directory count as the project for the
read guard and accept-edits, tells the model, and refuses what is not a
directory or is already inside.
"""
import asyncio
import contextlib
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AETHERIS_HOME"] = tempfile.mkdtemp(prefix="projectdirs-home-")
from aetheris import config          # noqa: E402
config.MCP_ENABLED = False
config.CHANNEL_ENABLED = False
config.SAVE_CHAT_HISTORY = False
config.AUTO_ALLOW = False

from aetheris import app, systemprompt, tools   # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


class Chat:
    def __init__(self):
        self.messages = [{"role": "system", "content": "sys"}]
        self.session_id = None
        self.mcp_servers = []


def run(handler, line, chat=None):
    chat = chat or Chat()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        outcome = asyncio.run(handler(chat, line, line.lower()))
    return outcome, out.getvalue(), chat


project = tempfile.mkdtemp(prefix="projectdirs-")
os.chdir(project)

print("--- /init ---")
outcome, shown, _ = run(app._cmd_init, "/init")
check("it becomes a request to the model", isinstance(outcome, app._Next))
request = outcome[0] if isinstance(outcome, app._Next) else ""
check("to write AGENTS.md when there is no instructions file",
      "`AGENTS.md`" in request and "improve" not in request, request[:120])
check("having looked first, and with the commands to build and test",
      "Look before you write" in request and "test" in request)
check("it says what it is doing", "AGENTS.md" in shown)
check("and the system prompt is read again once that turn ends",
      app._take_refresh_after_turn() is True and app._take_refresh_after_turn() is False)
with open(os.path.join(project, "CLAUDE.md"), "w") as f:
    f.write("# Old notes\n")
outcome, _, _ = run(app._cmd_init, "/init")
check("an instructions file that is there is the one improved, not shadowed",
      "`CLAUDE.md`" in outcome[0] and "already exists" in outcome[0], outcome[0][:160])
outcome, _, _ = run(app._cmd_init, "/init AGENTS.md")
check("one of the names, typed, is the file written",
      "`AGENTS.md`" in outcome[0] and "already exists" not in outcome[0])
outcome, _, _ = run(app._cmd_init, "/init the release process")
check("anything else is what to pay attention to",
      "the release process" in outcome[0] and "`CLAUDE.md`" in outcome[0])
check("the file it writes is one the prompt reads",
      app._INIT_TARGET in systemprompt.CONTEXT_FILES)
app._take_refresh_after_turn()

print("\n--- /add-dir ---")
other = tempfile.mkdtemp(prefix="projectdirs-other-")
secret = os.path.join(other, "notes.txt")
with open(secret, "w") as f:
    f.write("shared notes\n")
check("a file in another directory is outside the project", tools._outside_workspace(secret))
asked = []
real_prompt = tools._approval_prompt
tools._approval_prompt = lambda *a, **k: asked.append(a[0]) or False
try:
    refused = tools._read_outside("read_file", secret)
    check("and reading it is asked about first", refused.startswith("[System]")
          and asked == ["Read Outside The Project"], str(asked))
    outcome, shown, chat = run(app._cmd_add_dir, f"/add-dir {other}")
    check("/add-dir adds it", os.path.realpath(other) in config.ADDED_DIRS, shown)
    check("after which it is part of the project",
          not tools._outside_workspace(secret))
    asked.clear()
    check("and a read there is not asked about",
          tools._read_outside("read_file", secret) == "" and asked == [])
    check("the model is told, with the path",
          chat.messages[-1]["content"].startswith("[System]")
          and os.path.realpath(other) in chat.messages[-1]["content"])
    check("in a message that is not drawn back as something typed",
          chat.messages[-1]["content"].startswith(app._NOT_TYPED))
finally:
    tools._approval_prompt = real_prompt
_, shown, chat = run(app._cmd_add_dir, f"/add-dir {other}")
check("adding it twice says it is already there", "already part" in shown
      and len(config.ADDED_DIRS) == 1 and len(chat.messages) == 1, shown)
_, shown, _ = run(app._cmd_add_dir, f"/add-dir {project}/sub-that-is-not-there")
check("a path that is not a directory is refused", "Not a directory" in shown, shown)
_, shown, _ = run(app._cmd_add_dir, "/add-dir")
check("alone it lists them", os.path.realpath(other) in shown and os.getcwd() in shown, shown)
check("it is not a setting /set would save", "ADDED_DIRS" not in config.settable())
config.ADDED_DIRS.clear()

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("project directory checks passed")
