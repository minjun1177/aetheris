"""`/btw`: a question answered from the conversation without becoming part of it.

A side question asked as a message stays in the context for every turn after
it - on a small model's window, a real cost for a passing thought. What is
checked: the conversation is not changed, the request is the conversation plus
the question (so the provider's cache still fits it), the model is told it is
an aside, and a tool it reaches for anyway is named and never run.
"""
import asyncio
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AETHERIS_HOME"] = tempfile.mkdtemp(prefix="btw-home-")

from aetheris import config          # noqa: E402
config.MCP_ENABLED = False
config.CHANNEL_ENABLED = False
config.GIT_AUTO_COMMIT = False

from aetheris import llm_client, providers, tools   # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


class Fake(providers.Provider):
    name = "fake"
    label = "Fake"
    needs_key = False

    def __init__(self, chunks, native=False):
        super().__init__({"model": "f-1"})
        self.chunks = chunks
        self.supports_native_tools = native
        self.sent = None
        self.tools = None

    def ready(self):
        return ""

    async def stream(self, messages, max_tokens=None, tools=None):
        self.sent, self.tools = messages, tools
        for chunk in self.chunks:
            yield chunk
        yield {"done": True, "prompt_tokens": 1, "completion_tokens": 1,
               "total_seconds": 0.0, "eval_seconds": 0.0}


def ask(provider, messages, question):
    saved, providers._active = providers._active, provider
    out, real = io.StringIO(), sys.stdout
    sys.stdout = out
    try:
        answer = asyncio.run(llm_client.side_question(messages, question))
    finally:
        sys.stdout = real
        providers._active = saved
    return answer, out.getvalue()


history = [{"role": "system", "content": "sys"},
           {"role": "user", "content": "refactor the parser"},
           {"role": "assistant", "content": "Working on it."}]
before = [dict(m) for m in history]

print("--- an answer, and nothing kept ---")
fake = Fake([{"text": "It is in context.py."}])
answer, shown = ask(fake, history, "where is compaction done?")
check("the answer comes back", answer == "It is in context.py.", answer)
check("and is shown", "context.py" in shown)
check("the conversation is exactly as it was", history == before)
check("the request is the conversation as it stands, then the question",
      fake.sent[:-1] == before and fake.sent[-1]["role"] == "user"
      and fake.sent[-1]["content"].startswith("where is compaction done?"),
      str(fake.sent[-1]))
check("and says it is an aside, with no tools and no carrying on",
      llm_client.SIDE_NOTE in fake.sent[-1]["content"]
      and "Call no tool" in llm_client.SIDE_NOTE)

print("\n--- a tool reached for is not run ---")
ran = []
real_dispatch = tools._dispatch
tools._dispatch = lambda *a, **k: ran.append(a) or "[Success]"
try:
    fake = Fake([{"text": "Let me look.\n<tool_call>\n"
                          '{"name": "read_file", "arguments": {"path": "a.py"}}\n'
                          "</tool_call>"}])
    answer, shown = ask(fake, history, "what is in a.py?")
    native = Fake([{"text": "Checking."},
                   {"tool_call": {"name": "list_dir", "arguments": {"path": "."}}}],
                  native=True)
    native_answer, native_shown = ask(native, history, "what is here?")
finally:
    tools._dispatch = real_dispatch
check("a text call is named", "read_file" in shown and "/btw runs no tools" in shown, shown)
check("and dropped from the answer", answer == "Let me look.", repr(answer))
check("a native call is named too", "list_dir" in native_shown, native_shown)
check("neither ran", ran == [], str(ran))
check("the tools a turn would offer are offered, so the request matches the cache",
      native.tools == llm_client.native_tools() and native.tools)
check("and the conversation is still untouched", history == before)

print("\n--- a caller that shows the tools itself is handed them ---")
fake = Fake([{"text": "Let me look.\n<tool_call>\n"
                      '{"name": "read_file", "arguments": {"path": "a.py"}}\n'
                      "</tool_call>"}])
saved, providers._active = providers._active, fake
out, real = io.StringIO(), sys.stdout
sys.stdout, reached = out, []
try:
    answer = asyncio.run(llm_client.side_question(history, "what is in a.py?",
                                                  reached_out=reached))
finally:
    sys.stdout, providers._active = real, saved
check("the names land in the list", reached == ["read_file"], str(reached))
check("and are not printed as well", "/btw runs no tools" not in out.getvalue(),
      out.getvalue())


print("\n--- the panel ---")
from aetheris import app, tui   # noqa: E402


def cells(row):
    plain = config._ESCAPE.sub("", row)
    return sum(config.cell_width(c) for c in plain), plain


with config.narrowed(60):
    rows = tui.btw_panel("어디서 compaction을 하나요? " * 3,
                         "**context.py** has it.\n```py\nx = 1\n```\n" + "긴 문장 " * 30,
                         ["read_file"])
