"""`/usage` counts turns, not requests to the model.

One thing the person asks for is answered with as many requests as it takes
tools: the first one, then another after every tool result, six of them under
deepthink, and a whole sub-agent conversation on top. Each of those used to be
its own bar and its own row of numbers, so a single question that took four
tool calls read as five separate questions - and the graph said the
conversation was five times as long as it was.

`config.turn_index` is what groups them back together, and `context.token_turns`
is the only place that reads it. What is checked here is that the grouping is
right, that it survives being saved and loaded, and that a session recorded
before any of this existed still renders.
"""
import asyncio
import io
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aetheris import config
config.MCP_ENABLED = False
config.SAVE_CHAT_HISTORY = False
config.AUTO_ALLOW = True
config.GIT_AUTO_COMMIT = False
config.PERMISSIONS_ENABLED = False
config.NATIVE_TOOLS = False

from aetheris import context, llm_client, providers, tui      # noqa: E402

# Somewhere with no repository in it: the scripted turn below calls git_status
# and git_diff, and running those against this checkout prints its whole diff.
os.chdir(tempfile.mkdtemp(prefix="usage-"))

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


def totals(turns):
    return [(t["requests"], t["prompt"] + t["completion"]) for t in turns]


# ---------------------------------------------------------------------------
print("--- the grouping itself ---")
history = [
    {"prompt": 100, "completion": 10, "turn": 1},
    {"prompt": 200, "completion": 20, "turn": 2},     # one question,
    {"prompt": 300, "completion": 30, "turn": 2},     # two tool calls,
    {"prompt": 400, "completion": 40, "turn": 2},     # three requests
    {"prompt": 500, "completion": 50, "turn": 3},
]
turns = context.token_turns(history)
check("a turn that used tools is one entry, not four",
      totals(turns) == [(1, 110), (3, 990), (1, 550)], str(totals(turns)))
check("and nothing is lost by folding it",
      sum(t["prompt"] + t["completion"] for t in turns)
      == sum(e["prompt"] + e["completion"] for e in history))

# A session written before turns were recorded has nothing to group by. Each
# entry standing alone is the old behaviour, and it is the only honest answer:
# merging neighbours would invent a grouping the file does not support.
old = [{"prompt": 1, "completion": 1}, {"prompt": 2, "completion": 2}]
check("an old session's entries each stand alone",
      totals(context.token_turns(old)) == [(1, 2), (1, 4)],
      str(totals(context.token_turns(old))))
mixed = old + [{"prompt": 4, "completion": 4, "turn": 1},
               {"prompt": 8, "completion": 8, "turn": 1}]
check("and a resumed session's new turns still group",
      totals(context.token_turns(mixed)) == [(1, 2), (1, 4), (2, 24)],
      str(totals(context.token_turns(mixed))))
check("an empty history is no turns", context.token_turns([]) == [])

# ---------------------------------------------------------------------------
print("\n--- a real turn with tool calls in it ---")


class Scripted(providers.Provider):
    """A provider that reads from a script. Yields only the §3 event shape."""
    name, label, default_base_url, needs_key = "scripted", "Scripted", "", False
    supports_native_tools = False

    def __init__(self, replies):
        super().__init__({})
        self.replies = list(replies)
        self.requests = 0

    def list_models(self):
        return []

    async def stream(self, messages, tools=None):
        self.requests += 1
        yield {"text": self.replies.pop(0) if self.replies else "done."}
        yield {"done": True, "prompt_tokens": 1000, "completion_tokens": 100,
               "total_seconds": 0.1, "eval_seconds": 0.05}


config.token_history.clear()
config.turn_index = 0
scripted = Scripted([
    '<tool_call>\n{"name": "git_status", "arguments": {}}\n</tool_call>',
    '<tool_call>\n{"name": "git_diff", "arguments": {}}\n</tool_call>',
    "끝났습니다.",
])
saved = providers._active
providers._active = scripted
config.next_turn()
quiet, real = io.StringIO(), sys.stdout
sys.stdout = quiet
try:
    asyncio.run(llm_client.chat_turn(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "상태 좀"}]))
finally:
    sys.stdout = real
    providers._active = saved

check("the turn took three requests", scripted.requests == 3, str(scripted.requests))
check("and the raw history recorded three", len(config.token_history) == 3,
      str(len(config.token_history)))
turns = context.token_turns()
check("but /usage sees one turn", len(turns) == 1, str(totals(turns)))
check("carrying what the whole question cost",
      totals(turns) == [(3, 3300)], str(totals(turns)))

config.next_turn()
providers._active = Scripted(["짧은 답."])
quiet, real = io.StringIO(), sys.stdout
sys.stdout = quiet
try:
    asyncio.run(llm_client.chat_turn(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "안녕"}]))
finally:
    sys.stdout = real
    providers._active = saved
turns = context.token_turns()
check("the next question is its own turn",
      totals(turns) == [(3, 3300), (1, 1100)], str(totals(turns)))

# ---------------------------------------------------------------------------
print("\n--- the counter survives being put down and picked up ---")
# A resumed conversation that restarted at 1 would drop its first request into
# the same group as an old one, silently merging two questions asked days apart.
config.token_history[:] = [{"prompt": 1, "completion": 1, "turn": 7}]
config.resume_turns()
check("resuming carries on past the loaded turns", config.turn_index == 7,
      str(config.turn_index))
config.next_turn()
check("so the next question is turn 8", config.turn_index == 8, str(config.turn_index))

