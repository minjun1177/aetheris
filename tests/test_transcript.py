"""Ctrl+O: the conversation expanded, and collapsed again - a toggle.

As a turn scrolls past, an argument is cut to 120 characters, a result to 600,
and thinking is hidden unless `SHOW_THINKING` is on - and none of the thinking
is kept in the conversation. What is checked: a turn is recorded whole as it
happens and in order, every turn of the conversation is kept, the record is
drawn in full or as it scrolled past, answers as markdown, a redraw records
nothing, `/clear` and a loaded session start it again, and Ctrl+O at the
prompt reaches the main loop with the half-typed line kept.
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

print("\n--- expanded, it is all there ---")
_, shown = printed(transcript.show, True)
check("the whole result", "THE END" in shown)
check("every row of the long argument", "row 60 of a file" in shown)
check("and the thinking", "written whole" in shown)
check("headed as expanded, with the key that collapses it",
      "expanded" in shown and "Ctrl+O collapses" in shown, shown[:200])

print("\n--- collapsed, it is as it scrolled past ---")
_, shown = printed(transcript.show, False)
check("the result cut again", "THE END" not in shown and "chars total" in shown)
check("the thinking a line that says it is there",
      "written whole" not in shown and "thought" in shown, shown[:300])
check("and how to expand it", "Ctrl+O expands" in shown)
check("drawing it recorded nothing", len(transcript.turns()) == 1,
      str(len(transcript.turns())))

print("\n--- every turn, not only the last ---")
transcript.start("and the next thing")
transcript.said("A **bold** answer with `code`.")
check("a new turn is added, not swapped in", len(transcript.turns()) == 2
      and transcript.turns()[0]["entries"], str(len(transcript.turns())))
check("entries() is what the turn that just ran did",
      transcript.entries() == [("answer", "", "A **bold** answer with `code`.")])
_, shown = printed(transcript.show, True)
check("both turns are drawn, each under the line that asked it",
      "THE END" in shown and "❯ and the next thing" in shown, shown[-300:])
check("and an answer is drawn as markdown, not as its asterisks",
      "bold" in shown and "**bold**" not in shown and "`code`" not in shown)

print("\n--- the toggle ---")
transcript.expanded = False
check("pressed once it expands", transcript.toggle() is True)
check("pressed again it collapses", transcript.toggle() is False)
transcript.toggle()
transcript.start("a new message")
check("a new turn is drawn the usual way, so the next press expands",
      transcript.expanded is False)

print("\n--- collapsing puts the screen back as it was ---")
from aetheris import screen     # noqa: E402
transcript.reset()
transcript.start("what does it print")
transcript.thought("a thought that live never showed")
transcript.said("hi bob")
screen.ledger.rows.clear()
live = ("  \033[1m❯\033[0m what does it print\n\n  ─ tokens: 10 in · 2 out\n\n"
        "  hi bob\n\n  ✎ session titled: Greeting\n")
screen.ledger.feed(live)
on_screen = [row.raw for row in screen.ledger.rows]


def press():
    out, real = io.StringIO(), sys.stdout
    sys.stdout = screen.Recorder(out)
    real_unmirrored = app.remote.unmirrored_stdout
    app.remote.unmirrored_stdout = lambda: sys.stdout
    try:
        app._toggle_transcript()
    finally:
        app.remote.unmirrored_stdout = real_unmirrored
        sys.stdout = real
    return out.getvalue()


expanded_view = press()
check("the first press draws it expanded",
      transcript.expanded and "a thought that live never showed" in expanded_view)
collapsed_view = press()
check("the second puts back the rows that were there, and only those",
      [row.raw for row in screen.ledger.rows] == on_screen
      and "a thought that live never showed" not in collapsed_view, collapsed_view)
check("token lines and the title line come back with them",
      "─ tokens: 10 in" in collapsed_view and "session titled" in collapsed_view)
check("no line that was not there: no 'thought' note, no footer",
      "✻ thought" not in collapsed_view and "Ctrl+O expands" not in collapsed_view)
press()
screen.ledger.feed("  ❯ the next question\n")
out, real = io.StringIO(), sys.stdout
sys.stdout = out
try:
    app._leave_expanded("the next question")
finally:
    sys.stdout = real
check("a message sent while expanded goes back to the screen as it was, "
      "with the line just typed under it",
      not transcript.expanded and [row.raw for row in screen.ledger.rows][:-1] == on_screen
      and "the next question" in screen.ledger.rows[-1].raw)
press()
app._leave_expanded(app.RESIZED)
check("a resize while expanded leaves the drawing to the resize redraw",
      not transcript.expanded and app._before_expanding is None)
check("and Ctrl+O's own prompt row is not recorded as a typed line",
      "promptkeys.Expand" in inspect.getsource(app._account_for_prompt))
screen.ledger.rows.clear()
transcript.reset()
transcript.start("a new message")

print("\n--- a redraw records nothing; /clear and a loaded session start again ---")
before = len(transcript.turns())
with transcript.paused():
    printed(tui._fmt_tool_result, "run_cmd", "drawn again")
    printed(tui._fmt_tool_call, "run_cmd", {"command": "ls"})
check("formatters inside paused() add nothing",
      len(transcript.turns()) == before and not transcript.entries())
history = [{"role": "system", "content": "s"},
           {"role": "user", "content": "first question"},
           {"role": "assistant", "content": "first answer"},
           {"role": "user", "content": "second question"},
           {"role": "assistant", "content": "second answer"}]
printed(app._replay_session, history)
check("a resize redraw of the history adds nothing", len(transcript.turns()) == before)
transcript.reset()
check("reset() empties it", transcript.turns() == [] and transcript.expanded is False)
printed(app._replay_session, history, True)
check("a loaded session becomes the record, a turn per question",
      [t["prompt"] for t in transcript.turns()] == ["first question", "second question"]
      and transcript.turns()[1]["entries"] == [("answer", "", "second answer")],
      str(transcript.turns()))
transcript.reset()
_, shown = printed(transcript.show)
check("with nothing recorded it says so", "nothing to show yet" in shown, shown)
_, shown = printed(tui._fmt_tool_call, "read_file", {"filepath": "a" * 300})
check("an argument cut to fit says so", "Ctrl+O" in shown, shown)
transcript.reset()

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
check("the main loop toggles the view and opens the prompt again",
      "if user_input is EXPANDED:\n            _toggle_transcript()\n            continue" in source)
whole = inspect.getsource(app)
paired = len(re.findall(r"config\.next_turn\(\)\n\s*transcript\.start\(", whole))
check("every turn starts a turn of the record",
      whole.count("config.next_turn()") == paired, f"{whole.count('config.next_turn()')} vs {paired}")
check("/clear empties it", "transcript.reset()" in inspect.getsource(app._cmd_clear))
check("loading a session seeds it", whole.count("_replay_session(") and "record=True" in inspect.getsource(app._cmd_load)
      and "record=True" in source)
check("Ctrl+O is in /help all", any(key == "Ctrl+O" for key, _ in tui.KEYS))


print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("transcript checks passed")
