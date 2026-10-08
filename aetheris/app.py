import argparse
import asyncio
import sys
import os
import re
import datetime
from aetheris import __version__
from aetheris import config
from aetheris import paths
from aetheris import terms
from aetheris import channel
from aetheris import images
from aetheris import notes
from aetheris import deepthink
from aetheris import git_ops
from aetheris import skills
from aetheris import mcp_client
from aetheris import permissions
from aetheris import providers
from aetheris import connect
from aetheris import remote
from aetheris import qr
from aetheris import mentions
from aetheris import tools
from aetheris import vault
from aetheris import verify
from aetheris import vm
from aetheris.config import S
from aetheris.systemprompt import systemprompt as _build_system_prompt
from aetheris.tui import (_welcome, _show_help, _show_skills, _show_mcp, _show_perms,
                                _show_settings, _fmt_setting, _fmt_tool_call, _fmt_tool_result,
                                display_usage_graph, _hr, complete_command)
from aetheris.renderer import _render_full
from aetheris.session import (save_session, load_session, list_sessions, find_sessions,
                     latest_in_dir, rename_session, generate_session_title, clean_title,
                     memory_prompt_section)
from aetheris.context import (manage_context, _estimate_tokens,
                                    _get_ctx_budget, token_turns)
from aetheris import llm_client
from aetheris.llm_client import chat_turn, parse_tool_calls, strip_thinking


def _compose_system_prompt(summary: str = "") -> str:
    base = config.SYSTEM_PROMPT
    if config.CUSTOM_PERSONA:
        base = config.CUSTOM_PERSONA + "\n\n" + base
    # The memories marked important, and the titles of this project's notes,
    # are read here rather than inside `systemprompt()` for one reason:
    # `config` builds that at import time, and neither `session` nor `notes`
    # can be imported from under it. Composing is where every other
    # per-conversation piece already goes, so they go here too - and they land
    # ahead of the summary, which must stay last for `_extract_summary`.
    # `channel.prompt_section()` belongs here for the same reason, and one of
    # its own: the id does not exist when `config` builds the prompt at import
    # time - this session has not joined a board yet. `main` refreshes once it
    # has, and every later rebuild picks it up from here.
    return (base + memory_prompt_section() + channel.prompt_section()
            + notes.notes_prompt_section() + summary)


def _extract_summary(system_content: str) -> str:
    m = re.search(r'\n\n<SUMMARY>(.*?)</SUMMARY>', system_content, re.DOTALL)
    return f"\n\n<SUMMARY>{m.group(1)}</SUMMARY>" if m else ""


def _refresh_system_prompt(messages: list[dict]) -> None:
    """Rebuild the system message in place, keeping persona and summary intact."""
    summary = _extract_summary(messages[0]["content"])
    config.SYSTEM_PROMPT = _build_system_prompt()
    messages[0]["content"] = _compose_system_prompt(summary)


def _adopt_session(loaded) -> list[dict]:
    """Make a loaded session file the live conversation. Returns its messages.

    `/load` and `--resume` are the same act at different moments, so they are
    the same code: the model, the persona, the token history and the loaded
    skills all belong to the conversation being resumed, not to the one being
    left behind.
    """
    if isinstance(loaded, dict):
        messages = loaded.get("messages", [])
        config.token_history.clear()
        config.token_history.extend(loaded.get("token_history", []))
        config.resume_turns()
        config.MODEL = loaded.get("model", config.MODEL)
        config.CUSTOM_PERSONA = loaded.get("persona", config.CUSTOM_PERSONA)
        config.SESSION_TITLE = loaded.get("title", "")
        config.ATTACCA_SESSION = loaded.get("attacca_session", "")
    else:                                   # a transcript from before version 2
        messages = loaded
        config.token_history.clear()
        config.resume_turns()
        config.SESSION_TITLE = ""
        config.ATTACCA_SESSION = ""
    if not messages or messages[0].get("role") != "system":
        # Every later turn addresses messages[0] as the system message. A file
        # saved with none - or with none left after a repair - must not make the
        # next turn raise.
        config.SYSTEM_PROMPT = _build_system_prompt()
        messages.insert(0, {"role": "system", "content": _compose_system_prompt()})
    config.LOADED_SKILLS[:] = skills.loaded_skill_names(messages)
    config.LOADED_MCP_SERVERS[:] = mcp_client.loaded_in(messages)
    # Resuming is a session start too, so the prompt is rebuilt for the state
    # this conversation is being picked up *in*: the memories marked important
    # since the file was written belong in it, and so do the tools and servers
    # that are attached now rather than the ones that were then. Persona and
    # the `<SUMMARY>` are carried across by `_refresh_system_prompt` itself,
    # and it is called after the two lists above are set so that what it builds
    # matches what was just loaded.
    _refresh_system_prompt(messages)
    return messages


def _replay_session(messages: list[dict]) -> None:
    """Print a resumed conversation the way it looked while it was happening.

    Every field is read defensively. This runs against a file, and a file
    written by another version - or one repaired after a crash - is allowed to
    be missing a `content` that was never optional here. Failing to *replay* a
    conversation must not be what stops it being resumed.
    """
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if not isinstance(content, str):
            content = ""
        if role == "system":
            continue
        elif role == "user":
            if content.startswith("[Tool Result for '"):
                m = re.match(r"\[Tool Result for '([^']+)'\]:\n(.*)", content, re.DOTALL)
                if m:
                    _fmt_tool_result(m.group(1), m.group(2))
                continue
            print(f"  {S.USER_CLR}{S.BOLD}❯{S.R} {content}")
        elif role == "assistant":
            for name, arguments in parse_tool_calls(content, quiet=True):
                _fmt_tool_call(name, arguments)

            c = re.sub(r'<tool_call>.*?</tool_call>', '', content, flags=re.DOTALL)
            c = strip_thinking(c)
            if c:
                print(_render_full(c))
                print()


def _show_ambiguous(query: str, matches: list, hint: str) -> None:
    """Say which sessions a name matched, rather than picking one of them."""
    print(f"  {S.WARN}⚠ '{query}' matches {len(matches)} sessions:{S.R}")
    for sid, title, _ in matches[:10]:
        print(f"  {S.GRAY}•{S.R} {S.WHITE}{title or '(untitled)'}{S.R}  {S.MUTED}{sid}{S.R}")
    print(f"  {S.GRAY}{hint}{S.R}\n")


def _run_user_command(command: str) -> str:
    """Run what the user typed after `!`, with the approval gate lifted.

    The prompt and the permission rules exist to put a person between the model
    and the machine. Here the person *is* the one asking, so making them approve
    their own keystrokes would be theatre. Everything else that makes `run_cmd`
    survivable is exactly what is wanted - the idle detection, the timeout, the
    output trimming, the session left registered when a program stops at a
    prompt - so this is `safe_run_cmd` with only the gate held open, the same
    way `dispatch_tool` holds it open for an allow rule.
    """
    config.POLICY_AUTO_ALLOW = True
    try:
        return tools.safe_run_cmd(command)
    finally:
        config.POLICY_AUTO_ALLOW = False


def _attach_mentions(text: str) -> tuple[str, list]:
    """Expand `@path` in a typed message and say what came with it.

    The model is never told a mention failed by silence: a path that is not
    there is named on screen, and the message goes as written. Returns the
    message and the image paths to hang on it - an image is not turned into
    text, so it travels beside the sentence rather than inside it.
    """
    expanded, notes, pictures = mentions.expand(text)
    for path, attached, detail in notes:
        if attached:
            print(f"  {S.MUTED}◆ attached {S.WHITE}{path}{S.R} {S.GRAY}({detail}){S.R}")
        else:
            print(f"  {S.WARN}⚠ @{path}: {detail}{S.R}")
    if pictures:
        pictures = _images_this_model_can_see(pictures)
    if notes:
        print()
    return expanded, pictures


def _images_this_model_can_see(pictures: list) -> list:
    """`pictures`, or none of them plus an explanation on screen.

    Told before the request rather than after it. A model that cannot see gets
    no error from anybody: the hosted APIs would reject it loudly, but a local
    model without `vision` has the image dropped by Ollama and answers about
    the sentence alone - confidently, and about nothing. That is the one
    failure worth spending a capability probe to avoid.
    """
    provider = providers.current()
    if provider.sees_images():
        return pictures[:config.IMAGE_MAX_PER_MESSAGE]
    plural = "s were" if len(pictures) != 1 else " was"
    print(f"  {S.WARN}⚠ {provider.model or provider.label} cannot see images; "
          f"{len(pictures)} image{plural} left off.{S.R}")
    if provider.name == "ollama":
        able = providers.ollama_vision_models(provider.host)
        if able:
            print(f"  {S.MUTED}  installed models that can: "
                  f"{', '.join(able[:5])}{S.R}")
    print(f"  {S.MUTED}  The message itself was sent as written.{S.R}")
    return []


def _connect_mcp_servers() -> list:
    """Bring up the configured MCP servers. Returns the ones that failed.

    This runs before the first system prompt is built, because the servers'
    tool lists are part of it.
    """
    if not config.MCP_ENABLED:
        return []
    pending = [s for s in mcp_client.load_servers().values() if s.state != "disabled"]
    if not pending:
        return []

    plural = "s" if len(pending) != 1 else ""
    sys.stdout.write(f"  {S.MUTED}⟳ connecting {len(pending)} MCP server{plural}…{S.R}")
    sys.stdout.flush()
    try:
        mcp_client.connect_all()
    finally:
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()
    return [s for s in pending if s.state == "failed"]


def _wrapped(text: str, indent: int = 6) -> list:
    """A message broken to the terminal's width, so none of it is lost."""
    import textwrap
    width = max(30, config.tw() - indent)
    lines = []
    for paragraph in str(text or "").splitlines() or [""]:
        lines.extend(textwrap.wrap(paragraph.strip(), width) or [""])
    return lines


def _report_mcp_problems(failed: list) -> None:
    for problem in getattr(mcp_client.load_servers, "errors", []):
        print(f"  {S.ERR}✗ {problem}{S.R}")
    for server in failed:
        # Wrapped rather than cut at 200: what a server says when it will not
        # start is a missing binary, a bad token or a path that is not there,
        # and all three read at the end of the sentence.
        print(f"  {S.WARN}⚠ MCP server '{server.name}' failed:{S.R}")
        for line in _wrapped(server.error):
            print(f"  {S.MUTED}  {line}{S.R}")
    if failed:
        print(f"  {S.MUTED}  Run {S.ACCENT}/mcp{S.MUTED} for details, {S.ACCENT}/mcp reload{S.MUTED} to retry.{S.R}\n")