widths = {cells(row)[0] for row in rows}
plain = [cells(row)[1] for row in rows]
check("every row of the panel is the same width", widths == {60}, str(widths))
check("it is boxed and titled",
      plain[0].strip().startswith("╭─ /btw") and plain[-1].strip().startswith("╰─"))
check("the question is inside it, above a divider",
      "compaction" in plain[1] and any(p.strip().startswith("├") for p in plain))
check("so is the answer, code block included",
      any("context.py" in p for p in plain) and any("x = 1" in p for p in plain))
check("a tool it reached for is named inside the panel",
      any("read_file" in p and "/btw runs no tools" in p for p in plain))
check("and it says how to dismiss it", tui.BTW_DISMISS in plain[-1])
check("the width override ends with the block", config._WIDTH_OVERRIDE == 0)
with config.narrowed(60):
    left = tui.btw_panel("q", "a", dismiss=False)
check("a panel that will not be waited on does not offer to be dismissed",
      tui.BTW_DISMISS not in left[-1] and "not kept" in left[-1])

print("\n--- /btw draws the panel, and with no keyboard leaves it ---")
real_stdin = sys.stdin
sys.stdin = io.StringIO("")
check("no keyboard means nothing to wait on", tui.wait_to_dismiss() is False)
fake = Fake([{"text": "<think>hmm</think>It is in context.py."}])
saved, providers._active = providers._active, fake
out, real = io.StringIO(), sys.stdout
sys.stdout = out
try:
    asyncio.run(app._btw_panel(history, "where is compaction done?"))
finally:
    sys.stdout, providers._active, sys.stdin = real, saved, real_stdin
shown = out.getvalue()
check("the answer is shown inside the panel",
      "╭─" in shown and "/btw" in shown and "It is in context.py." in shown)
check("its reasoning is not", "hmm" not in shown)
check("it is shown once - not streamed above the panel as well",
      shown.count("It is in context.py.") == 1)
check("and the conversation is still untouched", history == before)

print("\n--- f keeps the aside in the conversation ---")
check("f is the key that keeps it", tui._panel_key("f") == "keep"
      and tui._panel_key("F") == "keep")
check("Esc, Enter, Space and q still dismiss it",
      all(tui._panel_key(k) == "dismiss" for k in ("\x1b", "\r", " ", "q")))
check("any other key does neither", tui._panel_key("x") == "")
sys.stdin = io.StringIO("")
check("with no keyboard there is no key to wait for", tui.wait_for_panel_key() == "")
sys.stdin = real_stdin
check("the panel says f keeps it", "f keeps" in tui.BTW_DISMISS)

config.SAVE_CHAT_HISTORY = True
config.SESSION_DIR = os.path.join(os.environ["AETHERIS_HOME"], "sessions")
from aetheris import session as session_mod, transcript   # noqa: E402


class Chat:
    def __init__(self):
        self.messages = [dict(m) for m in before]
        self.session_id = None


def btw(chat, line, pressed):
    real_panel = app._btw_panel

    async def panel(messages, question):
        return f"answer to {question}", pressed
    app._btw_panel = panel
    out, real = io.StringIO(), sys.stdout
    sys.stdout = out
    try:
        outcome = asyncio.run(app._cmd_btw(chat, line, line.lower()))
    finally:
        sys.stdout = real
        app._btw_panel = real_panel
    return outcome, out.getvalue()


kept_chat = Chat()
outcome, shown = btw(kept_chat, "/btw which file holds the parser?", True)
check("kept: the question and the answer join the conversation",
      kept_chat.messages[-2:] == [
          {"role": "user", "content": "which file holds the parser?"},
          {"role": "assistant", "content": "answer to which file holds the parser?"}],
      str(kept_chat.messages[-2:]))
check("without the note that called it an aside",
      llm_client.SIDE_NOTE not in kept_chat.messages[-2]["content"])
check("it is drawn as a turn, and says it was kept",
      "which file holds the parser?" in shown and "kept" in shown, shown)
check("and saved at once", kept_chat.session_id is not None and
      session_mod.load_session(kept_chat.session_id)["messages"][-1]["content"]
      == "answer to which file holds the parser?")

left_chat = Chat()
outcome, shown = btw(left_chat, "/btw just curious", False)
check("dismissed: the conversation is as it was", left_chat.messages == before)
check("but the session is saved now, not with the next message",
      left_chat.session_id is not None)
record = session_mod.load_session(left_chat.session_id).get("transcript", {})
check("with the aside in its record, for Ctrl+O and /load",
      any(turn["prompt"] == "/btw just curious" for turn in record.get("turns", [])),
      str(record)[:200])
empty = Chat()
empty.messages = [{"role": "system", "content": "sys"}]
btw(empty, "/btw first thing", False)
check("an aside before anything was said leaves no session file", empty.session_id is None)
transcript.reset()
config.SAVE_CHAT_HISTORY = False


print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("btw checks passed")
