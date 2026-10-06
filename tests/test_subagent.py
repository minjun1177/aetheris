"""The sub-agent: what it may do, what it may not, and what comes back.

Driven by a scripted provider rather than a real model, so the checks are about
the harness's behaviour and not the model's mood. The one thing that matters
most is the last section: only the report crosses back, never the twenty tool
results the sub-agent waded through to write it.
"""
import io
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aetheris import config
config.AUTO_ALLOW = True
config.MCP_ENABLED = False
config.SAVE_CHAT_HISTORY = False
config.SUBAGENT_MAX_TURNS = 4

from aetheris import providers
from aetheris import subagent
from aetheris import toolspec
from aetheris import tools

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


class Scripted(providers.Provider):
    """Replays a fixed list of replies, and records what it was asked."""
    name = "scripted"
    label = "Scripted"
    needs_key = False

    def __init__(self, replies):
        super().__init__({"model": "scripted-1"})
        self.replies = list(replies)
        self.seen = []

    def ready(self):
        return ""

    async def stream(self, messages, max_tokens=None, tools=None):
        self.tools_seen = tools
        self.seen.append([dict(m) for m in messages])
        reply = self.replies.pop(0) if self.replies else "done."
        yield {"text": reply}
        yield {"done": True, "prompt_tokens": 1, "completion_tokens": 1,
               "total_seconds": 0.0, "eval_seconds": 0.0}


def with_provider(replies, fn):
    saved_state, saved_active = providers._state, providers._active
    scripted = Scripted(replies)
    providers._active = scripted
    quiet, real = io.StringIO(), sys.stdout
    sys.stdout = quiet
    try:
        return fn(), scripted, re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", quiet.getvalue())
    finally:
        sys.stdout = real
        providers._state, providers._active = saved_state, saved_active


work = tempfile.mkdtemp(prefix="subagent-")
open(os.path.join(work, "target.txt"), "w", encoding="utf-8").write("marker=zz-42\n")
os.chdir(work)

print("--- what it is offered ---")
offered = [t["name"] for t in json.loads(toolspec.prompt_schema(exclude=subagent.DENIED))]
check("it gets the ordinary tools", "read_file" in offered and "run_cmd" in offered)
for withheld in subagent.DENIED:
    check(f"it is not offered {withheld}", withheld not in offered)
check("over the text protocol it gets the raw-block rules",
      "<content>" in subagent.prompt(1, native=False))
check("over native tools it does not",
      "<content>" not in subagent.prompt(1, native=True))
check("either way it gets the behaviour rules",
      all("#### DO NOT" in subagent.prompt(1, native=n) for n in (True, False)))
check("and the tool list only when there is no tool interface",
      "AVAILABLE TOOLS" in subagent.prompt(1, native=False)
      and "AVAILABLE TOOLS" not in subagent.prompt(1, native=True))
check("its prompt says it cannot reach the user",
      "never will" in subagent.prompt(1))

print("\n--- a run that uses a tool and reports back ---")
script = [
    '<tool_call>\n{"name": "read_file", "arguments": {"filepath": "target.txt"}}\n</tool_call>',
    "The marker is zz-42, on line 1 of target.txt.",
]
report, scripted, printed = with_provider(script, lambda: subagent.run("find the marker"))
check("the report comes back", "zz-42" in report, repr(report[:60]))
check("only the report comes back", "[Tool Result" not in report)
check("it really called the tool", len(scripted.seen) == 2, f"{len(scripted.seen)} turns")
check("the tool result stayed in the sub-agent's own history",
      any("[Tool Result for 'read_file']" in m["content"]
          for m in scripted.seen[-1]), "")
check("the boundary is marked on screen",
      "sub-agent hired" in printed and "sub-agent reported back" in printed)

print("\n--- it cannot use what it was not offered ---")
script = [
    '<tool_call>\n{"name": "get_user_input", "arguments": {"questions": []}}\n</tool_call>',
    "I could not ask, so I assumed the default.",
]
report, scripted, _ = with_provider(script, lambda: subagent.run("ask the user"))
refusal = [m["content"] for m in scripted.seen[-1] if "[Tool Result" in m["content"]]
check("the call is refused, not run", refusal and "not available to a sub-agent" in refusal[0],
      str(refusal)[:80])