def _report_strays() -> None:
    """Point out state an older version wrote into this directory.

    Named, never touched. `sessions` and `memory.json` are ordinary enough
    names that moving one on sight would eventually take somebody's real work
    with it - so this says what it found and what to type, and stops there.
    """
    strays = paths.strays_in_cwd()
    if not strays:
        return
    print(f"  {S.MUTED}\u25c6 {', '.join(strays)} here look like state from an "
          f"older version.{S.R}")
    print(f"  {S.MUTED}  It now lives in {paths.home()}. Nothing has been moved; "
          f"to move it:{S.R}")
    print(f"  {S.GRAY}    mv {' '.join(strays)} {paths.home()}/{S.R}\n")


def _agent_label() -> str:
    """How this session appears to the other agents: its model, and its subject."""
    if config.SESSION_TITLE:
        return f"{config.MODEL} - {config.SESSION_TITLE}"
    return config.MODEL


def _report_agents(agent_id: str) -> None:
    """Say who else is already working here. Silent when nobody is.

    A solo session should not be told about a feature it has no use for, and a
    second terminal opened on the same project should not have to go looking to
    find out it is not alone.
    """
    if not agent_id:
        return
    others = channel.peers()
    if not others:
        return
    print(f"  {S.INFO}◆ You are agent {S.BOLD}{agent_id}{S.R}{S.INFO} here. "
          f"{len(others)} other agent(s) working in this project:{S.R}")
    for record in others:
        holds = ", ".join(record.get("holds") or [])
        print(f"  {S.GRAY}•{S.R} {S.WHITE}{record['id']}{S.R} "
              f"{S.MUTED}{record.get('label') or 'unknown model'}"
              f"{'  holding ' + holds if holds else ''}{S.R}")
    print(f"  {S.MUTED}  They can see you too. {S.GRAY}/agents{S.MUTED} for the "
          f"board.{S.R}\n")


def _close_menu_when_unwanted(buffer) -> None:
    """Shut the completion menu the moment the line stops asking for one.

    A `Condition` decides whether a menu is coming; a completion *state* is
    whether one is open. The reserved rows answer to either, so a menu left
    open over a line that no longer starts with `/` keeps the band under the
    prompt after the reason for it has been deleted.
    """
    try:
        if buffer.complete_state is not None and not config.COMPLETE_WHILE_TYPING():
            buffer.cancel_completion()
    except Exception:
        pass          # a menu is a convenience; it never stops typing


def _report_usage(messages: list[dict]) -> None:
    """Tell the remote what this conversation costs now.

    The phone has no `/usage` and no room for one, but it does have room for a
    number above the box - and "how much of the window is left" is the thing
    somebody driving from a train most wants and least can ask for.
    """
    try:
        remote.set_usage(_estimate_tokens(messages), _get_ctx_budget(),
                         len(token_turns()))
    except Exception:
        pass          # a number on a strip is never worth an exception


# Commands the link does not get to run. One entry, and the reasoning is the
# whole of it: everything else the remote can type it can also undo from there,
# and this is the one that takes away the thing it would need to.
_NOT_FROM_THE_REMOTE = ("/exit", "/quit")


def _refused_from_remote(cmd: str) -> bool:
    """Is this a command the phone may not run? Says why, on both screens."""
    if cmd not in _NOT_FROM_THE_REMOTE or not remote.driven():
        return False
    print(f"  {S.WARN}◆ {cmd} is not taken from the remote.{S.MUTED} Closing the "
          f"session from a phone leaves the phone with nothing to reconnect to - "
          f"and this terminal with a prompt nobody asked to leave.{S.R}")
    print(f"  {S.MUTED}Type it at the keyboard, or {S.GRAY}/remote off{S.MUTED} "
          f"to hand the session back to it.{S.R}\n")
    return True


def _print_above(text: str) -> None:
    """Print a line that arrives while a prompt is open.

    One `print`, and one place saying why it is one `print`: what makes this
    work is `patch_stdout(raw=True)` in `_read_line`, without which everything
    routed through here loses its escapes to prompt_toolkit's sanitising and
    arrives as `?[38;2;250;189;47m…`. Everything that prints above a live
    prompt - another agent's message, the remote's notices, the echo of a line
    typed there - comes through here, so that reason is written down once.
    """
    print(text)


def _show_remote_notices() -> None:
    """Who has been at the remote's door, printed as soon as the prompt is free.

    Written by the server's own threads and read here, exactly as the channel's
    messages are: a line printed from a request handler would land in the
    middle of a streaming answer.

    `◆` rather than anything more evocative: this is read on whatever terminal
    the person has, and Windows Terminal's default font has no glyph for U+26BF
    - the "squared key" that was here first came out as a replacement box.
    Geometric Shapes is the block everything else in this interface draws from
    for that reason. "Somebody opened the link" and "somebody
    tried a wrong token" are both things a person only finds useful while they
    can still act on them, which is at a prompt.
    """
    try:
        for text in remote.take_notices():
            _print_above(f"  {S.WARN}◆ {text}{S.R}")
    except Exception:
        pass          # the door is a convenience; it never stops the prompt


def _show_arrivals() -> list:
    """Print what other agents have said, as soon as the terminal is free.

    The model gets the same messages at the start of its next turn - the two
    have separate cursors on purpose, because a message the person has read on
    screen has not yet been read by the model, and the other way round.

    Returns what it printed, because the caller has one more decision to make
    about it: whether any of it should start a turn on its own.
    """
    arrived = []
    try:
        arrived = channel.take_for_screen()
        for entry in arrived:
            _print_above(f"  {S.PURPLE}✉ {channel.describe(entry)}{S.R}")
    except Exception:
        pass          # the board is a convenience; it never stops the prompt
    return arrived


# Turns the channel has started by itself since a person last typed anything.
# The budget is what stops two idle agents answering each other until morning;
# see `_auto_turn_armed`.
_auto_turns = 0
_auto_woke = False          # the line about to be returned is the channel's, not a person's
_auto_spent_said = False    # the budget has run out, and that was said once
# Direct questions that have been printed but not yet acted on. They are kept
# rather than answered on the spot because the two are not the same moment: the
# arrivals are drained just *before* the prompt opens, when there is no running
# application to exit, and again from the watcher once there is. Without this,
# a question that landed in the gap between a turn ending and the prompt opening
# would be printed and then waited on forever.
_auto_asked_by: list = []


def _auto_cap() -> int:
    """How many fruitless auto-turns are allowed. 0 is no ceiling."""
    return max(0, int(getattr(config, "CHANNEL_AUTO_TURN_MAX", 3) or 0))


def _auto_turn_armed() -> bool:
    """Whether an arriving question may press Enter on this idle terminal."""
    if not config.CHANNEL_ENABLED or not getattr(config, "CHANNEL_AUTO_TURN", True):
        return False
    cap = _auto_cap()
    return cap == 0 or _auto_turns < cap


def _auto_request(who: str) -> str:
    """The line the channel types, when it types one.

    Deliberately not the message itself: the message is already going in ahead
    of this, through `turn_note` and the model's own cursor, and putting it in
    twice would be two versions of one thing to keep in step. What this adds is
    the part a typed line carries and a delivered message does not - that it is
    being asked for *now*, and how far it is allowed to go.
    """
    return (f"[Channel] {who} messaged you directly and there is nobody at this "
            f"keyboard, so the channel pressed Enter for them. Their message is "
            f"just above. Answer it with send_agent_message (kind='answer'), "
            f"and if they asked "
            f"you to do something, DO IT yourself now rather than asking them to "
            f"- releasing a file you hold, or saying plainly what stops you. Do "
            f"not ask them a question back and do not report anything as done "
            f"that you have not done. Then stop: this is somebody else's "
            f"question, not a new job, and nobody is here to approve one.")


def _maybe_auto_turn(session_pt, arrived: list) -> bool:
    """Press Enter for a question that arrived at a terminal nobody is sitting at.

    This is the other half of the channel actually working. A message delivered
    into the context of a session that is idle at its prompt is delivered to
    nothing: the model does not read it until the *next* turn, and the next turn
    happens when a person comes back and types. So the agent that asked "are you
    finished with shared.py?" waits for somebody's lunch to end.

    Three things keep it from being a nuisance:

    - **only a direct message.** A broadcast is news, and in a project with six
      agents every one of them would wake and answer the same one;
    - **only an empty prompt.** A half-typed line is that person's, and exiting
      the prompt would throw it away without them ever seeing why;
    - **only `CHANNEL_AUTO_TURN_MAX` times** between one human line and the
      next. Two idle agents are a loop, and a loop that runs on a hosted model
      runs on somebody's money.

    Without `prompt_toolkit` there is no way in: `input()` cannot be interrupted,
    so the message waits for the next Enter exactly as it did before.
    """
    global _auto_turns, _auto_woke, _auto_spent_said
    for entry in arrived:
        # `wakes` is the second half of "only a direct message": an `answer` or
        # a `done` is information, and starting a turn on a terminal nobody is
        # sitting at to read information is how an unattended session ends up
        # holding a conversation instead of doing a job.
        if (channel.is_direct(entry) and channel.wakes(entry)
                and entry.get("from") not in _auto_asked_by):
            _auto_asked_by.append(entry["from"])
    if not _auto_asked_by or session_pt is None:
        return False
    if not _auto_turn_armed():
        # Said once, not every time: an agent being talked at is exactly the
        # situation where the reminder would repeat until it was noise.
        if getattr(config, "CHANNEL_AUTO_TURN", True) and not _auto_spent_said:
            _auto_spent_said = True
            print(f"  {S.MUTED}↪ {_auto_cap()} turns answered without you and "
                  f"nothing done in any of them; this one waits. Press Enter to "
                  f"take it.{S.R}")
        # Dropped rather than held: the person has it on screen now, and the
        # model gets it from its own cursor on whatever turn comes next. Holding
        # it would only mean a queue that grows for as long as nobody is here.
        _auto_asked_by.clear()
        return False

    prompt_app = getattr(session_pt, "app", None)
    if prompt_app is None or not getattr(prompt_app, "is_running", False):
        return False
    try:
        if prompt_app.current_buffer.text.strip():
            return False        # somebody is typing; the line is theirs
    except Exception:
        return False

    who = ", ".join(sorted(_auto_asked_by))
    _auto_turns += 1
    _auto_woke = True
    cap = _auto_cap()
    print(f"  {S.INFO}◆ {who} asked something and nobody is here; answering it "
          f"{S.MUTED}({_auto_turns}/{cap if cap else '∞'}){S.R}")
    try:
        prompt_app.exit(result=_auto_request(who))
    except Exception:
        _auto_turns -= 1
        _auto_woke = False
        return False
    _auto_asked_by.clear()
    return True


async def _watch_channel(session_pt=None) -> None:
    """While a prompt is waiting for a line, watch for another agent's message.

    Without this a reply only appears after the person presses Enter, which for
    the agent that asked the question means the answer arrives at some point
    after it stopped being useful. It runs only for as long as the prompt is
    open, so nothing here can print over a streaming answer.
    """
    while True:
        await asyncio.sleep(max(0.5, float(getattr(config, "CHANNEL_POLL_SECONDS", 2))))
        try:
            channel.heartbeat(_agent_label())
            arrived = _show_arrivals()
            _show_remote_notices()
        except Exception:
            return
        try:
            if _maybe_auto_turn(session_pt, arrived):
                return          # the prompt is closing; this line is answered
        except Exception:
            return              # never let the watcher be the reason a prompt dies


