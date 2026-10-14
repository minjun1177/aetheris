"""`update_todos`: the model's checklist, kept by the harness and shown back to it.

A small model loses its place on a long job - does step three twice, or stops
after four - because the list it wrote at the start has scrolled out of what it
attends to. What is checked: the shapes a list arrives in are all read, the
open items are put in front of the model before every request, that reminder
never lands in the stored conversation, and the list lives and dies with the
session.
"""
import asyncio
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOME = tempfile.mkdtemp(prefix="todos-home-")
os.environ["AETHERIS_HOME"] = HOME

from aetheris import config          # noqa: E402
config.MCP_ENABLED = False
config.GIT_AUTO_COMMIT = False
config.AUTO_VERIFY = False
config.CHANNEL_ENABLED = False

from aetheris import app, llm_client, providers, session, todos, tools   # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


print("--- what a list can look like ---")
items = todos.parse("[x] read the parser\n[ ] fix the bug\n- [ ] add a test\n2. [x] run it\n\n")
check("checkbox lines", [(i["text"], i["done"]) for i in items] == [
    ("read the parser", True), ("fix the bug", False), ("add a test", False), ("run it", True)],
      str(items))
check("a bare line is an open item", todos.parse("just this") == [{"text": "just this",
                                                                   "done": False}])
check("a list of strings", [i["done"] for i in todos.parse(["[x] a", "b"])] == [True, False])
objects = todos.parse([{"content": "plan", "status": "completed"},
                       {"text": "build", "done": False}, {"task": "ship"}])
check("objects with a status or a done flag, as other harnesses send them",
      [(i["text"], i["done"]) for i in objects] == [("plan", True), ("build", False),
                                                   ("ship", False)], str(objects))
check("a list is capped", len(todos.parse("\n".join(f"[ ] {n}" for n in range(50))))
      == todos.MAX_ITEMS)

print("\n--- the tool ---")
config.TODOS.clear()
result = tools.dispatch_tool("update_todos", {"todos": "[x] one\n[ ] two\n[ ] three"})
check("it saves the list and says where things stand",
      result.startswith("[Success]") and "1 of 3 done" in result and len(config.TODOS) == 3,
      result)
result = tools.dispatch_tool("update_todos", {"content": "[x] one\n[x] two\n[x] three"})
check("a raw <content> block is the same list", "all done" in result, result)
check("nothing to read is an error, not an empty list",
      tools.dispatch_tool("update_todos", {"todos": "   \n"}).startswith("[Success]")
      and tools.dispatch_tool("update_todos", {"todos": {"weird": 1}}).startswith("[Error]"))

print("\n--- the reminder ---")
config.TODOS[:] = todos.parse("[x] read\n[ ] fix\n[ ] test")
note = todos.reminder()
check("names what is left, and how much", "2 of 3 left" in note and "2. fix" in note
      and "3. test" in note and "read" not in note, note)
config.TODOS[:] = todos.parse("\n".join(f"[ ] step {n}" for n in range(1, 9)))
check("spells out five and counts the rest", "and 3 more" in todos.reminder()
      and "step 6" not in todos.reminder(), todos.reminder())
config.TODOS[:] = todos.parse("[x] a\n[x] b")
check("says nothing once everything is done", todos.reminder() == "")

config.TODOS[:] = todos.parse("[ ] fix")
history = [{"role": "system", "content": "s"}, {"role": "user", "content": "go"}]
sent = todos.with_reminder(history)
check("it rides on the last user message that is sent",
      sent[-1]["content"].startswith("go\n\n[Todo") and len(sent) == 2, sent[-1]["content"])
check("and the history itself is untouched", history[-1]["content"] == "go")
config.TODOS.clear()
check("with nothing to remind, what is sent is the history", todos.with_reminder(history)
      is history)

print("\n--- through a turn ---")


class Planner(providers.Provider):
    name = "planner"
    label = "Planner"
    needs_key = False

    def __init__(self):
        super().__init__({"model": "p-1"})
        self.seen = []
        self.replies = [
            '<tool_call>\n{"name": "update_todos", "arguments": {}}\n'
            "<content>\n[ ] look\n[ ] answer\n</content>\n</tool_call>",
            '<tool_call>\n{"name": "update_todos", "arguments": {}}\n'
            "<content>\n[x] look\n[ ] answer\n</content>\n</tool_call>",
            "Done.",
        ]

    def ready(self):
        return ""

    async def stream(self, messages, max_tokens=None, tools=None):
        self.seen.append(messages[-1]["content"])
        yield {"text": self.replies.pop(0)}
        yield {"done": True, "prompt_tokens": 1, "completion_tokens": 1,
               "total_seconds": 0.0, "eval_seconds": 0.0}


config.TODOS.clear()
planner, saved = Planner(), providers._active
providers._active = planner
messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "do it"}]
quiet, real = io.StringIO(), sys.stdout
sys.stdout = quiet
try:
    asyncio.run(llm_client.chat_turn(messages))
finally:
    sys.stdout = real
    providers._active = saved
check("the first request carries no reminder - there is no list yet",
      "[Todo" not in planner.seen[0], planner.seen[0][-80:])
check("the next one is told what is open", "2 of 2 left" in planner.seen[1], planner.seen[1][-120:])
check("and the one after, what is still open", "1 of 2 left: 2. answer" in planner.seen[2],
      planner.seen[2][-120:])
check("none of it was stored in the conversation",
      not any("[Todo -" in m["content"] for m in messages))

print("\n--- the list belongs to the session ---")
config.SAVE_CHAT_HISTORY = True
config.TODOS[:] = todos.parse("[x] a\n[ ] b")
sid = session.save_session(messages, None)
with open(os.path.join(config.SESSION_DIR, f"{sid}.json"), encoding="utf-8") as handle:
    stored = json.load(handle)
check("it is saved with the session", stored.get("todos") == config.TODOS, str(stored.get("todos")))
config.TODOS.clear()
app._adopt_session(session.load_session(sid))
check("and comes back when it is resumed", [i["text"] for i in config.TODOS] == ["a", "b"],
      str(config.TODOS))
config.TODOS.clear()
sid2 = session.save_session([{"role": "system", "content": "s"}], None)
with open(os.path.join(config.SESSION_DIR, f"{sid2}.json"), encoding="utf-8") as handle:
    check("a session without one is written as it always was", "todos" not in json.load(handle))
config.TODOS[:] = todos.parse("[ ] stale")
app._adopt_session(session.load_session(sid2))
check("and loading it does not inherit the last one's list", config.TODOS == [])
check("it is live state, not a /set setting", "TODOS" not in config.settable())


print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("todo checks passed")
