"""`/compact`: the conversation summarised when the person asks, not only when full.

The summary goes into the system prompt, the last exchange stays as it was, a
focus reaches the summariser, and a summary already there is summarised along
with the rest rather than thrown away.
"""
import asyncio
import contextlib
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AETHERIS_HOME"] = tempfile.mkdtemp(prefix="compact-home-")
from aetheris import config
config.SAVE_CHAT_HISTORY = False

from aetheris import app, context, providers   # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


asked = []
reply = {"text": "SUMMARY-ONE"}


async def fake_complete(messages, max_tokens):
    asked.append(messages[0]["content"])
    return reply["text"]


providers.complete = fake_complete


def conversation():
    return [
        {"role": "system", "content": "You are a helper."},
        {"role": "user", "content": "first question about parsers"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "[Tool Result for 'read_file']:\n1:abc|x = 1"},
        {"role": "assistant", "content": "read it"},
        {"role": "user", "content": "second question"},
        {"role": "assistant", "content": "second answer"},
        {"role": "user", "content": "the latest question"},
        {"role": "assistant", "content": "the latest answer"},
    ]


def run(coroutine):
    with contextlib.redirect_stdout(io.StringIO()):
        return asyncio.run(coroutine)


print("--- the conversation goes into a summary ---")
messages = conversation()
done = run(context.compact(messages))
check("it reports what it did", done is not None and done["pairs"] == 3
      and done["kept"] == 1, str(done))
check("the summary is in the system prompt",
      "<SUMMARY>\nSUMMARY-ONE\n</SUMMARY>" in messages[0]["content"], messages[0]["content"])
check("the system prompt itself is still there", messages[0]["content"].startswith("You are a helper."))
check("the last exchange is kept as it was",
      [m["content"] for m in messages[1:]] == ["the latest question", "the latest answer"],
      str(messages[1:]))
check("and the estimate went down", done["after"] < done["before"], str(done))
check("the summariser saw the whole conversation",
      "first question about parsers" in asked[-1] and "the latest answer" in asked[-1])

print("\n--- a focus reaches the summariser ---")
messages = conversation()
run(context.compact(messages, focus="the parser bug in lexer.py"))
check("the focus is in what the summariser is asked",
      "the parser bug in lexer.py" in asked[-1], asked[-1][-300:])

print("\n--- a second compaction keeps the first ---")
reply["text"] = "SUMMARY-TWO"
messages[1:] = conversation()[1:]
run(context.compact(messages))
check("the earlier summary is given to the summariser", "SUMMARY-ONE" in asked[-1])
check("and replaced by the new one, once",
      messages[0]["content"].count("<SUMMARY>") == 1 and "SUMMARY-TWO" in messages[0]["content"])

print("\n--- nothing to do, or a failed summary, changes nothing ---")
empty = [{"role": "system", "content": "sys"}]
check("an empty conversation is not compacted", run(context.compact(empty)) is None)
reply["text"] = ""
messages = conversation()
before = [dict(m) for m in messages]
check("a summary that failed reports None", run(context.compact(messages)) is None)
check("and leaves the conversation alone", messages == before)
reply["text"] = "S"
one = conversation()[:3]
done = run(context.compact(one))
check("a single exchange is summarised, not kept twice",
      done is not None and done["kept"] == 0 and len(one) == 1, str(one))

print("\n--- /compact is a command ---")
reply["text"] = "FROM-THE-COMMAND"


class Chat:
    pass


chat = Chat()
chat.messages, chat.session_id = conversation(), None
out = io.StringIO()
with contextlib.redirect_stdout(out):
    outcome = asyncio.run(app._cmd_compact(chat, "/compact keep the API names", "/compact keep the api names"))
check("it is handled", outcome is app._HANDLED)
check("the focus is what was typed after it", "keep the API names" in asked[-1])
check("and it says what it did", "Compacted" in out.getvalue(), out.getvalue()[-200:])
check("the summary is in place", "FROM-THE-COMMAND" in chat.messages[0]["content"])

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("compact checks passed")