# One box takes two completely different things: a message for the model, and -
# behind a `!` - a command for this machine. They used to look identical while
# being typed, and the first sign that a line was a shell command was the shell
# command running. So the box says which it is, before Enter rather than after.
SHELL_STYLE = "fg:#fabd2f"          # S.WARN, in the spelling prompt_toolkit takes


def _prompt_message() -> str:
    """The prompt, which says `Shell` over itself while a `!` is being typed."""
    if getattr(config, "typing_shell", None) and config.typing_shell():
        return (f"  {S.WARN}{S.BOLD}Shell{S.R}{S.MUTED} - runs on this machine as "
                f"you; not sent to the model{S.R}\n"
                f"  {S.WARN}{S.BOLD}❯{S.R} ")
    return f"  {S.USER_CLR}{S.BOLD}❯{S.R} "


async def _remote_line() -> str:
    """A line typed on the remote, as soon as there is one."""
    while True:
        await asyncio.sleep(0.25)
        line = remote.take_line()
        if line:
            return line


def _echo_remote(line: str) -> str:
    """Print a remotely typed line where a typed one would have been.

    The person at this keyboard has to be able to read the transcript
    downwards and see what was asked, even when it was asked from a train.
    """
    _print_above(f"  {S.USER_CLR}{S.BOLD}❯{S.R} {line}  {S.MUTED}(from the remote){S.R}")
    remote.set_driver("remote")
    return line


# What `_read_line` returns when the prompt closed for a call from Attacca rather
# than for a line: the main loop serves it - outside `patch_stdout`, where an
# approval prompt reads the keyboard the ordinary way - and opens the prompt
# again without starting a turn.
IDLE_CALL = object()
_kept_typing = ""             # half a line, set aside while a call was served


def _attacca_listening() -> bool:
    """Whether a call from Attacca can arrive while the prompt is open."""
    if not providers.current().drives_turns:
        return False
    from aetheris import attacca
    return attacca.connected()


async def _attacca_call() -> bool:
    """Resolves once a call from Attacca is waiting. Looks; never takes one.

    Taking it here would lose it whenever the keyboard won the same race - the
    call would be off the queue with nobody left to answer it. So this only
    sees that something is there, and `attacca.serve_waiting` takes it.
    """
    from aetheris import attacca
    while not attacca.call_waiting():
        await asyncio.sleep(attacca.IDLE_POLL)
    return True


def _serve_idle_calls() -> None:
    """Run what Attacca's agent asked for while the prompt was open."""
    from aetheris import attacca
    try:
        attacca.serve_waiting()
    except KeyboardInterrupt:
        print(f"\n  {S.GRAY}Stopped - the agent was told the call was cancelled.{S.R}")
    except Exception as error:
        connect._print_problem("Could not serve a call from Attacca", error)
    print()


async def _typed_or_remote(session_pt, message) -> str:
    """Whichever comes first: a line typed here, or one typed on the remote.

    The prompt is cancelled when the remote wins, which costs whatever was
    half-typed at this keyboard. That is the right way round: a person who is
    at the keyboard is the one who can see the line disappear and type it
    again, and the alternative - holding the remote's line until somebody
    presses Enter here - is exactly the hang the remote exists to avoid.
    """
    global _kept_typing
    kept, _kept_typing = _kept_typing, ""
    # `default` only when there is something to put back, so every prompt that
    # never met an Attacca call opens exactly the way it always did.
    typed = asyncio.ensure_future(session_pt.prompt_async(message, default=kept)
                                  if kept else session_pt.prompt_async(message))
    called = (asyncio.ensure_future(_attacca_call()) if _attacca_listening()
              else None)
    if not remote.running() and called is None:
        return (await typed).strip()

    waiting = asyncio.ensure_future(_remote_line()) if remote.running() else None
    contestants = {typed} | {c for c in (waiting, called) if c is not None}
    done, _ = await asyncio.wait(contestants, return_when=asyncio.FIRST_COMPLETED)
    if typed in done:
        for other in (waiting, called):
            if other is not None:
                other.cancel()
        if waiting is not None:
            remote.set_driver("terminal")
        return typed.result().strip()

    if called is not None and (waiting is None or waiting not in done):
        # Attacca's agent wants a tool run, and nobody here asked it anything.
        # Whatever was half-typed is kept and put back when the prompt reopens -
        # it was not this person's mistake that the box closed under them.
        _kept_typing = session_pt.default_buffer.text
        if waiting is not None:
            waiting.cancel()
        typed.cancel()
        try:
            await typed
        except (asyncio.CancelledError, Exception):
            pass
        return IDLE_CALL

    if called is not None:
        called.cancel()
    typed.cancel()
    try:
        await typed
    except (asyncio.CancelledError, Exception):
        # The CancelledError is the one just asked for; anything else is
        # `prompt_toolkit` objecting to being stopped mid-line, which is not
        # worth losing the line that arrived over.
        pass
    return _echo_remote(waiting.result())


async def _read_line(session_pt) -> str:
    """One line from the person - or from the remote, or from the channel.

    Three things can put a line here and only one of them is somebody typing at
    this keyboard. `_watch_channel` exits the prompt when another agent asks a
    direct question and the box is empty; `_typed_or_remote` returns whatever a
    phone sent, whichever arrived first. `_auto_woke` says which of the three
    happened, and that is what refills the auto-turn budget - a person typing
    anything at all is the signal that somebody is here again.
    """
    global _auto_turns, _auto_woke, _auto_spent_said
    _auto_woke = False
    remote.ensure_mirror()
    remote.set_busy(False)
    if session_pt is None:
        # No prompt_toolkit: nothing here can race a blocking `input()`, so both
        # a message and a remote line wait for the next Enter. They are printed
        # either way, which is the part that matters - the person can see there
        # is something to answer.
        _show_arrivals()
        _show_remote_notices()
        waiting = remote.take_line()
        if waiting:
            return _echo_remote(waiting)
        if _attacca_listening():
            from aetheris import attacca
            if attacca.call_waiting():
                # Served before blocking, since nothing can interrupt `input()`;
                # a call that lands while it waits is served after the Enter.
                return IDLE_CALL
        remote.set_driver("terminal")
        line = input(f"  {S.USER_CLR}{S.BOLD}❯{S.R} ").strip()
        _auto_turns, _auto_spent_said = 0, False
        _auto_asked_by.clear()
        return line

    if channel.worked():
        # The turn that just ended did something - read a file, made a change,
        # ran a test - so it was not the loop this budget exists to stop. Only a
        # run of turns that came back with nothing done spends it. That is what
        # makes `CHANNEL_AUTO_TURN_MAX = 0` a reasonable thing to set: two agents
        # actually working can talk all day, and two agents only talking cannot.
        _auto_turns, _auto_spent_said = 0, False
    # Queued, not answered: there is no running application to exit out of until
    # `prompt_async` below is awaited. The watcher picks it up on its first poll,
    # which is what covers a question that landed in the gap between the last
    # turn ending and this prompt opening.
    _maybe_auto_turn(session_pt, _show_arrivals())
    _show_remote_notices()
    # `ANSI` lives behind the prompt_toolkit guard in `config`, so it is reached
    # the same way `main` reaches it rather than imported at module level.
    ANSI = config.ANSI
    # A callable, so prompt_toolkit asks again on every keystroke and the
    # banner appears with the `!` rather than on the next line.
    message = lambda: ANSI(_prompt_message())          # noqa: E731
    watcher = asyncio.ensure_future(_watch_channel(session_pt))
    # `patch_stdout` is what lets the watcher print *above* the prompt rather
    # than through the middle of what is being typed.
    #
    # `raw=True` is not decoration. Without it prompt_toolkit sanitises what it
    # is handed - `Output.write` turns every ESC into `?` to stop stray cursor
    # movement corrupting its picture of the screen - so a coloured line
    # printed while a prompt is open arrives as `?[38;2;250;189;47m◆ …`. That
    # is every message from another agent and every notice from the remote, on
    # every platform; it was only ever noticed on Windows because that is where
    # somebody happened to be sitting when one arrived. What goes out this way
    # is colour and nothing else, which is exactly what `raw` is safe for.
    keep_prompt_intact = getattr(config, "patch_stdout", None)
    try:
        if keep_prompt_intact is None:
            line = await _typed_or_remote(session_pt, message)
        else:
            with keep_prompt_intact(raw=True):
                # Inside, not outside: `patch_stdout` replaces `sys.stdout` with
                # a proxy of its own, and the tee has to sit over *that* to
                # catch what is printed while the prompt is open.
                remote.ensure_mirror()
                line = await _typed_or_remote(session_pt, message)
    finally:
        watcher.cancel()
    if line is IDLE_CALL:
        return line           # an agent elsewhere, not somebody arriving here
    if not _auto_woke:
        # Somebody is at the keyboard. Whatever the channel spent answering on
        # its own is forgiven, including an empty line - pressing Enter is how
        # you say "I am here" to a terminal that has stopped answering. Anything
        # still queued is theirs too: the model reads it this turn regardless,
        # through `turn_note` and its own cursor.
        _auto_turns, _auto_spent_said = 0, False
        _auto_asked_by.clear()
    return line


