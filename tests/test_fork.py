"""`/fork`: carry on in a copy of the conversation, with the original left as it was.

The copy gets its own file and title, the original's file keeps every message
it had and none that came after, and `/load` finds both.
"""
import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AETHERIS_HOME"] = tempfile.mkdtemp(prefix="fork-home-")
from aetheris import config
config.SAVE_CHAT_HISTORY = True
config.SESSION_DIR = os.path.join(os.environ["AETHERIS_HOME"], "sessions")

from aetheris import app, session   # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


class Chat:
    pass


def fork(chat, line):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        outcome = asyncio.run(app._cmd_fork(chat, line, line.lower()))
    return outcome, out.getvalue()


def saved(session_id):
    with open(os.path.join(config.SESSION_DIR, f"{session_id}.json"), encoding="utf-8") as f:
        return json.load(f)


chat = Chat()
chat.messages = [{"role": "system", "content": "sys"}]
chat.session_id = None

print("--- nothing to fork ---")
outcome, printed = fork(chat, "/fork")
check("an empty conversation is not forked", "nothing to fork" in printed
      and chat.session_id is None, printed)

print("\n--- a fork ---")
config.SESSION_TITLE = "Parser rewrite"
chat.messages += [{"role": "user", "content": "rewrite the parser"},
                  {"role": "assistant", "content": "plan A"}]
chat.session_id = session.save_session(chat.messages, None)
original = chat.session_id
outcome, printed = fork(chat, "/fork try plan B")
check("it is handled", outcome is app._HANDLED)
check("the conversation now has a new id", chat.session_id and chat.session_id != original,
      f"{original} -> {chat.session_id}")
check("and the title that was asked for", config.SESSION_TITLE == "try plan B")
check("it says how to go back", f"/load {original}" in printed, printed)
check("the copy holds the conversation so far",
      [m["content"] for m in saved(chat.session_id)["messages"][1:]]
      == ["rewrite the parser", "plan A"])

chat.messages.append({"role": "user", "content": "plan B then"})
session.save_session(chat.messages, chat.session_id)
check("what is said after the fork goes to the copy",
      saved(chat.session_id)["messages"][-1]["content"] == "plan B then")
check("the original keeps what it had, and nothing after",
      [m["content"] for m in saved(original)["messages"][1:]] == ["rewrite the parser", "plan A"]
      and saved(original)["title"] == "Parser rewrite", str(saved(original)["messages"]))
check("/load finds both", {original, chat.session_id} <= {s[0] for s in session.list_sessions()},
      str(session.list_sessions()))

print("\n--- a fork without a title ---")
config.SESSION_TITLE = "Parser rewrite"
fork(chat, "/fork")
check("is named after the original", config.SESSION_TITLE == "Parser rewrite (fork)",
      config.SESSION_TITLE)

print("\n--- when there is nowhere to fork to ---")
config.SAVE_CHAT_HISTORY = False
before = chat.session_id
outcome, printed = fork(chat, "/fork x")
check("with saving off it says so and changes nothing",
      "not being saved" in printed and chat.session_id == before, printed)
config.SAVE_CHAT_HISTORY = True

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("fork checks passed")
