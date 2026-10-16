"""Ctrl+O: the last turn again, uncut.

As a turn scrolls past, an argument is cut to 120 characters, a result to 600,
and thinking is hidden unless `SHOW_THINKING` is on - and none of the thinking
is kept in the conversation. What is checked: a turn is recorded whole as it
happens and in order, the record is printed in full, a new turn replaces it,
and Ctrl+O at the prompt reaches the main loop with the half-typed line kept.
"""
import asyncio
import inspect
import io
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AETHERIS_HOME"] = tempfile.mkdtemp(prefix="transcript-home-")

from aetheris import config          # noqa: E402
config.MCP_ENABLED = False
config.CHANNEL_ENABLED = False
config.GIT_AUTO_COMMIT = False
config.SHOW_THINKING = False

from aetheris import app, llm_client, promptkeys, providers, tools, transcript, tui  # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


def printed(function, *args):
    out, real = io.StringIO(), sys.stdout
    sys.stdout = out
    try:
        result = function(*args)
    finally:
        sys.stdout = real
    return result, re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", out.getvalue())


LONG = "\n".join(f"row {n} of a file the model wrote" for n in range(1, 61))
RESULT = "x" * 2000 + "THE END"


class Scripted(providers.Provider):
    name = "scripted"
    label = "Scripted"
    needs_key = False

    def __init__(self):
        super().__init__({"model": "s-1"})
        self.replies = [
            [{"thinking": "the file has to be written whole, " * 10},
             {"text": 'Writing it.\n<tool_call>{"name": "write_file", "arguments": '
                      '{"filepath": "out.txt"}}\n<content>\n' + LONG + "\n</content>\n"
                      "</tool_call>"}],
            [{"text": "Done."}],
        ]

    def ready(self):
        return ""

    async def stream(self, messages, max_tokens=None, tools=None):
        for chunk in (self.replies.pop(0) if self.replies else [{"text": "?"}]):
            yield chunk
        yield {"done": True, "prompt_tokens": 1, "completion_tokens": 1,
               "total_seconds": 0.0, "eval_seconds": 0.0}


print("--- a turn is recorded whole ---")
real_write = tools._handlers()["write_file"]
tools._handlers()["write_file"] = lambda filepath, content: RESULT
saved, providers._active = providers._active, Scripted()
messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "write it"}]
transcript.start()
try:
    _, scrolled = printed(lambda: asyncio.run(llm_client.chat_turn(messages)))
finally:
    providers._active = saved
    tools._handlers()["write_file"] = real_write
kinds = [kind for kind, _, _ in transcript.entries()]
check("thinking, what was said, the call, its result, the answer - in that order",
      kinds == ["thinking", "answer", "call", "result", "answer"], str(kinds))
entries = transcript.entries()
check("the thinking is kept here though it was not shown and is not in the history",
      "written whole" in entries[0][2] and "written whole" not in scrolled
      and not any("written whole" in m["content"] for m in messages))
check("the call has every argument, however long",
      entries[2][1] == "write_file" and entries[2][2].get("content", "").strip() == LONG)
check("the result is all of it", entries[3][2] == RESULT)
check("what scrolled past was cut, and says Ctrl+O has the rest",
      "THE END" not in scrolled and "Ctrl+O" in scrolled, scrolled[-400:])

print("\n--- Ctrl+O prints it ---")
_, shown = printed(transcript.show)
check("the whole result", "THE END" in shown)
check("every row of the long argument", "row 60 of a file" in shown)
check("and the thinking", "written whole" in shown)

transcript.start()
_, shown = printed(transcript.show)
check("a new turn starts a new record", "nothing to show yet" in shown, shown)
_, shown = printed(tui._fmt_tool_call, "read_file", {"filepath": "a" * 300})
check("an argument cut to fit says so", "Ctrl+O" in shown, shown)

print("\n--- from the key to the main loop ---")
if config.PROMPT_TOOLKIT_AVAILABLE:
    binding = next(b for b in promptkeys.bindings().bindings if b.keys[0] == "c-o")

    class Event:
        class current_buffer:
            text = "half a ques"

        class app:
            result = None

            @classmethod
            def exit(cls, result=None):
                cls.result = result

    binding.handler(Event)
    check("Ctrl+O closes the prompt with the line as it stood",
          isinstance(Event.app.result, promptkeys.Expand)
          and Event.app.result == "half a ques")


async def closed_by_ctrl_o(session_pt, message, kept):
    return promptkeys.Expand("half a ques")


real_prompt = app._prompt
app._prompt = closed_by_ctrl_o
try:
    app._kept_typing = ""
    came_back = asyncio.run(app._typed_or_remote(None, ""))
finally:
    app._prompt = real_prompt
check("which the main loop is handed as EXPANDED", came_back is app.EXPANDED)
check("with the half-typed line kept for the prompt that opens next",
      app._kept_typing == "half a ques")
app._kept_typing = ""
source = inspect.getsource(app.main)
check("the main loop prints the record and opens the prompt again",
      "if user_input is EXPANDED:\n            transcript.show()\n            continue" in source)
check("and every turn starts a fresh record",
      source.count("config.next_turn()") == source.count("transcript.start()"))
check("Ctrl+O is in /help all", any(key == "Ctrl+O" for key, _ in tui.KEYS))


print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("transcript checks passed")