def _agents_command(rest: str) -> None:
    """`/agents`, and the three things a person can do to the board by hand."""
    # Split on the first word rather than matched with `startswith`: otherwise
    # `/agents saying hello` would be read as `say` and send "ing hello".
    verb, _, argument = rest.strip().partition(" ")
    verb, argument = verb.lower(), argument.strip()

    if verb in ("on", "off") and not argument:
        config.CHANNEL_ENABLED = verb == "on"
        agent_id = channel.join(_agent_label()) if config.CHANNEL_ENABLED else ""
        if not config.CHANNEL_ENABLED:
            channel.leave()
        _switch("/agents", verb, config.CHANNEL_ENABLED, "Agent channel",
                "this session appears on the board other agents here can see",
                on_note=f"This session is {agent_id or 'unregistered'}.",
                off_note="Other agents can no longer see this session, and its "
                         "claims are released.")
        return

    if verb == "say":
        if not argument:
            print(f"  {S.ERR}✗ Usage: /agents say <message>{S.R}\n")
            return
        ok, said = channel.send(argument)
        print(f"  {S.OK}✓ {said.capitalize()}.{S.R}\n" if ok
              else f"  {S.ERR}✗ Not sent: {said}.{S.R}\n")
        return

    if verb in ("plan", "goal"):
        # The shared goal is a project note, not a new field on the board:
        # `notes` is already per-project, already shared by everybody here, and
        # already announced in every one of their system prompts. A second
        # answer to "what are we all doing" would be a second answer waiting to
        # disagree with the first.
        note_id = str(getattr(config, "CHANNEL_PLAN_NOTE", "plan") or "plan")
        if argument:
            saved = notes.handle_write_note(note_id, argument)
            ok = not saved.startswith("[Error")
            print(f"  {S.OK}✓ {saved}{S.R} {S.MUTED}Every agent here can read it "
                  f"with read_note '{note_id}'.{S.R}\n" if ok
                  else f"  {S.ERR}✗ {saved}{S.R}\n")
            return
        body = notes.handle_read_note(note_id)
        if body.startswith("[Error") or body.startswith("[No note"):
            print(f"  {S.WARN}⚠ No shared plan yet.{S.R} {S.MUTED}"
                  f"/agents plan <what everybody here is working towards>{S.R}\n")
        else:
            print(f"  {S.BOLD}{S.ACCENT}The plan every agent here shares{S.R}")
            print(f"  {S.MUTED}note '{note_id}'{S.R}\n")
            print(_render_full(body))
            print()
        return

    if verb == "clear":
        # The board is shared, so this clears it for everybody here - which is
        # exactly why no tool reaches it. Same reasoning as `release`: the
        # person at the keyboard is the only one who can see every terminal.
        kind = argument.lower()
        if kind and kind not in ("dm", "direct", "dms", "everyone", "broadcast",
                                 "all"):
            print(f"  {S.ERR}✗ Usage: /agents clear [dm|everyone|all]{S.R}\n")
            return
        if kind == "all":
            kind = ""
        gone = channel.clear_messages(kind)
        _auto_asked_by.clear()
        what = {"dm": "direct message", "direct": "direct message",
                "dms": "direct message", "everyone": "broadcast",
                "broadcast": "broadcast"}.get(kind, "message")
        print(f"  {S.OK}✓ Cleared {gone} {what}{'' if gone == 1 else 's'} "
              f"from the board.{S.R} {S.MUTED}It is shared - they are gone for "
              f"every agent here.{S.R}\n" if gone
              else f"  {S.WARN}⚠ There was nothing to clear.{S.R}\n")
        return

    if verb == "release":
        target = argument
        if not target:
            print(f"  {S.ERR}✗ Usage: /agents release <path>{S.R}\n")
            return
        # The one way past another agent's claim, and deliberately not something
        # the model can reach: the person is the only one here who can see both
        # terminals and decide which of them should be holding the file.
        dropped = channel.force_release(target)
        print(f"  {S.OK}✓ Released {', '.join(dropped)}.{S.R}\n" if dropped
              else f"  {S.WARN}⚠ Nothing there is claimed: {target}{S.R}\n")
        return

    _show_agents()


def _switch(command: str, argument: str, now: bool, label: str, what: str,
            on_note: str = "", off_note: str = "", extra=()) -> bool | None:
    """One on/off command, worded and spaced like every other one.

    Three of these - `/autocommit`, `/autoverify`, `/deepthink` - already
    answered a bare `/x` by saying what the switch is for and where it stands,
    which is what somebody typing it wants to know. The other six answered with
    `✗ Usage: /x <on/off>` and nothing else: an error, for a command that was
    not wrong, with no hint of what it even switches. They also skipped the
    blank line every other command in this loop ends on, so the next prompt sat
    flush against the confirmation.

    Returns the new value, or None when nothing changed.
    """
    argument = (argument or "").strip().lower()
    if argument in ("on", "off"):
        value = argument == "on"
        note = on_note if value else off_note
        print(f"  {S.INFO}✓ {label} is {'ON' if value else 'OFF'}.{S.R}"
              + (f"{S.MUTED} {note}{S.R}" if note else ""))
        print()
        return value

    if argument:
        print(f"  {S.ERR}✗ '{argument}' is not on or off.{S.R}")
    print(f"  {S.INFO}{label} is {S.BOLD}{'ON' if now else 'OFF'}{S.R}"
          f"{S.MUTED} - {what}{S.R}")
    for line in extra:
        print(f"  {S.MUTED}{line}{S.R}")
    print(f"  {S.MUTED}Usage: {command} <on/off>{S.R}")
    print()
    return None


_TDD_LABEL = "/tdd"


def _arm_tdd() -> None:
    """Lock this project's test files for the request that is about to run.

    "Make this test pass" is a request models answer by editing the test -
    deleting the assertion, or loosening it until it is true. Asking them not
    to does not work, for the same reason it does not work in a planning stage:
    a 4B model that is stuck will take the opening it is given. So the opening
    is closed at the dispatcher, through the permission rules that already sit
    there, and the model is told plainly what it may not touch and why.

    One request, not a mode. It lifts itself when the turn ends (including on
    an error), because a lock nobody remembers turning on is worse than no lock.
    """
    permissions.release(_TDD_LABEL)          # never stack two arms
    rules = [f"{tool}({pattern})"
             for pattern in verify.TEST_PATTERNS
             for tool in ("edit_file", "write_file", "delete_file", "copy_file")]
    permissions.hold("deny", rules, _TDD_LABEL)
    config.TDD_LOCK = True
    print(f"  {S.INFO}✓ /tdd armed for one request.{S.MUTED} The test files are "
          f"read-only; only the code under test can change. Auto-verify gets "
          f"{config.TDD_VERIFY_FAILURES} tries instead of "
          f"{llm_client.MAX_VERIFY_FAILURES}.{S.R}")


def _export_command(rest: str, messages: list[dict]) -> None:
    """`/export [filename]`: the conversation as markdown.

    `rest` comes off `user_input` and not off the lowercased line the commands
    are matched against. A filename is not a command, and taking it from there
    meant `/export Notes.MD` quietly wrote `notes.md` - on a case-sensitive
    filesystem that is a different file from the one that was asked for.
    """
    name = rest.strip()
    if not name:
        # `/export ` with a stray space used to name no file at all and report
        # `Export failed: ''` instead of exporting.
        name = f"export_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    try:
        with open(name, "w", encoding="utf-8") as f:
            for m in messages:
                if not isinstance(m, dict) or m.get("role") == "system":
                    continue
                role_name = "User" if m.get("role") == "user" else "Assistant"
                content = m.get("content")
                if not isinstance(content, str):
                    continue
                c = re.sub(r'<tool_call>.*?</tool_call>', '', content, flags=re.DOTALL)
                c = strip_thinking(c)
                if c:
                    f.write(f"### {role_name}\n\n{c}\n\n")
        print(f"  {S.OK}✓ Conversation exported to {name}{S.R}\n")
    except Exception as e:
        print(f"  {S.ERR}✗ Export failed: {e}{S.R}\n")


def _system_command(rest: str, messages: list[dict]) -> None:
    """`/system <persona>` and `/system reset`: what goes in front of the prompt.

    Two things were wrong here, and both were about reading the line rather
    than about personas.

    `rest` comes off `user_input`, not off the lowercased `cmd` the commands are
    matched against. A persona is prose the person wrote for the model to read,
    and this is the only command whose whole argument is that - so it was the
    only one where lowercasing the line changed the meaning of what was stored:
    "You are a Korean tutor named Minji" became "you are a korean tutor named
    minji".

    And the system message is *composed*, not concatenated. Writing
    `persona + SYSTEM_PROMPT + summary` by hand left out the two blocks
    `_compose_system_prompt` adds - the memories marked important (5.15) and
    this project's note titles (5.16) - so setting a persona silently dropped
    both from the prompt until something else happened to rebuild it.
    `_refresh_system_prompt` carries the `<SUMMARY>` across on its own.
    """
    new_prompt = rest.strip()
    if not new_prompt:
        print(f"  {S.ERR}✗ Usage: /system <new prompt> or /system reset{S.R}\n")
        return

    config.CUSTOM_PERSONA = "" if new_prompt.lower() == "reset" else new_prompt
    _refresh_system_prompt(messages)
    if config.CUSTOM_PERSONA:
        print(f"  {S.INFO}✓ System prompt updated.{S.R}")
        print(f"  {S.WARN}⚠ To ensure the persona is applied correctly, please "
              f"clear the previous conversation with /clear.{S.R}\n")
    else:
        print(f"  {S.INFO}✓ System prompt reset to default.{S.R}")
        print(f"  {S.WARN}⚠ If the persona context from the previous conversation "
              f"remains, please clear the conversation history with /clear.{S.R}\n")


def _set_command(rest: str, messages: list[dict]) -> None:
    """`/set`: read and change a setting without editing `config.py`.

    `/set`              every setting, with the changed ones marked
    `/set NAME`         one of them
    `/set NAME value`   change it, for this session and the next
    `/set NAME default` put it back to what `config.py` says
    """
    name, _, value = rest.strip().partition(" ")
    if not name or not value.strip():
        _show_settings(name)
        return

    ok, detail = config.set_setting(name, value)
    if not ok:
        print(f"  {S.ERR}✗ {detail}{S.R}\n")
        return

    name = name.strip().upper()
    print(f"  {S.OK}✓ {name} = {_fmt_setting(getattr(config, name))}{S.R} "
          f"{S.MUTED}({detail}){S.R}")
    # Some of them are part of the system prompt - NATIVE_TOOLS decides whether
    # the tool catalogue is in it at all - so it is rebuilt every time rather
    # than only for the ones somebody remembered to list here.
    _refresh_system_prompt(messages)
    # A remote that is open was started from the settings as they were. Moving
    # the port is the one somebody actually types mid-session, and a `/set`
    # that quietly did nothing until the next restart would read as broken.
    if name.startswith("REMOTE_"):
        try:
            changed = remote.reconfigure()
        except Exception as e:
            print(f"  {S.ERR}✗ The remote could not move there: {e}{S.R}\n")
            return
        if changed.get("rebound"):
            print(f"  {S.INFO}◆ The remote moved, so its link changed. The old "
                  f"one no longer opens anything.{S.R}")
            for line in _remote_lines():
                print(f"  {S.MUTED}{line}{S.R}")
    print()


def _remote_lines() -> list:
    """What `/remote` says about a door that is already open."""
    if not remote.running():
        return ["│ /remote on opens it here; /remote on lan opens it to this "
                "machine's network as well.",
                "╰─ the token is made when it opens, is never saved, and dies "
                "with it."]
    state = remote.status()
    seen = ("nobody has opened it yet" if not state["seen"]
            else f"last read {channel.ago(state['seen'])}")
    rows = [f"│ bound to {state['host']}:{state['port']} - {seen}"]
    if state["pairing"]:
        rows.append(f"│ a browser must also type a code shown here before it can "
                    f"drive anything ({state['paired']} paired)")
    if state["clients"]:
        who = ", ".join(row["address"] for row in remote.clients()[:4])
        rows.append(f"│ opened from {who}")
    if state["refused"]:
        rows.append(f"│ refused for wrong tokens: {', '.join(state['refused'])}")
    if state["queued"]:
        rows.append(f"│ {state['queued']} line(s) typed there, waiting for this prompt")
    rows.append("╰─ open this, and whoever holds it is at this prompt:")
    rows += [f"   {url}" for url in state["urls"]]
    rows.append(f"   {'/remote qr shows it as something a camera can read.'}")
    return rows


