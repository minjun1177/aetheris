"""A model going round in circles is stopped by the harness, not by its budget.

The refusal counter only ever saw one loop: a tool it was not allowed, asked
for again. The commoner one is allowed - the same file read a fifth time, the
same `ls` - and nothing refused it, so it ran until the tool budget asked the
person whether to carry on. Checked here: the third identical call carries a
warning, the fourth is not run, a write in between resets the count (because
"test, fix, test" is progress), polling a running command is left alone, and a
model that keeps going is ended by the same escalation refusals always had.
"""
import asyncio
import io
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("AETHERIS_HOME", tempfile.mkdtemp(prefix="loops-home-"))

from aetheris import config          # noqa: E402
config.MCP_ENABLED = False
config.SAVE_CHAT_HISTORY = False
config.GIT_AUTO_COMMIT = False
config.AUTO_VERIFY = False
config.AUTO_ALLOW = True
config.CHANNEL_ENABLED = False

from aetheris import llm_client, providers, tools   # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


print("--- the guard on its own ---")
guard = llm_client.CallGuard()
read = ("read_file", {"filepath": "/x/a.py"})
results = []
for _ in range(3):
    check_first = guard.screen(*read)
    results.append((check_first, guard.note(*read, "[Success] contents")))
check("the first three run", all(screened is None for screened, _ in results))
check("only the third carries a warning",
      "[System]" not in results[0][1] and "[System]" not in results[1][1]
      and "will be refused" in results[2][1], results[2][1][-120:])
refusal = guard.screen(*read)
check("the fourth is refused, with the reason",
      refusal is not None and refusal.startswith(config.TOOL_REFUSAL_PREFIX)
      and "call number 4" in refusal, str(refusal)[:100])
check("a different argument is a different call",
      guard.screen("read_file", {"filepath": "/x/b.py"}) is None)
check("and the order of arguments does not make it one",
      llm_client.CallGuard._key("t", {"a": 1, "b": 2})
      == llm_client.CallGuard._key("t", {"b": 2, "a": 1}))

guard = llm_client.CallGuard()
test = ("run_cmd", {"command": "pytest -q"})
for attempt in range(5):
    check_it = guard.screen(*test)
    if check_it is not None:
        break
    guard.note(*test, "[Error] 1 failed")
    guard.note("edit_file", {"filepath": "/x/a.py", "new_content": str(attempt)},
               "[Success] File edited: /x/a.py")
check("test, fix, test - five times over - is never refused", check_it is None)

guard = llm_client.CallGuard()
edit = ("edit_file", {"filepath": "/x/a.py", "old_content": "a", "new_content": "b"})
for _ in range(3):
    guard.note(*edit, "[Error] old_content was not found")
check("a failing edit sent again unchanged is caught", guard.screen(*edit) is not None)

guard = llm_client.CallGuard()
write = ("write_file", {"filepath": "/x/a.py", "content": "same"})
for _ in range(3):
    guard.note(*write, "[Success] File written")
check("so is the same write, though it succeeds - it only resets the others",
      guard.screen(*write) is not None)

guard = llm_client.CallGuard()
poll = ("send_input", {"session": "s1", "stdin": ""})
for _ in range(6):
    guard.note(*poll, "[Waiting] still running")
check("listening to a running command again is what send_input is for",
      guard.screen(*poll) is None)

guard = llm_client.CallGuard()
verdicts = [guard.after("[System] denied") for _ in range(6)]
check("three refusals in a row nudge once, three more end the turn",
      verdicts == ["", "", "nudge", "", "", "stop"], str(verdicts))
guard = llm_client.CallGuard()
verdicts = [guard.after(r) for r in ("[System] no", "[Success] ok", "[System] no",
                                     "[System] no")]
check("and a call that went through breaks the run", "nudge" not in verdicts, str(verdicts))


print("\n--- a turn that reads the same file over and over ---")
work = tempfile.mkdtemp(prefix="loops-")
target = os.path.join(work, "a.txt").replace("\\", "/")
with open(target, "w", encoding="utf-8") as handle:
    handle.write("hello\n")

ran = []
real_read = tools.handle_read_file


def counting_read(filepath):
    ran.append(filepath)
    return real_read(filepath)


class Repeats(providers.Provider):
    name = "repeats"
    label = "Repeats"
    needs_key = False

    def __init__(self, replies):
        super().__init__({"model": "r-1"})
        self.replies = list(replies)

    def ready(self):
        return ""

    async def stream(self, messages, max_tokens=None, tools=None):
        yield {"text": self.replies.pop(0) if self.replies else "I give up."}
        yield {"done": True, "prompt_tokens": 1, "completion_tokens": 1,
               "total_seconds": 0.0, "eval_seconds": 0.0}


call = '<tool_call>{"name": "read_file", "arguments": {"filepath": "%s"}}</tool_call>' % target
tools._handlers()["read_file"] = counting_read
saved = providers._active
providers._active = Repeats([call] * 12)
messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "read it"}]
quiet, real = io.StringIO(), sys.stdout
sys.stdout = quiet
try:
    answer = asyncio.run(llm_client.chat_turn(messages))
finally:
    sys.stdout = real
    providers._active = saved
    tools._handlers()["read_file"] = real_read
printed = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", quiet.getvalue())
results = [m["content"] for m in messages if m["content"].startswith("[Tool Result")]
check("the file was read three times, not twelve", len(ran) == 3, str(len(ran)))
check("the third result warned", "will be refused" in results[2], results[2][-100:])
check("the fourth was refused without running", "Not run" in results[3], results[3][:120])
check("and the turn ended before the budget ran out",
      len(results) < 10 and "ending the turn" in printed, f"{len(results)} results")


print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("loop checks passed")
