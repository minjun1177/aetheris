"""Hire a second model for one self-contained job, and keep its mess out of here.

The assistant calls `spawn_agent` with a written brief. A fresh conversation is
started - its own system prompt, its own history, its own tool loop - it works
until it has an answer, and only that answer comes back as the tool result.

The point is not that two models are cleverer than one. It is that the twenty
file reads and dead-end greps a search takes are exactly the kind of thing that
fills a context window with material nobody needs again. A sub-agent spends its
own window on them and hands back a paragraph. It can also be pointed at a
different model, so a long mechanical search can run somewhere cheap while the
conversation stays on the good model.

What it may do:

* every tool the assistant has, minus the three in `DENIED` - it must not talk
  to the user directly, must not submit a plan, and by default must not hire
  anyone itself, because a recursive hiring chain is unbounded spend;
* nothing the assistant could not have done. Tool calls go through the same
  `dispatch_tool`, so the same permission rules apply and the same approval
  prompts appear. A sub-agent is not a way around a `deny` rule.

It is deliberately given a turn budget rather than the assistant's "shall I keep
going?" prompt: nobody wants to be asked about the inner workings of a job they
delegated precisely so they would not have to watch it.
"""

import asyncio
import threading

from simple_harness import config
from simple_harness import images
from simple_harness import providers
from simple_harness import toolspec
from simple_harness.config import S, _hr
from simple_harness.systemprompt import tool_rules


# Not offered to a sub-agent, and refused if it asks anyway. The listing and the
# refusal read from this one tuple, so they cannot disagree.
DENIED = ("get_user_input", "submit_plan_for_approval", "spawn_agent")


def _cfg(name, default):
    return getattr(config, name, default)


def withheld(depth: int) -> tuple:
    """The tools this sub-agent does not get. One definition, three users:
    the listing it is given, the schemas it is sent, and the refusal it meets."""
    return tuple(DENIED if depth >= _cfg("SUBAGENT_MAX_DEPTH", 1) else DENIED[:2])


def prompt(depth: int, native: bool | None = None) -> str:
    """The sub-agent's system prompt: the same protocol, a different job."""
    from simple_harness.systemprompt import native_tools_active
    if native is None:
        native = native_tools_active()
    catalogue = ("The tools you can use are supplied with this request. Use the "
                 "names and parameters exactly as they are given there."
                 if native else
                 "### AVAILABLE TOOLS:\n"
                 + toolspec.prompt_schema(exclude=withheld(depth)))
    return f"""\
You are a sub-agent. Another AI is in the middle of a task for a user, hit a
piece of work that stands on its own, and hired you to do just that piece.

You are not talking to the user and never will. Everything you write goes back
to the AI that hired you, as the result of one tool call. So:

- Do the work with your tools. Do not ask for permission or clarification -
  there is nobody to answer, and a question wastes the whole call.
- If the brief is missing something, make the most reasonable assumption, carry
  on, and say in your report which assumption you made.
- When you are done, write your final report as plain text with NO tool call.
  That message is the entire answer the other AI receives, so it must stand on
  its own: what you found, where you found it, and what you could not settle.
  Quote the exact lines, paths and names that matter - the AI reading this
  cannot see any of the tool output you saw.
- Report what you actually found, including "the thing you asked about is not
  there". A confident wrong answer is worse than no answer, because the AI
  reading it has no way to check.

{catalogue}

{tool_rules(native)}
Write your report in the language the brief is written in.
"""


def brief(task: str, context: str = "") -> str:
    parts = [f"### YOUR TASK\n{task.strip()}"]
    if context.strip():
        parts.append(f"### WHAT THE HIRING AI ALREADY KNOWS\n{context.strip()}")
    parts.append("Begin. Use tools as needed, then write your final report.")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------