def _open_saved_remote() -> None:
    """Re-open the door for somebody who left it open in `settings.json`.

    A new token every time, printed every time. `REMOTE_ENABLED` is a standing
    answer to "should this session be reachable", not a saved key - there is
    no saved key.
    """
    if not config.REMOTE_ENABLED or remote.running():
        return
    try:
        remote.start()
    except Exception as e:
        print(f"  {S.WARN}⚠ Remote control is on in your settings but could not "
              f"start: {e}{S.R}\n")
        return
    print(f"  {S.INFO}◆ Remote control is on.{S.R}")
    for line in _remote_lines():
        print(f"  {S.MUTED}{line}{S.R}")
    print()


def _show_remote_qr() -> None:
    """`/remote qr`: the link as something a camera can read.

    Forty-three random characters is not a thing anybody types into a phone,
    and a link that is hard to open is a feature that goes unused. The last
    address is the one drawn - on `lan` that is the one a phone can actually
    reach, which is the whole point of pointing a phone at it.
    """
    if not remote.running():
        print(f"  {S.ERR}✗ Nothing is open to scan.{S.MUTED} {S.GRAY}/remote on"
              f"{S.MUTED} first.{S.R}\n")
        return
    link = remote.status()["urls"][-1]
    if not qr.fits(link):
        print(f"  {S.WARN}⚠ That link is too long to draw.{S.R}\n  {link}\n")
        return
    print()
    print(qr.render(link, colour=bool(S.R)))
    print(f"\n  {S.MUTED}{link}{S.R}")
    if remote.status()["pairing"]:
        print(f"  {S.MUTED}Scanning it opens the page; driving the session also "
              f"needs the code this terminal prints when the page asks.{S.R}")
    print()


def _remote_command(rest: str) -> None:
    """`/remote`: the one door into this session, and whether it is open.

    On is a deliberate act every time. What it opens is not a view of the
    session but the session itself - the link types lines, and a line can be
    `!rm -rf ~` - so the switch says so, the LAN case says it louder, and
    neither the port nor the token survives the process.
    """
    verb, _, argument = rest.strip().partition(" ")
    verb, argument = verb.lower(), argument.strip().lower()

    if verb in ("qr", "code"):
        _show_remote_qr()
        return

    if verb == "forget":
        dropped = remote.forget_sessions()
        print(f"  {S.INFO}✓ {dropped or 'No'} paired browser"
              f"{'' if dropped == 1 else 's'} dropped.{S.MUTED} The link still "
              f"works; whoever opens it has to be told a new code.{S.R}\n")
        return

    if verb == "on" and not remote.running():
        try:
            remote.start("lan" if argument in ("lan", "network") else "")
        except Exception as e:
            print(f"  {S.ERR}✗ The remote could not be opened: {e}{S.R}\n")
            return
    elif verb == "off":
        remote.stop()

    chosen = _switch(
        "/remote", verb, remote.running(), "Remote control",
        "this session can be driven from a browser: the transcript, the prompt, "
        "and the approvals that would otherwise wait for a keystroke",
        on_note="Whoever opens the link is at this prompt, with everything it "
                "can do.",
        off_note="The port is closed and the token is gone. A link already open "
                 "stops working.",
        extra=_remote_lines())
    if chosen is not None:
        config.REMOTE_ENABLED = remote.running()
    if verb == "on" and remote.running():
        for line in _remote_lines():
            print(f"  {S.MUTED}{line}{S.R}")
        if remote.status()["host"] == "0.0.0.0":
            print(f"  {S.WARN}⚠ This is open to every machine that can reach "
                  f"yours.{S.MUTED} Only the link opens it, but the link is all "
                  f"it takes.{S.R}")
        print()


def _vm_command(rest: str) -> None:
    """`/vm`: what the Python scratch process is holding, and how to clear it.

    The VM keeps a namespace across a whole session and lives in a directory of
    its own, so both are things a person may reasonably want to see or empty
    without having to ask the model to do it for them.
    """
    verb = rest.strip().lower()
    state = vm.state()

    if verb in ("reset", "clear"):
        result = vm.run("pass", reset=True)
        print(f"  {S.OK}✓ The Python VM is empty again.{S.R}\n" if not result.get("crashed")
              else f"  {S.ERR}✗ {result['crashed']}.{S.R}\n")
        return

    if verb in ("stop", "off"):
        vm.shutdown()
        print(f"  {S.INFO}✓ The Python VM was stopped.{S.MUTED} The next "
              f"run_python starts a new one.{S.R}\n")
        return

    print()
    print(f"  {S.BOLD}{S.ACCENT}Python VM{S.R}")
    print(f"  {_hr(width=44)}")
    if not state["alive"]:
        print(f"  {S.MUTED}not running{S.R} {S.GRAY}- the next run_python starts it{S.R}")
    else:
        print(f"  {S.OK}running{S.R}  {S.GRAY}{state['calls']} call(s), "
              f"up {state['age']:.0f}s{S.R}")
    print(f"  {S.GRAY}scratch directory:{S.R} {state['directory']}")
    print(f"  {S.GRAY}per call:{S.R} {config.VM_TIMEOUT}s, "
          f"{config.VM_OUTPUT_CHARS} chars of output"
          + (f", {config.VM_MEMORY_MB}MB" if os.name != "nt" else ""))
    print(f"  {S.MUTED}/vm reset{S.R} {S.GRAY}empties it, {S.MUTED}/vm stop{S.GRAY} "
          f"ends the process{S.R}\n")


def _show_agents() -> None:
    """The board: who is here, what they are holding, and what has been said."""
    if not config.CHANNEL_ENABLED:
        print(f"  {S.WARN}⚠ The agent channel is off.{S.MUTED} "
              f"{S.GRAY}/agents on{S.MUTED} turns it back on.{S.R}\n")
        return
    here = channel.agents()
    mine = channel.me()
    print()
    print(f"  {S.BOLD}{S.ACCENT}Agents in {channel.workspace()}{S.R}")
    print(f"  {_hr(width=44)}")
    if not here:
        print(f"  {S.GRAY}nobody, not even this session{S.R}")
    for record in here:
        who = f"{record['id']} (you)" if record["id"] == mine else record["id"]
        holds = ", ".join(record.get("holds") or []) or "nothing"
        print(f"  {S.ACCENT}{who:12}{S.R} {S.WHITE}{record.get('label') or '?'}{S.R}")
        print(f"  {' ' * 12} {S.MUTED}started {channel.ago(record.get('started'))}, "
              f"holding {holds}{S.R}")
    recent = channel.read_board()["messages"][-6:]
    if recent:
        print()
        print(f"  {S.BOLD}{S.ACCENT}Recently said{S.R}")
        print(f"  {_hr(width=44)}")
        for entry in recent:
            print(f"  {S.MUTED}{channel.ago(entry.get('at')):>8}{S.R} "
                  f"{S.GRAY}{channel.describe(entry)}{S.R}")
    print(f"\n  {S.MUTED}/agents say <text> to talk to them, /agents release "
          f"<path> to take a file back.{S.R}\n")


