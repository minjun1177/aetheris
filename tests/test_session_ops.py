"""`/sessions delete` and `/sessions rename`: changing saved conversations by hand.

What is checked: a session is deleted only after a yes, the one on screen is
never deleted out from under it, a name typed into either command cannot reach
a file outside the sessions directory, and a rename moves the file to match
its new title while the conversation open keeps its own.
"""
import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AETHERIS_HOME"] = tempfile.mkdtemp(prefix="sessionops-home-")
from aetheris import config          # noqa: E402
config.SAVE_CHAT_HISTORY = True
config.SESSION_DIR = os.path.join(os.environ["AETHERIS_HOME"], "sessions")
config.MCP_ENABLED = False

from aetheris import app, connect, session   # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


def saved_as(title, text):
    config.SESSION_TITLE = title
    return session.save_session([{"role": "system", "content": "sys"},
                                 {"role": "user", "content": text}], None)


class Chat:
    def __init__(self, session_id=None):
        self.messages = [{"role": "system", "content": "sys"}]
        self.session_id = session_id
        self.mcp_servers = []


def run(line, answers, chat=None):
    """Run `/sessions ...` with `answers` given, in order, to every question."""
    chat = chat or Chat()
    queue = list(answers)
    real = connect._ask
    connect._ask = lambda *a, **k: queue.pop(0) if queue else ""
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            outcome = asyncio.run(app._cmd_sessions(chat, line, line.lower()))
    finally:
        connect._ask = real
    return outcome, out.getvalue(), chat


def ids():
    return {sid for sid, _, _ in session.list_sessions()}


parser = saved_as("Parser rewrite", "rewrite the parser")
deploy = saved_as("Deploy notes", "how do we deploy")
config.SESSION_TITLE = ""

print("--- delete ---")
outcome, shown, _ = run("/sessions delete parser", ["n"])
check("a no keeps it", parser in ids() and "Kept" in shown, shown)
check("and the command is handled, not sent", outcome is app._HANDLED)
outcome, shown, _ = run("/sessions delete parser", ["y"])
check("a yes deletes it", parser not in ids() and "Deleted" in shown, shown)
check("and nothing else", deploy in ids())
_, shown, chat = run("/sessions delete deploy", ["y"], Chat(session_id=deploy))
check("the conversation on screen is not deleted", deploy in ids()
      and "you are in" in shown, shown)
_, shown, _ = run("/sessions delete", ["1", "y"])
check("with no name the list asks which", deploy not in ids(), shown)

print("\n--- a typed name cannot leave the sessions directory ---")
outside = os.path.join(os.environ["AETHERIS_HOME"], "memory.json")
with open(outside, "w") as f:
    f.write("{}")
check("delete_session refuses a path", not session.delete_session("../memory"))
check("and the file is still there", os.path.exists(outside))
check("retitle_saved refuses one too", session.retitle_saved("../memory", "x") == "")
check("an empty id names nothing", not session.delete_session(""))

print("\n--- rename ---")
notes = saved_as("Old notes", "some notes")
config.SESSION_TITLE = "The open one"
_, shown, _ = run("/sessions rename old notes", ["Release checklist"])
renamed = [sid for sid, title, _ in session.list_sessions() if title == "Release checklist"]
check("the session takes the new title", len(renamed) == 1, shown)
check("and its file is named after it", renamed and renamed[0] != notes
      and notes not in ids())
with open(os.path.join(config.SESSION_DIR, f"{renamed[0]}.json"), encoding="utf-8") as f:
    kept = json.load(f)
check("with the conversation inside unchanged",
      kept["messages"][-1]["content"] == "some notes")
check("and the open conversation's title is its own still",
      config.SESSION_TITLE == "The open one", config.SESSION_TITLE)
_, shown, _ = run("/sessions rename release", [""])
check("an empty answer changes nothing", "Kept as it was" in shown
      and renamed[0] in ids(), shown)
open_id = saved_as("Open session", "hello")
_, shown, chat = run("/sessions rename open session", ["Renamed open"], Chat(session_id=open_id))
check("renaming the open one moves the title on screen with it",
      config.SESSION_TITLE == "Renamed open" and chat.session_id != open_id
      and chat.session_id in ids(), shown)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("session operation checks passed")