async def _work(task: str, context: str, depth: int) -> str:
    # Imported here because `tools` imports this module to reach `run`, and at
    # module level that would close a circle.
    from simple_harness.llm_client import (stream_reply, parse_tool_calls, strip_thinking,
                            native_tools, native_enabled, _from_native,
                            _render_call, NATIVE_ERROR)
    from simple_harness.tools import dispatch_tool
    from simple_harness.tui import _fmt_tool_result

    messages = [{"role": "system", "content": prompt(depth, native_enabled())},
                {"role": "user", "content": brief(task, context)}]
    budget = int(_cfg("SUBAGENT_MAX_TURNS", 12))
    last = ""
    nudged = False

    use_native = native_enabled()
    denied = withheld(depth)
    schemas = native_tools(exclude=denied) if use_native else None

    for turn in range(budget):
        calls: list = []
        reply = await stream_reply(messages, tools=schemas,
                                   calls_out=calls if use_native else None)
        last = reply if config.STORE_THINKING else strip_thinking(reply)

        if not last.strip() and not calls:
            # Same trap as the assistant's own loop: a reasoning model can end a
            # turn having only thought. Losing a whole sub-agent to that is
            # expensive, so ask once before giving up on it.
            if not nudged:
                nudged = True
                messages.append({"role": "user", "content": (
                    "[System] Your last reply was empty. Reply now: either the "
                    "<tool_call> you decided on, or your final report.")})
                continue
            return "[The sub-agent reasoned but never wrote a report.]"

        nudged = False
        parsed = _from_native(calls) if calls else parse_tool_calls(reply)
        if calls:
            last = ((last + "\n" if last.strip() else "")
                    + "\n".join(_render_call(c) for c in calls))
        messages.append({"role": "assistant", "content": last})

        if not parsed:
            return last.strip()

        for name, arguments in parsed:
            if NATIVE_ERROR in arguments:
                result = (f"[Error] Your call to '{name}' could not be read: "
                          f"{arguments[NATIVE_ERROR]}. Nothing was run. Try again.")
            elif name in denied:
                # `withheld(depth)`, not `DENIED`: that function exists so the
                # listing, the schemas and the refusal cannot disagree, and
                # this was the one of the three still reading the raw tuple.
                # With SUBAGENT_MAX_DEPTH raised above 1 - it is an ordinary
                # `/set` - a depth-1 sub-agent was handed `spawn_agent` in its
                # prompt and in its schemas and then refused it here, so it
                # spent its budget on a tool it had been told it had.
                result = (f"[System] '{name}' is not available to a sub-agent. "
                          "Finish the work you were given and write your report; "
                          "the AI that hired you will handle the rest.")
            else:
                result = dispatch_tool(name, arguments)
                if result is None:
                    result = (f"[Error] There is no tool named '{name}'. Use only "
                              "the tools listed in your system prompt.")
            # Outside the branch: a call that was refused or could not be read
            # is exactly the one the person watching needs to see, and showing
            # only the results that ran left a sub-agent looking idle while it
            # spent its budget knocking on a door it does not have.
            _fmt_tool_result(name, result)
            # Same arrangement the main loop uses: `view_image` leaves the path
            # behind and it is hung on the result message it belongs to. A
            # sub-agent sent to read a screenshot has to be able to see it.
            step = {"role": "user", "content": f"[Tool Result for '{name}']:\n{result}"}
            looked_at = images.take_pending()
            if looked_at:
                step["images"] = looked_at
            messages.append(step)

    # Out of turns. Ask for the report rather than throwing the work away - a
    # partial answer with its limits stated is still worth having.
    messages.append({"role": "user", "content": (
        f"[System] You have used all {budget} of your turns. Stop calling tools "
        "and write your final report now, from what you already have. Say plainly "
        "which parts you did not get to.")})
    reply = await stream_reply(messages)
    return strip_thinking(reply).strip() or last.strip() or "[No report.]"


def _run_off_thread(task: str, context: str, depth: int, model: str) -> str:
    """Run the sub-agent's loop on its own thread, and wait for it here.

    Tool handlers are ordinary blocking functions called from inside the
    assistant's event loop, so this one cannot simply `asyncio.run` - that loop
    is already running. A private thread gets a private loop, and blocking this
    thread until it finishes is what every other tool does anyway.
    """
    outcome = {}

    def worker():
        try:
            with providers.using_model(model):
                outcome["value"] = asyncio.run(_work(task, context, depth))
        except BaseException as error:      # reported, never raised into the loop
            outcome["error"] = error

    thread = threading.Thread(target=worker, name="subagent", daemon=True)
    thread.start()
    thread.join()

    if "error" in outcome:
        return f"[Error] The sub-agent stopped: {outcome['error']}"
    return outcome.get("value", "[Error] The sub-agent returned nothing.")


def run(task: str, context: str = "", model: str = "") -> str:
    """The `spawn_agent` tool. Returns the sub-agent's report as the tool result."""
    task = (task or "").strip()
    if not task:
        return ("[Error] spawn_agent needs a 'task'. Write the brief as if for "
                "someone who cannot see this conversation.")

    depth = int(_cfg("SUBAGENT_DEPTH", 0))
    limit = int(_cfg("SUBAGENT_MAX_DEPTH", 1))
    if depth >= limit:
        return (f"[System] Sub-agents may not hire sub-agents (depth limit {limit}). "
                "Do this part of the work yourself.")

    label = model or providers.current().model
    _banner("hired", label, task)

    config.SUBAGENT_DEPTH = depth + 1
    try:
        report = _run_off_thread(task, context, depth + 1, model)
    finally:
        config.SUBAGENT_DEPTH = depth

    _banner("reported back", label, "")
    return report


def _banner(state: str, model: str, task: str) -> None:
    print(f"\n  {S.PURPLE}{'┌' if state == 'hired' else '└'}─ sub-agent {state}{S.R}"
          f"  {S.MUTED}{model}{S.R}")
    if task:
        first = task.splitlines()[0]
        print(f"  {S.PURPLE}│{S.R} {S.GRAY}{first[:88]}{'…' if len(first) > 88 else ''}{S.R}")
    print(f"  {_hr(width=60)}")


if __name__ == "__main__":
    print("This file can not run directly.")