async def main(resume_id: str = "") -> None:

    if config.CURRENT_OS == "Windows":
        os.system("")

    print("\033[2J\033[H", end="")
    providers.apply_startup()
    failed_mcp = _connect_mcp_servers()
    if providers.current().drives_turns and providers.current().api_key:
        # After the MCP servers, so the first announcement already lists theirs.
        from aetheris import attacca
        attacca.connect_in_background()
    config.SYSTEM_PROMPT = _build_system_prompt()
    messages: list[dict] = [{"role": "system", "content": _compose_system_prompt()}]

    current_session_id = None
    # Adopted before the banner, so the banner reports the resumed session's
    # model rather than the one it is about to be replaced by; replayed after
    # it, so the transcript reads downwards from the header as it did the first
    # time. `cli()` has already established that the id names a real file.
    resumed = load_session(resume_id) if resume_id else None
    if resumed:
        messages = _adopt_session(resumed)
        current_session_id = resume_id

    _welcome()
    _report_mcp_problems(failed_mcp)
    _report_strays()
    agent_id = channel.join(_agent_label())
    _report_agents(agent_id)
    if agent_id:
        # The prompt above was built before this session had an id - `config`
        # builds it at import time, and joining is a file the board has to be
        # asked for. Rebuilt here, once, while nothing has been sent yet, so
        # the model knows which agent it is before its first turn rather than
        # having to infer it from a message addressed to a name it has never
        # been given.
        _refresh_system_prompt(messages)
    _open_saved_remote()
    _report_usage(messages)

    if resume_id:
        if resumed:
            print(f"  {S.OK}✓ Resumed session: {config.SESSION_TITLE or resume_id} "
                  f"(Model: {config.MODEL}){S.R}\n")
            _replay_session(messages)
        else:
            print(f"  {S.ERR}✗ Session not found: {resume_id}{S.R}")
            print(f"  {S.GRAY}Starting a new one instead.{S.R}\n")

    if config.PROMPT_TOOLKIT_AVAILABLE:
        paths.ensure_home()          # FileHistory opens its file straight away
        from aetheris.config import (SlashCommandCompleter, PathMentionCompleter,
                                      merge_completers, PromptSession, FileHistory)
        # Driven by the same table `/help` renders, so the menu and the help
        # cannot disagree about what exists - which is how several commands
        # ended up working, completable and undocumented - and so the menu can
        # say what each one does and what may follow it.
        completer = merge_completers([
            SlashCommandCompleter(complete_command),
            PathMentionCompleter(),
        ])
        session_pt = PromptSession(
            history=FileHistory(config.HISTORY_FILE),
            completer=completer,
            # Pointed at the real stream rather than at whatever `sys.stdout`
            # happens to be: with a remote already open, `sys.stdout` is the
            # mirror, and everything this draws - the prompt itself included -
            # went out to the phone. See `remote.unmirrored_stdout`.
            output=config.create_output(stdout=remote.unmirrored_stdout()),
            # What colours the line itself once it starts with `!`. The banner
            # above comes from `_prompt_message`; between them, a command for
            # this machine never looks like a message for the model.
            lexer=config.ShellLineLexer(SHELL_STYLE),
            # The menu has to open on its own for `@` to be discoverable: nobody
            # presses Tab after a character they have not been told completes.
            # A condition rather than `True`, because `True` also means "keep
            # eight rows free under the prompt at all times" - a blank band
            # under every ordinary sentence anybody types. See
            # `config._wants_the_menu`.
            complete_while_typing=config.COMPLETE_WHILE_TYPING,
        )
        # Deleting the `/` has to take the menu - and the rows held for it -
        # away again. prompt_toolkit keeps a completion state until something
        # cancels it, and the height it reserves is `while_typing OR a state is
        # open`, so without this the band outlives the line that earned it.
        session_pt.default_buffer.on_text_changed += _close_menu_when_unwanted

    # Which MCP servers the system prompt was built for, so a load can be
    # noticed. Sorted, so the comparison is about the set and not the order.
    mcp_servers_in_prompt = sorted(config.LOADED_MCP_SERVERS)

    while True:
        try:
            user_input = await _read_line(
                session_pt if config.PROMPT_TOOLKIT_AVAILABLE else None)
            # A console that hands back surrogate escapes would otherwise poison
            # the history: every later save and request would raise.
            if user_input is not IDLE_CALL:
                user_input = config.safe_text(user_input)
        except (EOFError, KeyboardInterrupt):
            channel.leave()
            remote.stop()
            mcp_client.shutdown()
            if providers.current().drives_turns:
                from aetheris import attacca
                attacca.disconnect()
            print(f"\n\n  {S.GRAY}Goodbye!{S.R}\n")
            break

        if user_input is IDLE_CALL:
            # Outside the `try` above on purpose: Ctrl+C at an approval prompt
            # for a call nobody here made means "not that", not "quit".
            _serve_idle_calls()
            continue

        if not user_input:
            continue

        if user_input.startswith("!"):
            # The user's own command, not the model's. It still goes into the
            # conversation, because the reason to run `!git status` mid-chat is
            # almost always so that the next question can be about its output.
            command = user_input[1:].strip()
            if not command:
                print(f"  {S.ERR}✗ Usage: !<shell command>{S.R}\n")
                continue
            print(f"\n  {S.MUTED}${S.R} {S.WHITE}{command}{S.R}")
            output = _run_user_command(command)
            _fmt_tool_result(command, output)
            print()
            # On screen it is theirs and unredacted - they ran it. What goes
            # into the conversation is not: that reaches the provider and the
            # session file, and `!cat .env` should not be how a key gets there.
            # `safe_run_cmd` is called directly here, so `dispatch_tool`'s own
            # redaction is not in the way.
            kept = f"[Shell] $ {command}\n{output}"
            hidden = vault.redact(kept)
            if hidden != kept:
                # Said rather than left to be discovered. The screen has the
                # real output and the model's copy does not, and the only way
                # to find that out used to be noticing `{{env:…}}` in a
                # directory listing and wondering what was wrong. A `.env`
                # value that is also an ordinary word - a project directory, a
                # user name - matches everywhere it appears, which is the
                # price of never letting one through.
                names = ", ".join(vault.used_in({"v": hidden})) or "something"
                print(f"  {S.MUTED}◆ {names} from .env {'is' if names.count(',') == 0 else 'are'} "
                      f"hidden in the copy the model gets. {S.GRAY}/set SECRET_REDACT off"
                      f"{S.MUTED} stops that.{S.R}\n")
            messages.append({"role": "user", "content": hidden})
            current_session_id = save_session(messages, current_session_id)
            continue

        cmd = user_input.lower()
        if _refused_from_remote(cmd):
            continue
        if cmd in ("/exit", "/quit"):
            channel.leave()
            remote.stop()
            mcp_client.shutdown()
            print(f"\n  {S.GRAY}Goodbye!{S.R}\n")
            break
        if cmd == "/usage":
            if providers.current().drives_turns:
                from aetheris import attacca
                attacca.show_usage()
            else:
                display_usage_graph(messages)
            continue
        if cmd == "/help":
            _show_help()
            continue
        if cmd == "/clear":
            config.LOADED_MCP_SERVERS.clear()
            config.SYSTEM_PROMPT = _build_system_prompt()
            messages = [{"role": "system", "content": _compose_system_prompt()}]
            current_session_id = None
            config.SESSION_TITLE = ""
            # A cleared conversation here is a new one there, too: Attacca
            # keeps its own context, and the old session would remember it all.
            config.ATTACCA_SESSION = ""
            config.token_history.clear()
            config.turn_index = 0
            config.LOADED_SKILLS.clear()
            # The prompt was just rebuilt for an empty set of loaded servers,
            # so what it was built for has to move with it - otherwise the next
            # message sees a difference that is not there and rebuilds again.
            mcp_servers_in_prompt = sorted(config.LOADED_MCP_SERVERS)
            print("\033[2J\033[H", end="")
            _welcome()
            _report_usage(messages)
            print(f"  {S.OK}✓ Conversation and usage cleared.{S.R}\n")
            continue
        if cmd == "/models":
            provider = providers.current()
            print(f"\n  {S.BOLD}{S.ACCENT}{provider.label} Models{S.R}")
            print(f"  {_hr(width=50)}")
            try:
                available = provider.list_models()
                if not available:
                    print(f"  {S.WARN}\u26a0 None found.{S.R}")
                for i, entry in enumerate(available, 1):
                    marker = f" {S.OK}\u25c0 current{S.R}" if entry["name"] == config.MODEL else ""
                    detail = f"  {S.GRAY}({entry['detail']}){S.R}" if entry.get("detail") else ""
                    print(f"  {S.ACCENT}{i:3}.{S.R} {S.WHITE}{entry['name']}{S.R}{detail}{marker}")
            except Exception as e:
                connect._print_problem("Failed to list models", e)
            print()
            continue
        if cmd == "/model":
            provider = providers.current()
            print(f"\n  {S.GRAY}provider{S.R}  {S.WHITE}{provider.label}{S.R}")
            print(f"  {S.GRAY}model{S.R}     {S.WHITE}{config.MODEL}{S.R}\n")
            connect.run(provider.name)
            continue
        if cmd == "/connect" or cmd.startswith("/connect "):
            connect.run(user_input.split(" ", 1)[1].strip() if " " in user_input else "")
            _refresh_system_prompt(messages)
            continue
        if cmd == "/sessions":
            sessions = list_sessions()
            print(f"\n  {S.BOLD}{S.ACCENT}Saved Sessions{S.R}")
            if not sessions:
                print(f"  {S.GRAY}  No saved sessions yet.{S.R}")
            for sid, title, meta in sessions:
                marker = f" {S.OK}◀ current{S.R}" if sid == current_session_id else ""
                print(f"  {S.GRAY}•{S.R} {S.WHITE}{title or S.GRAY + '(untitled)' + S.R}{S.R}{marker}")
                print(f"    {S.MUTED}{sid}{S.R}  {S.GRAY}{meta}{S.R}")
            print(f"\n  {S.GRAY}Load one with {S.ACCENT}/load <id or title>{S.GRAY}.{S.R}\n")
            continue
        if cmd.startswith("/load"):
            parts = user_input.split(" ", 1)
            if len(parts) < 2 or not parts[1].strip():
                print(f"  {S.ERR}✗ Usage: /load <id or title>{S.R}\n")
                continue
            query = parts[1].strip()
            matches = find_sessions(query)
            if len(matches) > 1:
                _show_ambiguous(query, matches, "Re-run /load with one of the ids above.")
                continue
            sid = matches[0][0] if matches else query
            loaded = load_session(sid)
            if loaded:
                messages = _adopt_session(loaded)
                current_session_id = sid
                label = config.SESSION_TITLE or sid
                print(f"  {S.OK}✓ Loaded session: {label} (Model: {config.MODEL}){S.R}\n")
                _replay_session(messages)
            else:
                print(f"  {S.ERR}✗ Session not found: {sid}{S.R}\n")
            continue
        if cmd == "/title" or cmd.startswith("/title "):
            parts = user_input.split(" ", 1)
            new_title = parts[1].strip() if len(parts) > 1 else ""
            if not new_title:
                shown = config.SESSION_TITLE or f"{S.GRAY}(untitled){S.R}"
                print(f"\n  {S.GRAY}title{S.R}  {S.WHITE}{shown}{S.R}")
                print(f"  {S.GRAY}id{S.R}     {S.WHITE}{current_session_id or '(not saved yet)'}{S.R}")
                print(f"  {S.MUTED}Rename with /title <new title>{S.R}\n")
                continue
            if not clean_title(new_title):
                print(f"  {S.ERR}✗ That title is empty after cleanup.{S.R}\n")
                continue
            current_session_id = rename_session(current_session_id, new_title)
            print(f"  {S.OK}✓ Session titled: {config.SESSION_TITLE}{S.R} {S.MUTED}({current_session_id or 'saved on next message'}){S.R}\n")
            continue
        if cmd == "/autotitle" or cmd.startswith("/autotitle "):
            chosen = _switch(
                "/autotitle", user_input[len("/autotitle"):], config.AUTO_TITLE,
                "Auto session titling",
                "the model names a new session after its first exchange",
                on_note="New sessions are named for you; /title renames one.",
                off_note="New sessions stay untitled until you name them with /title.")
            if chosen is not None:
                config.AUTO_TITLE = chosen
            continue
        if cmd == "/automode" or cmd.startswith("/automode "):
            chosen = _switch(
                "/automode", user_input[len("/automode"):], config.AUTO_ALLOW,
                "Automode",
                "every guarded tool runs without stopping to ask you first",
                on_note="Tools run without asking. /perms is the finer-grained way.",
                off_note="Every guarded tool waits for your approval again.")
            if chosen is not None:
                config.AUTO_ALLOW = chosen
            continue
        if cmd == "/fullcontent" or cmd.startswith("/fullcontent "):
            chosen = _switch(
                "/fullcontent", user_input[len("/fullcontent"):],
                config.RETURN_ALL_FILE_CONTENT, "Full content",
                "a file or page reaches the model whole rather than cut short",
                on_note="read_file and get_url hand over the whole thing.",
                off_note=f"They stop at {config.FILE_MAX_DISPLAY_LENGTH} characters.")
            if chosen is not None:
                config.RETURN_ALL_FILE_CONTENT = chosen
            continue
        if cmd == "/record" or cmd.startswith("/record "):
            chosen = _switch(
                "/record", user_input[len("/record"):], config.SAVE_CHAT_HISTORY,
                "Chat history recording",
                f"conversations are saved under {config.SESSION_DIR}",
                on_note="This conversation is saved as you go; /sessions lists them.",
                off_note="Nothing more is written to disk for this conversation.")
            if chosen is not None:
                config.SAVE_CHAT_HISTORY = chosen
            continue
        if cmd.startswith("/export"):
            # `user_input`, not `cmd`: see `_export_command`.
            _export_command(user_input[len("/export"):], messages)
            continue
        if cmd.startswith("/system"):
            # `user_input`, not `cmd`: see `_system_command`.
            _system_command(user_input[len("/system"):], messages)
            continue
        if cmd == "/planmode" or cmd.startswith("/planmode "):
            chosen = _switch(
                "/planmode", user_input[len("/planmode"):], config.PLANMODE,
                "Plan mode",
                "the model must submit a plan for approval before it changes anything",
                on_note="It calls submit_plan_for_approval first and waits for you.",
                off_note="It goes straight at the work again.")
            if chosen is not None:
                config.PLANMODE = chosen
            continue
        if cmd == "/skills" or cmd.startswith("/skills "):
            arg = user_input.split(" ", 1)[1].strip().lower() if " " in user_input else ""
            if not arg:
                _show_skills()
            elif arg == "reload":
                skills.discover_skills(force=True)
                _refresh_system_prompt(messages)
                print(f"  {S.OK}✓ Skills reloaded: {len(skills.list_skills())} available.{S.R}\n")
            else:
                print(f"  {S.ERR}✗ Usage: /skills [reload]{S.R}\n")
            continue
        if cmd.startswith("/skill"):
            parts = user_input.split(" ", 1)
            if len(parts) < 2 or not parts[1].strip():
                print(f"  {S.ERR}✗ Usage: /skill <name>  (see /skills){S.R}\n")
                continue
            skill = skills.get_skill(parts[1].strip())
            if skill is None:
                print(f"  {S.ERR}✗ Skill not found: {parts[1].strip()}{S.R}\n")
                continue
            _fmt_tool_call("use_skill", {"skill_name": skill["name"]})
            result = skills.handle_use_skill(skill["name"])
            _fmt_tool_result("use_skill", result)
            print()             # one blank line, like every other command here
            messages.append({"role": "user", "content": f"[Tool Result for 'use_skill']:\n{result}"})
            current_session_id = save_session(messages, current_session_id)
            continue
        if cmd == "/mcp" or cmd.startswith("/mcp "):
            parts = user_input.split()
            sub = parts[1].lower() if len(parts) > 1 else ""
            args = parts[2:]
            injected = ""

            if not sub:
                _show_mcp()
            elif sub in ("tools", "prompts", "all"):
                _show_mcp(sub, args[0] if args else "")
            elif sub in ("reload", "refresh"):
                sys.stdout.write(f"  {S.MUTED}⟳ reloading MCP servers…{S.R}")
                sys.stdout.flush()
                mcp_client.reconnect()
                sys.stdout.write("\r\033[K")
                sys.stdout.flush()
                _refresh_system_prompt(messages)
                print(f"  {S.OK}✓ MCP reloaded: {mcp_client.status_summary()}.{S.R}\n")
                _report_mcp_problems([s for s in mcp_client.all_servers() if s.state == "failed"])
            elif sub in ("connect", "reconnect"):
                if not args:
                    print(f"  {S.ERR}✗ Usage: /mcp connect <server name>{S.R}\n")
                else:
                    touched = mcp_client.reconnect(args[0])
                    if not touched:
                        print(f"  {S.ERR}✗ No MCP server named '{args[0]}'. See /mcp.{S.R}\n")
                    else:
                        server = touched[0]
                        _refresh_system_prompt(messages)
                        if server.state == "connected":
                            print(f"  {S.OK}✓ '{server.name}' connected: {len(server.tools)} tool(s).{S.R}\n")
                        else:
                            print(f"  {S.ERR}✗ '{server.name}' is {server.state}:{S.R}")
                            for line in _wrapped(server.error):
                                print(f"  {S.MUTED}  {line}{S.R}")
                            print()
            elif sub == "resources":
                print()
                print(mcp_client.list_resources_text(args[0] if args else ""))
                print()
            elif sub == "prompt":
                if len(args) < 2:
                    print(f"  {S.ERR}✗ Usage: /mcp prompt <server> <prompt name> [key=value ...]{S.R}\n")
                else:
                    server = mcp_client.get_server(args[0])
                    if server is None or server.state != "connected":
                        print(f"  {S.ERR}✗ No connected MCP server named '{args[0]}'.{S.R}\n")
                    else:
                        prompt_args = {}
                        for token in args[2:]:
                            key, sep, value = token.partition("=")
                            if sep:
                                prompt_args[key] = value
                        try:
                            fetched = server.get_prompt(args[1], prompt_args)
                        except Exception as e:
                            print(f"  {S.ERR}✗ {server.name}: {e}{S.R}\n")
                        else:
                            injected = mcp_client.prompt_to_text(fetched)
                            print(f"\n  {S.MUTED}─ prompt '{args[1]}' from {server.name}{S.R}")
                            print(_render_full(injected))
                            print()
            elif sub in ("on", "off"):
                config.MCP_ENABLED = sub == "on"
                if config.MCP_ENABLED:
                    _report_mcp_problems(_connect_mcp_servers())
                else:
                    mcp_client.shutdown()
                _refresh_system_prompt(messages)
                _switch("/mcp", sub, config.MCP_ENABLED, "MCP",
                        "the attached servers' tools are offered to the model",
                        on_note=f"{mcp_client.status_summary()}. /mcp lists them.",
                        off_note="No server's tools reach the model, and none is running.")
            else:
                print(f"  {S.ERR}✗ Usage: /mcp [tools|prompts|all|resources|reload|connect <name>|prompt <server> <name>|on|off]{S.R}\n")

            if not injected:
                continue
            user_input = injected
        if cmd == "/perms" or cmd.startswith("/perms "):
            parts = user_input.split(" ", 2)
            sub = parts[1].lower() if len(parts) > 1 else ""
            argument = parts[2].strip() if len(parts) > 2 else ""

            if not sub:
                _show_perms()
            elif sub in ("reload", "refresh"):
                permissions.load_rules(force=True)
                allowed = len(permissions.rules_for("allow"))
                denied = len(permissions.rules_for("deny"))
                print(f"  {S.OK}✓ Permission rules reloaded: {allowed} allow, {denied} deny.{S.R}\n")
                for problem in permissions.errors:
                    print(f"  {S.ERR}✗ {problem}{S.R}")
            elif sub in ("allow", "deny"):
                if not argument:
                    print(f"  {S.ERR}✗ Usage: /perms {sub} <rule>   e.g. /perms {sub} run_cmd(git *){S.R}\n")
                else:
                    saved, where = permissions.add_rule(argument, sub)
                    if saved:
                        print(f"  {S.OK}✓ {sub}: {argument}{S.R} {S.MUTED}({where}){S.R}\n")
                    else:
                        print(f"  {S.ERR}✗ Could not save the rule: {where}{S.R}\n")
            else:
                print(f"  {S.ERR}✗ Usage: /perms [reload|allow <rule>|deny <rule>]{S.R}\n")
            continue
        if cmd == "/think" or cmd.startswith("/think "):
            chosen = _switch(
                "/think", user_input[len("/think"):], config.SHOW_THINKING,
                "Model reasoning",
                "a reasoning model's scratch work is shown as it arrives",
                on_note="It is shown dimmed, and still never kept in the history.",
                off_note="It is hidden. It was never kept in the history either way.")
            if chosen is not None:
                config.SHOW_THINKING = chosen
            continue

        if cmd == "/undo":
            ok, message = git_ops.undo_last()
            colour = S.OK if ok else S.WARN
            print(f"  {colour}{'✓' if ok else '⚠'} {message}{S.R}\n")
            continue

        if cmd == "/autocommit" or cmd.startswith("/autocommit "):
            standing = []
            if not git_ops.repo_root():
                standing.append("This directory is not a git repository, so nothing "
                                "is committed either way.")
            recent = git_ops.recent_ai_commits(5)
            standing += [f"│ {c['sha']}  {c['subject']}  ({c['when']})" for c in recent]
            if recent:
                standing.append("╰─ /undo takes the newest one back")
            chosen = _switch(
                "/autocommit", user_input[len("/autocommit"):], config.GIT_AUTO_COMMIT,
                "Auto-commit",
                "each file an AI tool changes is committed on its own",
                on_note="Each AI edit gets its own commit; /undo takes one back.",
                off_note="AI edits are no longer committed for you.",
                extra=standing)
            if chosen is not None:
                config.GIT_AUTO_COMMIT = chosen
            continue

        if cmd == "/autoverify" or cmd.startswith("/autoverify "):
            standing = [f"│ {name} in {os.path.basename(root) or root}: {reason}"
                        for (name, root), reason in verify.turned_off().items()]
            standing.append("It runs only a check the project already declares - "
                            f"{', '.join(c.name for c in verify.CHECKS)} - and "
                            "never invents one.")
            chosen = _switch(
                "/autoverify", user_input[len("/autoverify"):], config.AUTO_VERIFY,
                "Auto-verify",
                "after a turn changes a file, this project's own check is run and "
                "a failure goes back to the model",
                on_note=("An edit that breaks the project's check comes back to the "
                         f"model with the error, up to {llm_client.MAX_VERIFY_FAILURES} times."),
                off_note="Nothing is run after an edit; checking the work is yours again.",
                extra=standing)
            if chosen is not None:
                config.AUTO_VERIFY = chosen
                # `on` after one turned itself off has to mean "try it again",
                # or the command would report ON and still run nothing.
                if chosen:
                    verify.reset()
            continue

        if cmd == "/deepthink" or cmd.startswith("/deepthink "):
            standing = [f"│ {i}. {stage.title}"
                        for i, stage in enumerate(deepthink.STAGES, 1)]
            standing.append("├─ a request that needs no changes stops after the first.")
            standing.append("╰─ a final check that says it is not done starts again "
                            f"at 1, up to {config.DEEPTHINK_MAX_PASSES} times.")
            chosen = _switch(
                "/deepthink", user_input[len("/deepthink"):], config.DEEPTHINK,
                "Deepthink",
                f"one request becomes {len(deepthink.STAGES)} turns",
                on_note="Say what you want built and it will plan, argue with the "
                        "plan, build it, review the diff, then run it.",
                off_note="Back to one turn per request.",
                extra=standing)
            if chosen is not None:
                config.DEEPTHINK = chosen
            continue

        if cmd == "/agents" or cmd.startswith("/agents "):
            _agents_command(user_input[len("/agents"):])
            continue

        if cmd == "/remote" or cmd.startswith("/remote "):
            _remote_command(user_input[len("/remote"):])
            continue

        if cmd == "/vm" or cmd.startswith("/vm "):
            _vm_command(user_input[len("/vm"):])
            continue

        if cmd == "/set" or cmd.startswith("/set "):
            # The user's own casing, not `cmd`: a string setting keeps what
            # they typed, and only the name is case-insensitive.
            _set_command(user_input[len("/set"):], messages)
            continue

        # Last of the commands on purpose: `/tdd <request>` arms the lock and
        # then *falls through* with the request as the message, so it has to
        # sit where falling through lands on the fall-through.
        if cmd == "/tdd" or cmd.startswith("/tdd "):
            rest = user_input[len("/tdd"):].strip()
            if rest.lower() == "off":
                dropped = permissions.release(_TDD_LABEL)
                config.TDD_LOCK = False
                print(f"  {S.INFO}✓ /tdd lifted.{S.MUTED}"
                      f"{f' {len(dropped)} rules dropped.' if dropped else ''}{S.R}\n")
                continue
            if not rest:
                _arm_tdd()
                print(f"  {S.MUTED}Send your request now, or /tdd off to lift "
                      f"it.{S.R}")
                print()
                continue
            _arm_tdd()
            print()
            user_input = rest          # and on to the fall-through below

        # Every slash command has had its turn and continued; what is left is a
        # message for the model, so this is where an `@path` becomes context.
        # Before the plan-mode note, so the attachment stays under the sentence
        # the user wrote rather than under a system aside.
        user_input, attached_images = _attach_mentions(user_input)

        if config.PLANMODE and not config.DEEPTHINK:
            if config.AUTO_ALLOW:
                plan_prompt = (
                    "\n\n[System Note: PLAN MODE is ON. For complex tasks or file modifications, you MUST use the `submit_plan_for_approval` tool before executing changes. Since AUTOMODE is ON, it will auto-approve. Follow your blueprint and verify afterwards.]"
                )
            else:
                plan_prompt = (
                    "\n\n[System Note: PLAN MODE is ON. For complex tasks, system changes, or file modifications:\n"
                    "1. Explore the codebase using search/read tools.\n"
                    "2. You MUST call the `submit_plan_for_approval` tool to present your blueprint and wait for the tool's result.\n"
                    "3. DO NOT use edit/write/run_cmd tools until the plan is approved via the tool's return value.\n"
                    "For simple conversational queries, you may answer directly.]"
                )
            user_input += plan_prompt

        if config.TDD_LOCK:
            # Told as well as enforced. The refusal alone would work, but it
            # costs a tool call to discover, and a model that knows the test is
            # off limits spends that call on the code instead. The last
            # sentence is the escape hatch: sometimes the test really is wrong,
            # and saying so is a better answer than six tries at satisfying it.
            user_input += (
                "\n\n[System Note: TDD MODE is ON for this request. This "
                "project's test files are locked - edit_file, write_file, "
                "delete_file and copy_file are refused on them, and trying "
                "will not work. Make the failing test pass by changing the "
                "code it tests. If you conclude the test itself is wrong, say "
                "so and stop; do not work around it.]")

        # Loading an MCP server changes what belongs in the system prompt: the
        # index should stop announcing a server whose tools are now listed, and
        # over the text protocol the prompt is where those tools live. The
        # prompt is not rebuilt every turn - a stable prefix is what the hosted
        # providers cache - so it is rebuilt exactly when that set has moved.
        if sorted(config.LOADED_MCP_SERVERS) != mcp_servers_in_prompt:
            _refresh_system_prompt(messages)
            mcp_servers_in_prompt = sorted(config.LOADED_MCP_SERVERS)

        # What the other agents said reaches the model here, ahead of the user's
        # own message so that the request stays the last thing in the history.
        # The person has already seen these at the prompt - the two cursors are
        # separate - so this only says that they were passed on.
        channel.heartbeat(_agent_label())
        arrived = channel.turn_note()
        if arrived:
            messages.append({"role": "user", "content": arrived})
            print(f"  {S.MUTED}↪ passed the channel messages to the model{S.R}")
        # From here the turn is judged on whether it does anything. `_read_line`
        # reads the verdict before opening the next prompt, and it decides both
        # whether the channel may press Enter again and - through
        # `channel.talk_streak` - whether another message may be sent at all.
        channel.reset_work()
        # The anchor every channel note ends on, taken only from a line a person
        # typed. A turn the channel started by itself carries a peer's question,
        # and letting that become "the job" is precisely the drift the anchor
        # exists to stop - the boat would be steering itself up the mountain.
        if not _auto_woke:
            channel.set_job(user_input)

        # One turn starts here and covers everything done to answer it - the
        # tool loop's follow-up requests, deepthink's six stages, any sub-agent -
        # so `/usage` reports what the question cost rather than what its last
        # request cost.
        config.next_turn()
        # The remote's own badge: a phone that cannot tell "thinking" from
        # "finished and silent" is a phone you have to keep refreshing.
        remote.set_busy(True)
        # Nothing should be waiting to be looked at when a turn begins. A
        # `view_image` whose turn died before its result was appended would
        # otherwise hang its picture on a later, unrelated message.
        images.forget_pending()
        turn = {"role": "user", "content": user_input}
        if attached_images:
            # Beside `content`, never inside it: invariant 5.3 wants the stored
            # history to stay plain text, and these are paths rather than bytes
            # so a saved session does not carry a screenshot around forever.
            turn["images"] = attached_images
        messages.append(turn)
        config.repair_messages(messages)
        current_session_id = save_session(messages, current_session_id)

        driven_elsewhere = providers.current().drives_turns
        try:
            if driven_elsewhere:
                # Attacca runs the loop - and its own context - on its side;
                # this end runs the tools it asks for. Nothing here to compact,
                # and no six local stages for deepthink to run.
                if config.DEEPTHINK:
                    print(f"  {S.MUTED}◆ /deepthink is for a model this harness "
                          f"drives; Attacca runs its own loop.{S.R}")
                from aetheris import attacca
                result = await attacca.run_turn(messages)
            elif config.DEEPTHINK:
                # deepthink drives its own turns, and manages context between
                # them - it is several passes over one request, not one.
                result = await deepthink.run(messages)
            else:
                await manage_context(messages)
                result = await chat_turn(messages)

            current_session_id = save_session(messages, current_session_id)

            # Name the session once, from the exchange that just finished. Not
            # for Attacca: a title is a side question to a model, there is no
            # model here to ask, and Attacca names its own sessions anyway.
            if (config.AUTO_TITLE and not config.SESSION_TITLE and current_session_id
                    and not driven_elsewhere):
                sys.stdout.write(f"  {S.MUTED}✎ naming session…{S.R}")
                sys.stdout.flush()
                title = await generate_session_title(messages)
                sys.stdout.write("\r\033[K")
                sys.stdout.flush()
                if title:
                    current_session_id = rename_session(current_session_id, title)
                    print(f"  {S.MUTED}✎ session titled: {S.GRAY}{config.SESSION_TITLE}{S.R}\n")

        except Exception as e:
            # Wrapped rather than run off the edge: this catches a provider
            # refusing the request, and that message names the key, the quota
            # or the model id that has to change.
            print()
            connect._print_problem("Error", e)
        finally:
            remote.set_busy(False)
            _report_usage(messages)
            # `/tdd` is armed for one request and lifts itself here - including
            # when the turn ended in an error or the user interrupted it. A
            # lock that outlives what it was asked for is a lock nobody
            # remembers turning on.
            if config.TDD_LOCK:
                permissions.release(_TDD_LABEL)
                config.TDD_LOCK = False
                print(f"  {S.MUTED}◆ /tdd lifted - the test files are writable "
                      f"again.{S.R}\n")