check("and it is told to carry on", "sub-agent" in report or "assumed" in report,
      repr(report[:60]))

# ...and what it *was* offered, it may use. `withheld(depth)` exists so that the
# listing, the schemas and the refusal cannot disagree, and the refusal in the
# loop was the one of the three still reading the raw `DENIED` tuple. With
# SUBAGENT_MAX_DEPTH raised above 1 - an ordinary `/set` - a depth-1 sub-agent
# was handed `spawn_agent` in its prompt and in its schemas and then refused it
# here, so it spent its budget on a tool it had been told it had.
config.SUBAGENT_MAX_DEPTH = 2
try:
    check("with depth to spare, spawn_agent is offered",
          "spawn_agent" not in subagent.withheld(1)
          and "spawn_agent" in subagent.prompt(1, native=False),
          str(subagent.withheld(1)))
    script = [
        '<tool_call>\n{"name": "spawn_agent", "arguments": {"task": "count to one"}}\n</tool_call>',
        "My own sub-agent answered.",
    ]
    _report, scripted, _ = with_provider(script + ["done"],
                                         lambda: subagent.run("delegate again"))
    results = [m["content"] for m in scripted.seen[-1] if "[Tool Result" in m["content"]]
    check("it is not then refused by the loop that offered it",
          results and "not available to a sub-agent" not in results[0],
          str(results)[:90])
finally:
    config.SUBAGENT_MAX_DEPTH = 1
check("and at the depth limit it is withheld again",
      "spawn_agent" in subagent.withheld(1), str(subagent.withheld(1)))

print("\n--- it cannot run forever ---")
loop = ['<tool_call>\n{"name": "git_status", "arguments": {}}\n</tool_call>'] * 8
report, scripted, _ = with_provider(loop + ["Ran out of turns; here is what I have."],
                                    lambda: subagent.run("loop forever"))
check("the turn budget stops it",
      len(scripted.seen) <= config.SUBAGENT_MAX_TURNS + 1,
      f"{len(scripted.seen)} turns for a budget of {config.SUBAGENT_MAX_TURNS}")
check("it is asked for a report rather than cut off",
      any("all 4 of your turns" in m["content"] for m in scripted.seen[-1]))

print("\n--- a model that thinks and says nothing ---")
report, scripted, _ = with_provider(["", "Here is the report after being asked again."],
                                    lambda: subagent.run("say something"))
check("an empty reply is nudged, not accepted",
      "after being asked again" in report, repr(report[:60]))
check("the nudge explains what happened",
      any("Your last reply was empty" in m["content"] for m in scripted.seen[-1]))
check("the empty turn is kept out of the history",
      not any(m["role"] == "assistant" and not m["content"].strip()
              for m in scripted.seen[-1]))

report, _, _ = with_provider(["", ""], lambda: subagent.run("say nothing at all"))
check("twice empty is reported, not returned as silence",
      "never wrote a report" in report, repr(report[:60]))

print("\n--- guards ---")
config.SUBAGENT_DEPTH = 1
check("a sub-agent may not hire one", "may not hire" in subagent.run("recurse"))
config.SUBAGENT_DEPTH = 0
check("an empty brief is refused", "needs a 'task'" in subagent.run("   "))
check("the depth counter is left where it was", config.SUBAGENT_DEPTH == 0)

print("\n--- the assistant's own tool call reaches it ---")
script = ["Nothing to do."]
saved_state, saved_active = providers._state, providers._active
providers._active = Scripted(script)
quiet, real = io.StringIO(), sys.stdout
sys.stdout = quiet
try:
    routed = tools.dispatch_tool("spawn_agent", {"task": "say nothing"})
finally:
    sys.stdout = real
    providers._state, providers._active = saved_state, saved_active
check("spawn_agent dispatches to the sub-agent", routed.strip() == "Nothing to do.",
      repr(routed[:50]))
check("spawn_agent is in the registry", toolspec.get("spawn_agent") is not None)
check("and has a handler", "spawn_agent" in tools._handlers())

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("sub-agent checks passed")