config.token_history[:] = [{"prompt": 1, "completion": 1}]      # a pre-turn file
config.resume_turns()
check("a history with no turn numbers leaves the counter at zero",
      config.turn_index == 0, str(config.turn_index))

config.token_history.clear()
config.resume_turns()
check("and an empty one does too", config.turn_index == 0, str(config.turn_index))

# ---------------------------------------------------------------------------
print("\n--- what the two displays say about the same turn ---")
config.token_history[:] = [
    {"prompt": 1000, "completion": 100, "turn": 1},
    {"prompt": 2000, "completion": 200, "turn": 1},
]
buf, real = io.StringIO(), sys.stdout
sys.stdout = buf
try:
    tui.display_usage_graph([{"role": "system", "content": "x" * 500}])
    tui._fmt_tokens(2000, 200, 1.0, 0.5, context.token_turns()[-1])
finally:
    sys.stdout = real
printed = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", buf.getvalue())

check("/usage reports one turn", "turns 1 " in printed, printed[printed.find("turns"):][:40])
check("and says it took two requests", "model requests 2" in printed,
      printed[printed.find("model requests"):][:40])
check("the cumulative total is the sum of both", "total 3,300" in printed,
      printed[printed.find("total"):][:40])
check("the end-of-turn line agrees with it",
      "this turn: 2 requests · 3,300 tokens" in printed,
      printed[printed.find("this turn"):][:60])

buf, real = io.StringIO(), sys.stdout
sys.stdout = buf
try:
    tui._fmt_tokens(900, 100, 1.0, 0.5, {"requests": 1, "prompt": 900, "completion": 100})
finally:
    sys.stdout = real
check("but a turn of one request is shown exactly as it always was",
      "this turn" not in re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", buf.getvalue()))

# ---------------------------------------------------------------------------
print("\n--- the two counts on that screen are not the same count ---")
# `_get_conv_pairs` opens a new block at every user message that is not a tool
# result, and the harness writes several of those itself. One question answered
# by deepthink is one turn and seven blocks, and calling both of them "turns"
# on one screen said the conversation was seven times what it was.
from aetheris import deepthink                                # noqa: E402
one_question = [{"role": "system", "content": "s"},
                {"role": "user", "content": "만들어줘"}]
for number, stage in enumerate(deepthink.STAGES, 1):
    one_question += [{"role": "user", "content": f"[Deepthink {number}/6 - {stage.key}]"},
                     {"role": "assistant", "content": "ok"},
                     {"role": "user", "content": "[Tool Result for 'read_file']:\nx"}]
blocks = len(context._get_conv_pairs(one_question))
check("one deepthink question is seven blocks", blocks == 7, str(blocks))

config.token_history[:] = [{"prompt": 8600, "completion": 375, "turn": 1}
                           for _ in range(16)]
buf, real = io.StringIO(), sys.stdout
sys.stdout = buf
try:
    tui.display_usage_graph(one_question)
finally:
    sys.stdout = real
printed = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", buf.getvalue())
check("but it is still one turn", "turns 1 " in printed,
      printed[printed.find("turns"):][:36])
check("and the seven are no longer called turns too",
      "blocks 7" in printed and printed.count("turns") == 1, printed)

# The largest single thing in a short conversation's context, and the screen
# never used to say so - it had to be worked out by hand.
config.NATIVE_TOOLS = False
config.SYSTEM_PROMPT = __import__(
    "aetheris.systemprompt", fromlist=["x"]).systemprompt()
with_prompt = [{"role": "system", "content": config.SYSTEM_PROMPT},
               {"role": "user", "content": "x" * 4000}]
buf, real = io.StringIO(), sys.stdout
sys.stdout = buf
try:
    tui.display_usage_graph(with_prompt)
finally:
    sys.stdout = real
printed = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", buf.getvalue())
check("the fixed per-request overhead is reported", "fixed" in printed,
      printed[printed.find("fixed"):][:80])
# Not asserted as an absolute number of tokens: the estimator calibrates
# itself against whatever the provider last reported, and the scripted one
# above reported figures of its own. What must hold is the shape - over the
# text protocol the whole tool list is in messages[0], and it dominates.
overhead = tui._fixed_overhead(with_prompt)
check("over the text protocol that is messages[0] and nothing else",
      overhead == tui._estimate_tokens(with_prompt[:1]), f"~{overhead:,}")
check("and the tool list is most of what makes it big",
      len(config.SYSTEM_PROMPT) > 20000
      and "read_file" in config.SYSTEM_PROMPT,
      f"{len(config.SYSTEM_PROMPT):,} chars")

# Over a native interface the tool list is not in messages[0] at all - it goes
# in the request's own `tools` field. Counting only messages[0] there would
# report about a third of what the request actually costs.
config.NATIVE_TOOLS = True
native_prompt = __import__(
    "aetheris.systemprompt", fromlist=["x"]).systemprompt()
native = [{"role": "system", "content": native_prompt}]
if llm_client.native_enabled():
    check("a native provider is charged for its schemas too",
          tui._fixed_overhead(native) > tui._estimate_tokens(native) * 2,
          f"{tui._fixed_overhead(native):,} vs messages[0] {tui._estimate_tokens(native):,}")
else:
    print("  [skip] no native provider connected here")
config.NATIVE_TOOLS = False

config.token_history.clear()
config.turn_index = 0

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("usage checks passed")