def _use_utf8_output() -> None:
    """Make sure the harness can print its own interface.

    The TUI is drawn with box characters - the tool call alone uses U+25B8 and
    U+2570 - and an answer is routinely not ASCII either. A Windows console
    handles those, but a *pipe* on Windows does not: Python falls back to the
    locale code page there, cp1252 or cp949, and the first tool call raises
    UnicodeEncodeError halfway through drawing itself. Redirecting the output
    to a file should not crash the program.

    `errors="replace"` rather than "strict" for the same reason: a character
    the terminal genuinely cannot show is worth one replacement glyph, never a
    traceback in the middle of an answer.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass          # not a real stream, or an encoding it will not take


def _parse_args(argv: list) -> argparse.Namespace:
    """Answer `--help` and `--version`, and take the session to pick up from.

    Almost everything is a slash command once the harness is running; what
    cannot be is which conversation to open, because by the time there is a
    prompt to type at, a new one has already been started. So the two ways of
    naming one live here: `--resume` for a session you can name, and `-c` for
    the one you were last in, in this directory.

    Both spellings of each are accepted. `-resume` is the single dash the
    request was written with, and `--resume` is what anyone who has used
    another CLI will reach for first; refusing either would be pedantry.
    """
    parser = argparse.ArgumentParser(
        prog="aetheris",
        description="A terminal AI assistant built for small local models.",
        epilog="Run with no arguments to start a new session. Everything else is a "
               "slash command inside it - type /help there for the list.")
    parser.add_argument("-V", "--version", action="version",
                        version=f"aetheris {__version__}")
    picked = parser.add_mutually_exclusive_group()
    picked.add_argument("-r", "-resume", "--resume", metavar="ID", dest="resume",
                        help="resume a saved session by id or title (see /sessions)")
    picked.add_argument("-c", "-continue", "--continue", dest="continue_here",
                        action="store_true",
                        help="resume the newest session last worked on in this directory")
    return parser.parse_args(argv)


def _session_to_resume(args: argparse.Namespace) -> str:
    """The session id the command line asks for, or "" for a new conversation.

    Neither flag falls back to a fresh session when it cannot find one, and
    neither guesses between candidates: being dropped into an empty prompt when
    you asked to continue something is how an afternoon's context gets lost
    quietly. Both say what they looked for and stop.
    """
    if getattr(args, "continue_here", False):
        here = os.getcwd()
        sid = latest_in_dir(here)
        if not sid:
            print(f"\n  {S.ERR}✗ No saved session was last worked on here:{S.R} {S.WHITE}{here}{S.R}")
            print(f"  {S.GRAY}Run with no arguments to start one, or name another with "
                  f"{S.ACCENT}--resume <id>{S.GRAY}.{S.R}\n")
            raise SystemExit(1)
        return sid

    query = (args.resume or "").strip()
    if not query:
        return ""
    matches = find_sessions(query)
    if not matches:
        print(f"\n  {S.ERR}✗ No saved session matches: {query}{S.R}")
        print(f"  {S.GRAY}Sessions are listed by {S.ACCENT}/sessions{S.GRAY} inside one.{S.R}\n")
        raise SystemExit(1)
    if len(matches) > 1:
        print()
        _show_ambiguous(query, matches, "Re-run with one of the ids above.")
        raise SystemExit(1)
    return matches[0][0]


def cli() -> None:
    """The `aetheris` command, and what `python -m aetheris` runs.

    `main()` is a coroutine, and a console-script entry point has to be an
    ordinary function - so the event loop and the two exits that are not errors
    are handled here rather than under `__main__`, where an installed copy
    would never reach them.
    """
    _use_utf8_output()
    args = _parse_args(sys.argv[1:])
    # Before anything is started, and before the first turn can ask to run a
    # command: what this does to the machine it is on. Asked once per machine.
    if not terms.require():
        raise SystemExit(1)
    # Resolved here rather than inside `main()`: a name that matches nothing, or
    # matches several, is a mistake in the command that was typed, and the place
    # to answer it is before a screen has been cleared and servers started.
    resume_id = _session_to_resume(args)
    try:
        asyncio.run(main(resume_id))
    except KeyboardInterrupt:
        print(f"\n\n  {S.GRAY}Goodbye!{S.R}\n")
    except Exception as e:
        print(f"\n  {S.ERR}✗ Unexpected error: {e}{S.R}")


if __name__ == "__main__":
    cli()
