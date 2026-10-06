import os
import re
import json
import subprocess
import time
import shutil
import hashlib
import inspect
import requests
import psutil
from aetheris import atomic
from aetheris import config
from aetheris.config import S, TREE_SITTER_AVAILABLE, _TS_LANGUAGES, _EXT_TO_LANG
from aetheris.tui import _fmt_tool_call, _approval_prompt
from aetheris.skills import handle_use_skill
from aetheris import mcp_client
from aetheris import channel
from aetheris import images
from aetheris import git_ops
from aetheris import permissions
from aetheris import shell_session
from aetheris import toolspec
from aetheris import vault
from aetheris import verify
from aetheris import vm
from aetheris.websearch import search_web as search_pipeline, strip_html

if TREE_SITTER_AVAILABLE:
    from aetheris.config import Parser, Query, QueryCursor


def handle_search_web(query: str) -> str:
    return search_pipeline(query)


def _trim_output(text: str) -> str:
    limit = config.CMD_OUTPUT_CHARS
    text = text.rstrip()
    if len(text) <= limit:
        return text
    return ("...[earlier output trimmed]\n" + text[-limit:])


def _session_result(session, output: str, ended: bool, timed_out: bool) -> str:
    """Turn one read from a live command into the text the model sees."""
    output = _trim_output(output)

    if timed_out:
        session.close()
        return (f"[Error] '{session.command}' produced output for {config.CMD_TIMEOUT}s "
                "without stopping, so it was killed. Do not run it again unless you "
                "narrow it down."
                + (f"\n\n{output}" if output else ""))

    if ended:
        code = session.close()
        if code:
            return f"[Error] Command failed (exit code {code}).\n{output}" if output \
                else f"[Error] Command failed (exit code {code})."
        return output or "(the command produced no output)"

    # Still running and gone quiet: from out here that is what a program sitting
    # at a prompt looks like.
    shell_session.register(session)
    return (f"{output}\n\n"
            f"[Waiting] '{session.command}' is still running and has printed nothing for "
            f"{config.CMD_IDLE_TIMEOUT:g}s, so it is most likely waiting for input. "
            f"Read the output above and answer it with send_input:\n"
            f'    {{"name": "send_input", "arguments": {{"session": "{session.id}"}}}}\n'
            "    <stdin>\n    <your answer>\n    </stdin>\n"
            "Send an empty <stdin> block to just wait for more output, or call "
            f'end_process with session "{session.id}" to stop it.').strip()


def safe_run_cmd(command_string: str, stdin_text: str = "") -> str:
    """Run a shell command, and stay connected to it while it runs.

    The command is handed to the shell as written. It used to be split with
    `shlex` and then passed to `shell=True`, which mangled Windows paths (shlex
    eats backslashes) and left the shell to re-quote a list it had never been
    given - so `run_cmd` and what actually ran could differ.

    The process is not waited on to completion. Its output is drained as it
    appears, and when it goes quiet the output so far is returned along with the
    session id, so the model can read the prompt and answer it with `send_input`.
    That is the only way an interactive program can work here: its prompts are
    captured, so the user never sees them, and letting it hold the terminal just
    froze the app with nothing on screen.
    """
    command = (command_string or "").strip()
    if not command:
        return "[Error] Empty command."

    stdin_text = stdin_text if isinstance(stdin_text, str) else ""

    details = [("command", command)]
    if stdin_text.strip():
        preview = stdin_text if len(stdin_text) <= 200 else stdin_text[:200] + "...[truncated]"
        details.append(("input", preview.replace("\n", " ⏎ ").strip()))
    if not _approval_prompt("Run Command", details, rule=f"run_cmd({command})"):
        return "[System] User denied command execution."

    try:
        session = shell_session.start(command)
    except Exception as e:
        return f"[Error] Failed to execute command: {e}"

    if stdin_text.strip():
        session.send(stdin_text)

    try:
        output, ended, timed_out = session.read_until_idle(
            config.CMD_IDLE_TIMEOUT, time.time() + config.CMD_TIMEOUT)
    except KeyboardInterrupt:
        session.close()
        return "[System] The command was cancelled by the user."

    return _session_result(session, output, ended, timed_out)


def handle_send_input(session_id: str, text: str) -> str:
    """Answer a running command's prompt and read what it says next."""
    shell_session.prune()
    session = shell_session.get(session_id)
    if session is None:
        running = shell_session.active()
        if not running:
            return ("[Error] No command is waiting for input. Start one with run_cmd "
                    "first - a command that has already finished cannot be resumed.")
        listing = ", ".join(f"'{s.id}' ({s.command})" for s in running)
        return f"[Error] No session '{session_id}'. Currently waiting: {listing}"

    text = text if isinstance(text, str) else str(text or "")
    details = [("session", f"{session.id}  ({session.command})"),
               ("input", text.strip().replace("\n", " ⏎ ") or "(nothing - just wait)")]
    if not _approval_prompt("Send Input", details, rule=f"send_input({session.command})"):
        return "[System] User denied sending input."

    if text.strip() and not session.send(text):
        code = session.close()
        return (f"[Error] '{session.command}' had already exited (code {code}), "
                "so the input went nowhere.")

    try:
        output, ended, timed_out = session.read_until_idle(
            config.CMD_IDLE_TIMEOUT, time.time() + config.CMD_TIMEOUT)
    except KeyboardInterrupt:
        session.close()
        return "[System] The command was cancelled by the user."

    return _session_result(session, output, ended, timed_out)


def handle_end_process(session_id: str) -> str:
    session = shell_session.get(session_id)
    if session is None:
        return f"[Error] No running session '{session_id}'."
    command, sid = session.command, session.id
    session.close()
    return f"[Success] Stopped '{command}' (session {sid})."


# How much of the digest an anchor carries. Two characters is 256 values, and
# an anchor is checked against a *position* - so a line that has moved away and
# a different line that has moved in collide once in 256, and that collision is
# a silent overwrite of code the model never read. Three is 4096, for one more
# character per line: on this repository's largest file that is 465 tokens on a
# 23,600-token listing, 2%.
#
# The patterns below accept two *or* three, on purpose. A two-character digest
# can never equal a three-character hash, so it is refused either way - but
# being read as an anchor at all is what turns "old_content was not found" into
# "line 50 is not what 50:1f says it is; it now reads 50:1fa|...", which is the
# difference between a model that corrects itself and one that resends.
HASHLINE_DIGITS = 3

_HASHLINE_PATTERN = re.compile(r'^\d+:[0-9a-f]{2,3}\|')

def _existing_newline(filepath: str) -> str:
    """The line ending the file already uses, or "\\n" for one that is not there.

    Every read here goes through Python's universal newlines, so a CRLF file
    arrives as `\\n` and the information is gone by the time it is written back.
    `open(path, "w")` then translates to `os.linesep`, which on Windows turns a
    file with Unix endings into a file with Windows endings from top to bottom -
    a one-line edit that shows up in `git diff` as every line changed. Asking
    the file first is what keeps an edit to the size of the edit.
    """
    try:
        with open(filepath, "rb") as f:
            head = f.read(65536)
    except OSError:
        return "\n"
    return "\r\n" if b"\r\n" in head else "\n"


def _would_erase_a_secret(filepath: str, text: str) -> list:
    """Secrets the file holds now that this write would replace with a name.

    Only the ones actually at risk: a `.env.example` full of placeholders names
    no secret it is standing on, so it is written without complaint.
    """
    named = vault.used_in({"body": text})
    if not named:
        return []
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as handle:
            existing = handle.read()
    except OSError:
        return []                   # a new file replaces nothing
    secrets = vault.known()
    return [name for name in named
            if secrets.get(name) and secrets[name] in existing]


def _write_file(filepath: str, text: str, newline: str = "\n") -> str:
    """Replace a file with `text`, all at once or not at all. "" or the problem.

    Through `atomic`, for the reason that module exists: `open(path, "w")`
    truncates first and writes second, so a crash, a full disk or a Ctrl-C in
    between leaves the user with half of their file - and every tool here is
    writing a file somebody has, not one it is creating from nothing.

    A missing parent directory stays an error rather than being created on the
    way past. `atomic.write_text` makes one because its own callers - settings,
    sessions, the agent board - own their directories; here the path came from
    a model, and quietly building `src/utils/helpers/` because it guessed at a
    layout is how a tree grows directories nobody asked for. `create_dir` is
    the tool for that, and it asks first.
    """
    directory = os.path.dirname(os.path.abspath(filepath))
    if not os.path.isdir(directory):
        return (f"[Error] Cannot write file: [Errno 2] No such file or directory: "
                f"{filepath!r} - {directory} does not exist. create_dir it first.")

    overwritten = _would_erase_a_secret(filepath, text)
    if overwritten:
        # A placeholder is never expanded into a file (vault.FILLED_IN), which
        # is what stops a secret being copied out through one. The other half
        # of that rule is here: writing the placeholder *over* the real value
        # would replace the key with the word `{{env:NAME}}` and lose it - and
        # the model, which cannot see either, would have no idea it had.
        return (f"[Error] Nothing was written. {filepath} currently holds the real "
                f"value of {', '.join(overwritten)}, and this would put the "
                f"placeholder there instead - which would destroy "
                f"{'it' if len(overwritten) == 1 else 'them'}. A secret is not "
                f"yours to rewrite: leave the line alone, or tell the user what "
                f"you would change and let them edit it.")
    if newline != "\n":
        text = text.replace("\n", newline)
    try:
        atomic.write_text(filepath, text)
    except Exception as e:
        return f"[Error] Cannot write file: {e}"
    return ""


def _line_hash(line: str) -> str:
    return hashlib.md5(line.encode("utf-8")).hexdigest()[:HASHLINE_DIGITS]

def _encode_hashlines(content: str) -> str:
    lines = content.split("\n")
    result = []
    for i, line in enumerate(lines, 1):
        h = _line_hash(line)
        result.append(f"{i}:{h}|{line}")
    return "\n".join(result)

def _strip_hashlines(content: str) -> str:
    lines = content.split("\n")
    if not lines:
        return content

    non_empty = [l for l in lines if l.strip()]
    if not non_empty:
        return content
    matched = sum(1 for l in non_empty if _HASHLINE_PATTERN.match(l))
    ratio = matched / len(non_empty)

    if ratio < 0.8:
        return content

    stripped = []
    for line in lines:
        m = _HASHLINE_PATTERN.match(line)
        if m:
            stripped.append(line[m.end():])
        else:
            stripped.append(line)
    return "\n".join(stripped)


# ---------------------------------------------------------------------------
# run_python: the scratch VM (see vm.py for why it is not just `run_cmd python`)
# ---------------------------------------------------------------------------

_VM_NAMES_SHOWN = 12


def _trim_both_ends(text: str, limit: int) -> str:
    """Trim from the middle. The first lines and the last lines both matter.

    `_trim_output` keeps the tail, which is right for a command still printing.
    Here the whole run is over, and a loop that printed ten thousand lines is
    understood from how it started and how it ended - invariant 5.6b, applied
    to a tool result rather than to the context budget.
    """
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return (text[:head] + f"\n...[{len(text) - limit} characters trimmed]...\n"
            + text[-tail:])


def _truthy(value) -> bool:
    """`reset` arrives as a real boolean, or as the word for one.

    It has to be read carefully rather than with `bool()`: a model that sends
    `"reset": "false"` means the opposite of what the string is worth, and
    getting that wrong throws away every variable the model was working with.
    """
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1", "on")
    return bool(value)


def _vm_footer(reply: dict) -> str:
    """What the model most needs to know afterwards: what it still has."""
    new_names = [name for name in (reply.get("new") or []) if name]
    if not new_names:
        return ""
    shown = ", ".join(new_names[:_VM_NAMES_SHOWN])
    if len(new_names) > _VM_NAMES_SHOWN:
        shown += f", and {len(new_names) - _VM_NAMES_SHOWN} more"
    return f"\n\n(kept for your next run_python call: {shown})"


def handle_run_python(content: str, stdin: str = "", reset: bool = False) -> str:
    """Run a snippet in the scratch VM and say what came back."""
    code = _strip_hashlines(content if isinstance(content, str) else "")
    stdin_text = stdin if isinstance(stdin, str) else ""

    if not code.strip():
        return ("[Error] The call carried no code, so nothing was run. Send "
                "run_python again with the Python in a <content> block right "
                "after the JSON object.")

    lines = code.strip().splitlines()
    preview = "\n".join(lines[:6]) + ("\n..." if len(lines) > 6 else "")
    details = [("code", preview), ("in", vm.scratch_dir())]
    if stdin_text.strip():
        details.append(("input", stdin_text.replace("\n", " ⏎ ").strip()))
    if not _approval_prompt("Run Python", details, rule="run_python"):
        return "[System] User denied running the code."

    reply = vm.run(code, stdin_text, _truthy(reset))
    printed = _trim_both_ends(reply.get("output") or "", config.VM_OUTPUT_CHARS)

    # The VM was stopped, so the namespace is gone. Say that outright: a model
    # told only "it failed" goes on referring to a variable that no longer
    # exists, and the next error is a NameError that explains nothing.
    if reply.get("timeout"):
        return (f"[Error] The code was still running after {reply['timeout']:g}s, so the "
                "Python VM was stopped and restarted - every variable from earlier "
                "calls is gone. Look for a loop that never ends, or split the work up."
                + (f"\n\nIt printed this first:\n{printed}" if printed.strip() else ""))
    if reply.get("crashed"):
        detail = reply.get("detail") or ""
        return (f"[Error] {reply['crashed']}. It has been restarted, so every variable "
                "from earlier calls is gone."
                + (f"\n{detail}" if detail else "")
                + (f"\n\nIt printed this first:\n{printed}" if printed.strip() else ""))

    if reply.get("error"):
        head = f"It printed this before it failed:\n{printed}\n\n" if printed.strip() else ""
        return f"[Error] {head}{reply['error']}"

    parts = [f"[Success] ran in {reply.get('seconds', 0.0):.2f}s."]
    if printed.strip():
        parts.append(printed.rstrip())
    if reply.get("value") is not None:
        parts.append(f"=> {reply['value']}")
    if len(parts) == 1 and not reply.get("new"):
        parts.append("The code ran and produced nothing. To see a value, print() it "
                     "or end the snippet with the expression on its own line.")
    return "\n\n".join(parts) + _vm_footer(reply)




# ---------------------------------------------------------------------------
# editing by hashline anchor
# ---------------------------------------------------------------------------
#
# `read_file` returns every line as `50:1fa|print(answer)`, and until now that
# prefix was only a display aid: `edit_file` stripped it off and matched what
# was left as text. So the model still had to reproduce the line exactly - every
# space of indentation, every quote, every backslash - which is the single thing
# a small model gets wrong most often. And a line that appears twice anywhere in
# the file could not be edited at all: the snippet was ambiguous, and the edit
# was refused rather than guessed at.
#
# The prefix is enough on its own. `50` says which line to replace, and `1f`
# proves the model is looking at the version of it that is on disk right now.
# So an `old_content` made only of prefixes names its lines directly and the
# text is never retyped - and because a line number is unique, "found twice"
# stops being a possible answer.
#
# The hash is what makes this safe rather than merely convenient. A line number
# on its own would happily point at whatever has since moved into that position;
# with the hash, an edit against a stale reading of the file is refused and the
# model is told what is actually there now.

# `50:1fa`, or the whole row as read_file printed it: `50:1fa|print(answer)`.
_ANCHOR = re.compile(r'^\s*(\d+)\s*:\s*([0-9a-fA-F]{2,3})\s*(?:\|(.*))?$')
# A span, both ends verified: `50:1fa-53:9c0`.
_ANCHOR_SPAN = re.compile(r'^\s*(\d+)\s*:\s*([0-9a-fA-F]{2,3})\s*-\s*'
                          r'(\d+)\s*:\s*([0-9a-fA-F]{2,3})\s*$')

# The same span with the pipes still on it: `50:1fa-53:9c0|`, or
# `50:1fa|def f():-53:9c0|    return`. `read_file` prints a `|` after every
# anchor it produces, so a model writing a span puts one there too - it is
# generalising from the only example it has been shown. The strict pattern
# above is tried first and this one only when it misses, so what a correct
# span means does not change; this reads a spelling that used to fall through
# to text matching and come back as "old_content was not found", which says
# nothing about the anchor being one character off. Both ends are still
# hash-checked, and `_anchor_target` only takes a reading that *verifies*, so a
# row that splits in the wrong place loses to whichever reading describes the
# file. Non-greedy on the first text: split at the first `-<line>:<hash>`.
_ANCHOR_SPAN_PIPED = re.compile(r'^\s*(\d+)\s*:\s*([0-9a-fA-F]{2,3})\s*(?:\|.*?)?-\s*'
                                r'(\d+)\s*:\s*([0-9a-fA-F]{2,3})\s*(?:\|.*)?$')

# A row that names a line and quotes it but carries no hash: `3|    return x`
# or `3     return x`. Exactly one separator is consumed, so the line's own
# indentation survives into the text and can be compared with what is on disk.
# The quoted text is the whole of the evidence here - see `_anchor_problem`.
_ANCHOR_NO_HASH = re.compile(r'^[ \t]*(\d+)[ \t|](.*)$')


def _parse_anchors(old_content: str):
    """The lines an `old_content` *names*, or None if it quotes them instead.

    Returns `(first, last, checks, spanned)`. `checks` is the (line, hash, text)
    triples to verify - for a span only the two ends, since nobody can be asked
    to list every line between them.

    One ordinary line anywhere in the snippet makes the whole thing text: this
    has to be certain before it takes over, because falling through to the old
    behaviour is always safe and taking over wrongly is not.
    """
    rows = [row for row in (old_content or "").split("\n") if row.strip()]
    if not rows:
        return None

    def span_of(match):
        first, first_hash, last, last_hash = match.groups()
        return (int(first), int(last),
                [(int(first), first_hash.lower(), None),
                 (int(last), last_hash.lower(), None)], True)

    if len(rows) == 1:
        span = _ANCHOR_SPAN.match(rows[0])
        if span:
            return span_of(span)

    checks = []
    for row in rows:
        one = _ANCHOR.match(row)
        if not one:
            checks = None
            break
        number, digest, text = one.groups()
        checks.append((int(number), digest.lower(), text))
    if checks:
        return (checks[0][0], checks[-1][0], checks, False)

    return None


def _parse_piped_span(old_content: str):
    """A span still wearing the pipes `read_file` printed, or None.

    Kept apart from `_parse_anchors` because it is genuinely ambiguous with it:
    `6:cae|def f():-9:964|` reads as a span, and also as one anchor whose quoted
    text happens to contain `-9:964|`. Neither is silly, so this does not guess
    - `_anchor_target` tries both against the file and keeps whichever one
    describes it.
    """
    rows = [row for row in (old_content or "").split("\n") if row.strip()]
    if len(rows) != 1:
        return None
    span = _ANCHOR_SPAN_PIPED.match(rows[0])
    if not span:
        return None
    first, first_hash, last, last_hash = span.groups()
    return (int(first), int(last),
            [(int(first), first_hash.lower(), None),
             (int(last), last_hash.lower(), None)], True)


def _parse_unhashed_anchors(old_content: str):
    """Rows that name a line and quote it but carry no hash, or None.

    Same shape `_parse_anchors` returns, with `None` where the hash would be -
    which is what makes `_anchor_problem` insist on the quoted text for these
    rows. A line number on its own points at whatever has since moved into that
    position, and the quote is the only thing that says otherwise. `3 return x`
    is also what an ordinary snippet out of a numbered list looks like, so a
    reading that does not describe the file is not an error here: it goes back
    to being matched as text, exactly as it was before this existed.
    """
    rows = [row for row in (old_content or "").split("\n") if row.strip()]
    if not rows:
        return None
    checks = []
    for row in rows:
        one = _ANCHOR_NO_HASH.match(row)
        if not one:
            return None
        number, text = one.groups()
        checks.append((int(number), None, text))
    return (checks[0][0], checks[-1][0], checks, False)


def _show_lines(lines: list, numbers) -> str:
    """The lines named, in the shape `read_file` prints them.

    So a refusal hands back something that can be copied straight into the next
    call rather than costing a `read_file` round trip to go and look.
    """
    shown = []
    for number in sorted({n for n in numbers if 1 <= n <= len(lines)}):
        line = lines[number - 1]
        shown.append(f"  {number}:{_line_hash(line)}|{line[:160]}")
    return "\n".join(shown)


# The anchored row with the hash left off: `38|print()`. The pipe is required
# here - `38 print()` as *new* text is far more likely to be a line that really
# starts with a number than an anchor somebody forgot half of.
_PATCH_NO_HASH = re.compile(r'^[ \t]*(\d+)\|(.*)$')


def _no_old_content(filepath: str, file_content: str, new_content: str) -> str:
    """Nothing was named to replace. Say which of the two mistakes it was.

    A `new_content` of `38|print()` is the anchored row minus its hash, and the
    text after the `|` is what the line is to *become* - so there is nothing
    here that says the model ever read line 38, and nothing to check it against.
    It is refused rather than confirmed: the hash is what makes this form safe,
    and a model that is merely asked "are you sure?" says yes. But the refusal
    carries the lines it seems to have meant, in the shape read_file prints
    them, so the next call can be right without going back to look.

    Left alone otherwise, so a call that meant something else gets the same
    error it always did.
    """
    rows = [row for row in (new_content or "").split("\n") if row.strip()]
    matched = [_PATCH_NO_HASH.match(row) for row in rows]
    lines = file_content.split("\n")
    if rows and all(matched):
        numbers = [int(m.group(1)) for m in matched]
        listing = _show_lines(lines, numbers)
        if listing:
            return (f"[Error] Nothing was written. Those rows name lines but carry no "
                    f"hash, and the text after the `|` is what the line is to become - "
                    f"so there is nothing here that shows you have read what is "
                    f"already on {'them' if len(numbers) > 1 else 'it'}. "
                    f"{filepath} currently has:\n{listing}\n"
                    f"Send it again with each anchor exactly as it appears above - "
                    f"{numbers[0]}:{_line_hash(lines[numbers[0] - 1])}|<the new line> - "
                    f"or read_file for the rest.")
    return ("[Error] old_content was empty, so nothing was named to replace. "
            "Either put the hashline anchors of the lines in old_content "
            "(e.g. 50:1fa), or put the anchor in front of each new line "
            "(50:1fa|<the new line>) and leave old_content out.")


def _quoted_wrong(file_content: str, old_content: str) -> str:
    """What the lines an unverified hash-less row named actually say, or "".

    Reached when `old_content` looked like `3 return x` and line 3 does not say
    that, so it was matched as text and the text was not there either. Both
    readings failed, and the model is told nothing about why - which is the loop
    a small model gets stuck in. This adds the half it cannot see.
    """
    unhashed = _parse_unhashed_anchors(old_content)
    if unhashed is None:
        return ""
    lines = file_content.split("\n")
    listing = _show_lines(lines, [number for number, _, _ in unhashed[2]])
    if not listing:
        return ""
    return (f"\nIf you meant to name lines by number, they need the hash too. "
            f"They currently read:\n{listing}")


def _anchor_target(filepath: str, file_content: str, old_content: str) -> tuple:
    """Resolve `old_content` to a span of lines.

    Three outcomes, and the caller needs to tell them apart:

        (None, None, "")       not anchors at all - match it as text, as before
        (None, None, <error>)  anchors, but they do not describe the file
        (start, end, "")       these lines, verified against what is on disk
    """
    lines = file_content.split("\n")

    # A row can be readable more than one way, and which one was meant is not
    # decidable from the row - `6:cae|def f():-9:964|` is both a span and one
    # anchor with an odd quoted text. So nothing here guesses: each reading is
    # checked against the file and the first that *describes* it wins. When
    # none does, the complaint is about the first reading, which is the one the
    # model most likely meant.
    readings = [reading for reading in (_parse_anchors(old_content),
                                        _parse_piped_span(old_content),
                                        _parse_unhashed_anchors(old_content))
                if reading is not None]
    if not readings:
        return None, None, ""

    problem = ""
    for index, reading in enumerate(readings):
        complaint = _anchor_problem(filepath, lines, reading)
        if not complaint:
            return reading[0], reading[1], ""
        if not index:
            problem = complaint

    # A hash-less row that does not describe the file is not an error: it is
    # ordinary text that happens to start with a number, and matching it as
    # text is what happened before this reading existed. Falling back is always
    # safe; taking over wrongly is not.
    if all(digest is None for _, _, checks, _ in readings for _, digest, _ in checks):
        return None, None, ""
    return None, None, problem


def _anchor_problem(filepath: str, lines: list, reading: tuple) -> str:
    """"" if this reading describes the file, else what is wrong with it."""
    first, last, checks, spanned = reading

    if last < first:
        return (f"[Error] The anchors run backwards: {first} comes after "
                f"{last}. Name the first line of the span first.")
    out_of_range = next((n for n, _, _ in checks if n < 1 or n > len(lines)), 0)
    if out_of_range:
        return (f"[Error] There is no line {out_of_range} in {filepath} - "
                f"it has {len(lines)} lines. read_file it again and take "
                f"the anchors from that listing.")
    if not spanned:
        numbers = [n for n, _, _ in checks]
        if numbers != list(range(first, last + 1)):
            return (f"[Error] The anchors {', '.join(str(n) for n in numbers)} are not one "
                    f"unbroken run of lines. Either list every line from {first} to {last}, "
                    f"or write the span as {first}:{checks[0][1]}-{last}:{checks[-1][1]}.")

    # When the row quotes the line, the quote decides; the hash decides only
    # when there is nothing else. The hash is a few hand-copied characters and
    # the line is not, so a line that reads exactly right is the stronger
    # evidence when they disagree - and, in the other direction, a hash that
    # agrees with a line the model quoted differently is not evidence at all.
    # Three characters collide once in 4096 and the file is edited over and
    # over, so that is not a hypothetical: the quoted line is what catches it.
    # Trailing whitespace is invisible and routinely dropped in copying;
    # leading whitespace is the indentation and has to match.
    for number, digest, text in checks:
        line = lines[number - 1]
        if text is not None:
            if text.rstrip() == line.rstrip():
                continue
            # A line carrying a secret was shown to the model with the value
            # replaced, so the redacted spelling is the *only* one it can quote
            # back. Without this the model quotes exactly what it was given,
            # is told the line "is not what you say it is", is shown the same
            # redacted line again, and has nowhere to go - the refusal loop
            # this file exists to avoid. The hash is still the real line's, so
            # the proof that it read the current file is untouched.
            if text.rstrip() == vault.redact(line).rstrip():
                continue
        elif _line_hash(line) == digest:
            continue
        return (f"[Error] Line {number} of {filepath} is not what "
                f"{number}{':' + digest if digest else ''}"
                f"{'|' + text[:80] if text is not None else ''} says it "
                f"is. It now reads {number}:{_line_hash(line)}|{line[:120]}\n"
                f"The file has changed since you read it, or the anchor was mistyped. "
                f"read_file it again and use the anchors from the new listing.")

    return ""


# The same row `read_file` printed, handed back with different text after the
# `|`: `38:ff7|print()` means "line 38, which currently hashes to ff, becomes
# print()". One row says which line, proves it is the line that was read, and
# carries the replacement - so the shortest possible edit is one line long and
# the old text is never repeated anywhere.
#
# Leading whitespace is allowed and dropped. `read_file` never indents a row, so
# an indented one is a model that reformatted the block - and writing its literal
# text into the file, which is what would otherwise happen, is a silent wrong
# edit. A real source line that looks like `50:1fa|...` is imaginable; one that
# still looks like it after this much of a coincidence is not.
_ANCHORED_LINE = re.compile(r'^\s*(\d+)\s*:\s*([0-9a-fA-F]{2,3})\|(.*)$')


def _parse_patch(new_content: str):
    """`new_content` read as rows that each replace the line they name, or None.

    Every row has to be one, blank rows included - a `new_content` that is only
    partly anchored is ordinary replacement text and is left alone.
    """
    rows = (new_content or "").split("\n")
    if not any(row.strip() for row in rows):
        return None
    patch = []
    for row in rows:
        matched = _ANCHORED_LINE.match(row)
        if not matched:
            return None
        number, digest, text = matched.groups()
        patch.append((int(number), digest.lower(), text))
    return patch


def _patch_disagrees(old_content: str, patch: list) -> str:
    """Whether an `old_content` was given that says something else.

    A patch already names its lines, so `old_content` has nothing left to add:
    empty is the expected case and the same anchors are accepted. Anything else
    is two different instructions in one call, and picking one of them silently
    is how the wrong line gets edited.
    """
    if not (old_content or "").strip():
        return ""
    parsed = _parse_anchors(old_content)
    if parsed is not None:
        first, last, checks, spanned = parsed
        named = list(range(first, last + 1)) if spanned else [n for n, _, _ in checks]
        if sorted(named) == sorted(number for number, _, _ in patch):
            return ""
    lines = ", ".join(str(number) for number, _, _ in patch)
    return (f"[Error] old_content and new_content do not name the same lines. When "
            f"each new line carries its own anchor, new_content already says "
            f"everything - it is replacing line(s) {lines}. Send it with old_content "
            f"empty, or with exactly those anchors in it.")


def _edit_by_patch(filepath: str, file_content: str, patch: list,
                   old_content: str) -> str:
    """Apply `38:ff7|print()` rows: each replaces the one line it names."""
    problem = _patch_disagrees(old_content, patch)
    if problem:
        return problem

    lines = file_content.split("\n")
    seen = set()
    for number, digest, _ in patch:
        if number in seen:
            return (f"[Error] Line {number} is given twice in new_content. A line can "
                    f"only become one thing. To replace one line with several, put "
                    f"the anchor in old_content and the new lines, unanchored, in "
                    f"new_content.")
        seen.add(number)
        if number < 1 or number > len(lines):
            return (f"[Error] There is no line {number} in {filepath} - it has "
                    f"{len(lines)} lines. read_file it again and take the anchors "
                    f"from that listing.")
        current = lines[number - 1]
        if _line_hash(current) != digest:
            # There is no second piece of evidence here: the text beside the
            # anchor is what the line is to become, not what it is now. So the
            # hash is the whole check, and it is not waived.
            return (f"[Error] Line {number} of {filepath} is not what {number}:{digest} "
                    f"says it is. It now reads {number}:{_line_hash(current)}|"
                    f"{current[:120]}\nWriting a new line over it would overwrite "
                    f"something you have not read. Nothing was written - read_file "
                    f"again and take the anchors from the new listing.")

    details = [("path", filepath)]
    for number, _, text in patch[:8]:
        details.append((f"line {number}",
                        f"{_preview(lines[number - 1], 60)}  →  {_preview(text, 60)}"))
    if len(patch) > 8:
        details.append(("", f"...and {len(patch) - 8} more"))
    if not _approval_prompt("Edit File", details, rule=f"edit_file({filepath})"):
        return "[System] User denied file edit."

    for number, _, text in patch:
        lines[number - 1] = text
    problem = _write_file(filepath, "\n".join(lines), _existing_newline(filepath))
    if problem:
        return problem

    which = ", ".join(str(number) for number, _, _ in patch)
    return (f"[Success] File edited: {filepath} (line{'s' if len(patch) > 1 else ''} "
            f"{which} replaced). One line became one line, so nothing below moved "
            f"and the rest of your anchors are still good."
            + _echo_region("\n".join(lines), [n for n, _, _ in patch], moved=False))


def _preview(text: str, limit: int = 150) -> str:
    return text[:limit] + ("..." if len(text) > limit else "")


# Lines of context either side of a change, echoed back with the result.
EDIT_ECHO_LINES = 5
_EDIT_ECHO_MAX = 60          # ceiling, for an edit scattered across a big file


def _echo_region(file_content: str, changed: list, moved: bool) -> str:
    """The edited lines and their neighbours, as `read_file` would print them.

    Editing a line changes its hash, and changing the number of lines moves
    every anchor below it - so after an edit the model is holding anchors that
    are wrong, and its only recourse was to read the whole file again. That is
    a round trip and the whole file back into a context that is usually small,
    to recover a few lines it already knows.

    The lines it is working in are the ones it wants next, so they come back
    with the result: current, correct, and already in the format an edit is
    written in. Nothing is inferred here - this is the file as it now stands.
    """
    lines = file_content.split("\n")
    windows = []
    for number in sorted({n for n in changed if 1 <= n <= len(lines)}):
        low = max(1, number - EDIT_ECHO_LINES)
        high = min(len(lines), number + EDIT_ECHO_LINES)
        # Touching or overlapping windows become one, so a run of edits does
        # not print the same neighbourhood several times over.
        if windows and low <= windows[-1][1] + 1:
            windows[-1][1] = max(windows[-1][1], high)
        else:
            windows.append([low, high])
    if not windows:
        return ""

    shown, budget = [], _EDIT_ECHO_MAX
    for low, high in windows:
        if budget <= 0:
            shown.append("  …")
            break
        if shown:
            shown.append("  …")
        for number in range(low, min(high, low + budget - 1) + 1):
            shown.append(f"  {number}:{_line_hash(lines[number - 1])}|{lines[number - 1]}")
        budget -= high - low + 1

    return ("\nThe file now reads, around what you changed:\n" + "\n".join(shown)
            + ("\nAnchors outside this listing have moved with it; read_file for those."
               if moved else ""))


def _edit_by_anchor(filepath: str, file_content: str, start: int, end: int,
                    new_content: str) -> str:
    """Replace lines `start`..`end` outright. Every anchor has been verified."""
    lines = file_content.split("\n")
    replaced = "\n".join(lines[start - 1:end])
    count = end - start + 1
    where = f"line {start}" if count == 1 else f"lines {start}-{end} ({count} lines)"

    # An empty replacement removes the lines rather than leaving them blank,
    # which is the same thing `edit_file` has always done when asked to replace
    # a snippet with nothing.
    replacement = new_content.split("\n") if new_content else []

    details = [("path", filepath), ("replacing", where),
               ("from", _preview(replaced)),
               ("to", _preview(new_content) if replacement else "(the lines are removed)")]
    if not _approval_prompt("Edit File", details, rule=f"edit_file({filepath})"):
        return "[System] User denied file edit."

    lines[start - 1:end] = replacement
    problem = _write_file(filepath, "\n".join(lines), _existing_newline(filepath))
    if problem:
        return problem

    shifted = len(replacement) != count
    note = (" Every line below it has moved." if shifted else "")
    # The lines that are now where the edit landed, so the next edit in this
    # neighbourhood needs no read_file. A deletion leaves nothing of its own to
    # show, so the window is taken around where the lines used to start.
    touched = (list(range(start, start + len(replacement))) if replacement
               else [min(start, max(1, len(lines)))])
    return (f"[Success] File edited: {filepath} ({where} replaced).{note}"
            + _echo_region("\n".join(lines), touched, moved=shifted))


# A path and a body are the two arguments every file tool takes, and a small
# model sends the wrong *type* for them often enough to be worth naming: a path
# as `{"path": "x"}` because it read the schema as nesting, a body as a list of
# lines because it was thinking in lines. Both used to raise out of the handler
# on the first `.split` or `.strip`.
#
# A body is joined, because a list of lines is unambiguous and refusing it would
# throw away a write the model got right in every other respect. A path is not:
# there is no one thing `{"path": "x"}` can safely mean, so it is refused with
# the shape that was wanted.

def _as_path(value, name: str = "filepath") -> tuple:
    """(path, complaint). The path as a string, or why it cannot be one."""
    if isinstance(value, str):
        return value.strip(), ""
    if value is None or value == "" or value == {} or value == []:
        return "", (f"[Error] No '{name}' was given. Send it inside \"arguments\" "
                    "in the tool call JSON, as a plain string.")
    return "", (f"[Error] '{name}' must be a plain string path, not "
                f"{type(value).__name__}. You sent {json.dumps(value, ensure_ascii=False, default=str)[:120]}. "
                f'Send it as "{name}": "path/to/file".')


def _as_text(value) -> str:
    """A body as text, allowing for the model that sent it a line at a time."""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "\n".join(_as_text(item) for item in value)
    if value is None:
        return ""
    return str(value)


def handle_read_file(filepath: str) -> str:
    filepath, complaint = _as_path(filepath)
    if complaint:
        return complaint
    if images.is_image(filepath):
        # Read as text a PNG is a screenful of broken bytes that says nothing
        # and costs a thousand tokens to say it. A small model reaches for
        # `read_file` for everything, so it is sent to the right tool here
        # rather than left to work out what it is looking at.
        return (f"{config.TOOL_ERROR_PREFIX} {os.path.basename(filepath)} is an "
                f"image ({images.describe(filepath)}), not text. Call "
                "`view_image` with this path to look at it.")
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        if not config.RETURN_ALL_FILE_CONTENT and len(content) > config.FILE_MAX_DISPLAY_LENGTH:
            content = content[:config.FILE_MAX_DISPLAY_LENGTH] + "\n...[Too long]..."
        return _encode_hashlines(content)
    except Exception as e:
        return f"[Error] Cannot read file: {e}"

def handle_view_image(filepath: str) -> str:
    """Put an image in front of the model on its next request.

    A tool result is a string, so this cannot hand a picture back the way
    `read_file` hands back text. It checks the file and leaves the path with
    `images`, and `llm_client` hangs it on the tool-result message it is about
    to append - which is the message the model reads next.
    """
    filepath, complaint = _as_path(filepath)
    if complaint:
        return complaint
    if not os.path.exists(filepath):
        return f"{config.TOOL_ERROR_PREFIX} No such file: {filepath}"
    if not images.is_image(filepath):
        return (f"{config.TOOL_ERROR_PREFIX} {os.path.basename(filepath)} is not "
                f"an image this harness can send. It handles "
                f"{', '.join(sorted(set(images.MEDIA_TYPES.values())))}. "
                "Use `read_file` for text.")
    from aetheris import providers      # here, not at the top: see `_handlers`
    provider = providers.current()
    if not provider.sees_images():
        return (f"{config.TOOL_ERROR_PREFIX} {provider.model or provider.label} "
                "cannot see images, so there is no point sending one. Answer "
                "from what you can read, or ask the user to switch models.")
    data, _kind, why = images.encode(filepath)
    if not data:
        return f"{config.TOOL_ERROR_PREFIX} {why}"
    images.want(filepath)
    note = f" ({why})" if why else ""
    return (f"[Image attached: {filepath}]{note}\nIt is in front of you with "
            "this result - look at it and answer. Do not call view_image for "
            "the same file again.")


def handle_write_file(filepath: str, content: str) -> str:
    filepath, complaint = _as_path(filepath)
    if complaint:
        return complaint

    content = _strip_hashlines(_as_text(content))

    # A write with no body is a malformed call, not a request for an empty file -
    # the body went missing between the model and here. Saying so lets the model
    # resend it; writing it out would quietly destroy an existing file.
    if not content.strip():
        existing = os.path.isfile(filepath) and os.path.getsize(filepath) > 0
        return ("[Error] The call carried no content, so nothing was written"
                f"{' (the existing ' + filepath + ' was left untouched)' if existing else ''}. "
                "Send write_file again with the file body in a <content> block right "
                "after the JSON object.")

    preview = content if len(content) <= 200 else content[:200] + "...[truncated]"
    approved = _approval_prompt("Write File", [("path", filepath), ("preview", preview)],
                                rule=f"write_file({filepath})")
    if not approved: return "[System] User denied file write."
    problem = _write_file(filepath, content, _existing_newline(filepath))
    if problem:
        return problem
    return f"[Success] File written: {filepath}"

def handle_edit_file(filepath: str, old_content: str, new_content: str) -> str:
    """Replace part of a file. Three ways of saying which part, in this order.

    1. `new_content` rows that each carry their own anchor - `38:ff7|print()` -
       replace the line each one names, and `old_content` is not needed at all.
       The shortest form there is, and the one that never repeats the old text.
    2. `old_content` made of anchors - `50:1fa`, the whole row
       `50:1fa|print(answer)`, several rows, or a span `50:1fa-53:9c0` - names the
       lines, and `new_content` is what replaces them. This is the form to use
       when the number of lines changes, or when they are to be deleted.
    3. Anything else in `old_content` is matched as literal text, exactly as it
       always was.
    """
    filepath, complaint = _as_path(filepath)
    if complaint:
        return complaint
    old_content, new_content = _as_text(old_content), _as_text(new_content)

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            file_content = f.read()
    except Exception as e:
        return f"[Error] Cannot read file: {e}"

    # Before `_strip_hashlines`, which would eat the very anchors this reads.
    patch = _parse_patch(new_content)
    if patch is not None:
        return _edit_by_patch(filepath, file_content, patch, old_content)

    if not (old_content or "").strip():
        return _no_old_content(filepath, file_content, new_content)

    raw_old = old_content
    new_content = _strip_hashlines(new_content)

    start, end, problem = _anchor_target(filepath, file_content, old_content)
    if problem:
        return problem
    if start is not None:
        return _edit_by_anchor(filepath, file_content, start, end, new_content)

    old_content = _strip_hashlines(old_content)
    if old_content not in file_content:
        return ("[Error] The specified old_content was not found in the file. If the "
                "exact text is hard to reproduce, read_file the file and put the "
                "line's hashline anchor (e.g. 50:1fa) in old_content instead."
                + _quoted_wrong(file_content, raw_old))
    count = file_content.count(old_content)
    if count > 1:
        return (f"[Error] old_content found {count} times. Give a longer snippet, or "
                f"put the line's hashline anchor (e.g. 50:1fa, from read_file) in "
                f"old_content instead - a line number is never ambiguous.")

    old_preview = _preview(old_content)
    new_preview = _preview(new_content)
    approved = _approval_prompt("Edit File", [("path", filepath), ("from", old_preview), ("to", new_preview)],
                                rule=f"edit_file({filepath})")
    if not approved: return "[System] User denied file edit."

    new_file_content = file_content.replace(old_content, new_content, 1)
    problem = _write_file(filepath, new_file_content, _existing_newline(filepath))
    if problem:
        return problem

    # A text match names no lines, so where it landed is worked out from
    # how much of the file came before it.
    at = file_content.index(old_content)
    first = file_content.count("\n", 0, at) + 1
    touched = list(range(first, first + max(1, new_content.count("\n") + 1)))
    shifted = new_content.count("\n") != old_content.count("\n")
    return (f"[Success] File edited: {filepath}"
            + _echo_region(new_file_content, touched, moved=shifted))

def handle_delete_file(filepath: str) -> str:
    filepath, complaint = _as_path(filepath)
    if complaint:
        return complaint
    approved = _approval_prompt("Delete File", [("path", filepath)], rule=f"delete_file({filepath})")
    if not approved: return "[System] User denied file deletion."
    try:
        os.remove(filepath)
        return f"[Success] File deleted: {filepath}"
    except Exception as e:
        return f"[Error] Cannot delete file: {e}"

def handle_copy_file(src: str, dst: str) -> str:
    src, complaint = _as_path(src, "src")
    if complaint:
        return complaint
    dst, complaint = _as_path(dst, "dst")
    if complaint:
        return complaint
    approved = _approval_prompt("Copy File", [("from", src), ("to", dst)], rule=f"copy_file({src})")
    if not approved: return "[System] User denied file copy."
    try:
        shutil.copy2(src, dst)
        return f"[Success] File copied to: {dst}"
    except Exception as e:
        return f"[Error] Cannot copy file: {e}"

def handle_create_dir(dirpath: str) -> str:
    dirpath, complaint = _as_path(dirpath, "dirpath")
    if complaint:
        return complaint
    approved = _approval_prompt("Create Directory", [("path", dirpath)], rule=f"create_dir({dirpath})")
    if not approved: return "[System] User denied directory creation."
    try:
        os.makedirs(dirpath, exist_ok=True)
        return f"[Success] Directory created: {dirpath}"
    except Exception as e:
        return f"[Error] Cannot create directory: {e}"

# Ceiling on a network body read into memory. `response.text` reads whatever
# the server chose to send, and a model hands `get_url` whatever URL it found -
# so a link that turns out to point at a disk image was the harness being
# killed by the OOM reaper rather than an error anybody could read. Far above
# any page worth reading, and far below what hurts.
NETWORK_MAX_BYTES = 8 * 1024 * 1024


def _bounded_text(response) -> tuple:
    """A streamed response body as text, stopping at the ceiling. (text, cut)."""
    chunks, total = [], 0
    for chunk in response.iter_content(chunk_size=65536):
        if not chunk:
            continue
        chunks.append(chunk)
        total += len(chunk)
        if total >= NETWORK_MAX_BYTES:
            break
    raw = b"".join(chunks)[:NETWORK_MAX_BYTES]
    # `response.encoding`, never `apparent_encoding`: the latter reads
    # `response.content`, which on a streamed response means downloading the
    # whole thing again - the exact thing this exists to avoid.
    try:
        return raw.decode(response.encoding or "utf-8", "replace"), total >= NETWORK_MAX_BYTES
    except LookupError:
        return raw.decode("utf-8", "replace"), total >= NETWORK_MAX_BYTES


def handle_get_url(url: str) -> str:
    url, complaint = _as_path(url, "url")
    if complaint:
        return complaint
    try:
        response = requests.get(url, timeout=15, stream=True,
                                headers={"User-Agent": "Mozilla/5.0"})
        try:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "")
            body, cut = _bounded_text(response)
        finally:
            response.close()
        content = strip_html(body) if "text/html" in content_type else body
        if cut:
            content += (f"\n...[the page is larger than {NETWORK_MAX_BYTES // (1024 * 1024)}MB; "
                        "only the beginning was read]")
        if not config.RETURN_ALL_FILE_CONTENT and len(content) > config.FILE_MAX_DISPLAY_LENGTH:
            content = content[:config.FILE_MAX_DISPLAY_LENGTH] + "\n...[Too long]..."
        return content
    except Exception as e:
        return f"[Error] Cannot fetch URL: {e}"

def _as_option_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _normalise_questions(what_do, prompt, questions) -> list[dict]:
    """Accept every shape a model might send and return [{question, options}].

    The preferred shape is a list of questions, each with its own options. The
    old single-question form (`what_do` plus a flat `prompt` list of choices) is
    still understood, because that is what half the models will keep emitting.
    """
    items = []

    if isinstance(questions, dict):
        questions = [questions]
    if isinstance(questions, list):
        for entry in questions:
            if isinstance(entry, dict):
                text = str(entry.get("question") or entry.get("prompt")
                           or entry.get("what_do") or "").strip()
                options = _as_option_list(entry.get("options") or entry.get("choices")
                                          or entry.get("answers"))
            else:
                text, options = str(entry).strip(), []
            if text:
                items.append({"question": text, "options": options})
    elif isinstance(questions, str) and questions.strip():
        items.append({"question": questions.strip(), "options": []})

    if items:
        return items

    # The old form: one question in `what_do`, its choices in `prompt`.
    text = str(what_do or "").strip()
    options = _as_option_list(prompt)
    if not text and options:
        text, options = options[0], options[1:]
    if text:
        items.append({"question": text, "options": options})
    return items


def _ask_one(question: str, options: list[str], index: int, total: int) -> str | None:
    """Show one question with its own choices. None means the user gave up."""
    counter = f" {S.MUTED}({index}/{total}){S.R}" if total > 1 else ""
    print(f"\n  {S.INFO}?{S.R} {S.BOLD}Input Required{S.R}{counter}")
    print(f"  {S.MUTED}\u2502{S.R}  {question}\n  {S.MUTED}\u2502{S.R}")
    for i, option in enumerate(options, 1):
        print(f"  {S.MUTED}\u2502{S.R}  {S.ACCENT}{i}.{S.R} {option}")
    custom_idx = len(options) + 1
    print(f"  {S.MUTED}\u2502{S.R}  {S.GRAY}{custom_idx}.  Custom Input{S.R}\n  {S.MUTED}\u2502{S.R}")

    while True:
        try:
            if options:
                raw = input(f"  {S.MUTED}\u2570\u2500{S.R} {S.INFO}Chosen{S.R} "
                            f"{S.MUTED}(1~{custom_idx}){S.R} {S.INFO}\u203a{S.R} ").strip()
                if not raw:
                    continue
                choice = int(raw)
                if 1 <= choice <= len(options):
                    return options[choice - 1]
                if choice == custom_idx:
                    return config.safe_text(input(f"  {S.INFO}  \u203a{S.R} ").strip())
                print(f"  {S.ERR}    Input 1 to {custom_idx} number.{S.R}")
            else:
                answer = config.safe_text(input(f"  {S.MUTED}\u2570\u2500{S.R} {S.INFO}\u203a{S.R} ").strip())
                if answer:
                    return answer
        except ValueError:
            print(f"  {S.ERR}    Input correct number.{S.R}")
        except (EOFError, KeyboardInterrupt):
            print()
            return None


def handle_get_input(what_do="", prompt=None, questions=None) -> str:
    items = _normalise_questions(what_do, prompt, questions)
    if not items:
        return "[Error] No question was given. Pass 'questions': [{'question': ..., 'options': [...]}]."

    answers = []
    for i, item in enumerate(items, 1):
        answer = _ask_one(item["question"], item["options"], i, len(items))
        if answer is None:
            if not answers:
                return "[System] User cancelled the questions without answering."
            return ("[System] User stopped after answering the first "
                    f"{len(answers)} question(s):\n" + _format_answers(answers))
        answers.append((item["question"], answer))

    if len(answers) == 1:
        return answers[0][1]
    return _format_answers(answers)


def _format_answers(answers: list[tuple[str, str]]) -> str:
    lines = []
    for i, (question, answer) in enumerate(answers, 1):
        lines.append(f"{i}. {question}\n   -> {answer}")
    return "\n".join(lines)

def handle_list_dir(dirpath: str) -> str:
    dirpath, complaint = _as_path(dirpath, "dirpath")
    if complaint:
        return complaint
    if not os.path.exists(dirpath):
        return f"[Error] Directory not found: {dirpath}"
    try:
        items = os.listdir(dirpath)
        formatted_items = []
        for item in items:
            if os.path.isdir(os.path.join(dirpath, item)):
                formatted_items.append(f"{item}/ (Dir)")
            else:
                formatted_items.append(f"{item} (File)")
        return "\n".join(formatted_items) if formatted_items else "[Empty Directory]"
    except Exception as e:
        return f"[Error] list_dir failed: {e}"

def handle_git_status() -> str:
    try:
        res = subprocess.run(["git", "status"], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        return res.stdout or res.stderr
    except Exception as e:
        return f"[Error] git status failed: {e}"

def handle_git_diff() -> str:
    try:
        res = subprocess.run(["git", "diff"], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        return res.stdout or res.stderr
    except Exception as e:
        return f"[Error] git diff failed: {e}"

def handle_get_system_info() -> str:
    try:
        cpu_percent = psutil.cpu_percent(interval=0.5)
        cpu_count = psutil.cpu_count(logical=True)

        mem = psutil.virtual_memory()
        mem_total_gb = mem.total / (1024 ** 3)
        mem_used_gb = mem.used / (1024 ** 3)

        disk = psutil.disk_usage(os.getcwd())
        disk_total_gb = disk.total / (1024 ** 3)
        disk_used_gb = disk.used / (1024 ** 3)

        processes = []
        for p in psutil.process_iter(['pid', 'name', 'memory_percent', 'cpu_percent']):
            try:
                processes.append(p.info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        
        top_processes = sorted(processes, key=lambda x: x['memory_percent'] or 0, reverse=True)[:5]
        # `or 0`, the same way the sort key does it: psutil reports None for a
        # process whose memory it could not read, and formatting that as a
        # float turned the whole of get_system_info into one [Error] line.
        proc_lines = [
            f"  - PID {p['pid']}: {p['name']} (RAM: {p['memory_percent'] or 0:.1f}%)"
            for p in top_processes
        ]

        info = (
            f"[System Status]\n"
            f"\u2022 CPU Usage: {cpu_percent}% ({cpu_count} cores)\n"
            f"\u2022 RAM Usage: {mem.percent}% ({mem_used_gb:.1f}GB / {mem_total_gb:.1f}GB)\n"
            f"\u2022 Disk Usage: {disk.percent}% ({disk_used_gb:.1f}GB / {disk_total_gb:.1f}GB)\n\n"
            f"[Top Memory Processes]\n" + "\n".join(proc_lines)
        )
        return info

    except Exception as e:
        return f"[Error] Failed to fetch system info: {e}"


def handle_search_in_file(query: str, is_regex: bool = False) -> str:
    if not query:
        return "[Error] Search query is required."
    try:
        if is_regex:
            try:
                pattern = re.compile(query)
            except re.error as e:
                return f"[Error] Invalid regex pattern: {e}"
        else:
            pattern = re.compile(re.escape(query))

        matches = []
        max_results = 100
        search_root = os.getcwd()

        for root, dirs, files in os.walk(search_root):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('venv', '__pycache__', 'node_modules', '.git')]
            for filename in files:
                filepath = os.path.join(root, filename)
                rel_path = os.path.relpath(filepath, search_root)
                try:
                    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                        for line_num, line in enumerate(f, 1):
                            if pattern.search(line):
                                line_preview = line.rstrip()
                                if len(line_preview) > 200:
                                    line_preview = line_preview[:200] + "..."
                                matches.append(f"{rel_path}:{line_num}: {line_preview}")
                                if len(matches) >= max_results:
                                    break
                except (PermissionError, OSError):
                    continue
                if len(matches) >= max_results:
                    break
            if len(matches) >= max_results:
                break

        if not matches:
            return f"[Search] No matches found for: {query}"
        header = f"[Search] Found {len(matches)} match(es) for '{query}':\n"
        if len(matches) >= max_results:
            header = f"[Search] Showing first {max_results} matches for '{query}' (more may exist):\n"
        return header + "\n".join(matches)
    except Exception as e:
        return f"[Error] Search failed: {e}"


def handle_call_api(url: str, method: str, headers: str = "", payload: str = "") -> str:
    if not url:
        return "[Error] URL is required."
    if not method:
        return "[Error] HTTP method is required."

    method = method.upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        return f"[Error] Unsupported HTTP method: {method}"

    approved = _approval_prompt("API Call", [("URL", url), ("Method", method)], rule=f"call_api({url})")
    if not approved:
        return "[System] User denied API call."

    try:
        req_headers = {"User-Agent": "Aetheris/1.0"}
        if headers:
            if isinstance(headers, str):
                try:
                    parsed = json.loads(headers)
                    if isinstance(parsed, dict):
                        req_headers.update(parsed)
                except json.JSONDecodeError:
                    return "[Error] Invalid headers JSON format. Expected a JSON object like {\"Key\": \"Value\"}."
            elif isinstance(headers, dict):
                req_headers.update(headers)

        req_body = None
        if payload:
            if isinstance(payload, str):
                try:
                    req_body = json.loads(payload)
                except json.JSONDecodeError:
                    req_body = payload
            elif isinstance(payload, dict):
                req_body = payload

        # Streamed and capped for the same reason `get_url` is: an endpoint
        # that answers with a hundred megabytes should be an error the model
        # reads, not the harness going away.
        common = {"headers": req_headers, "timeout": 30, "stream": True}
        body_args = {"json": req_body if isinstance(req_body, dict) else None,
                     "data": req_body if isinstance(req_body, str) else None}
        if method == "GET":
            resp = requests.get(url, **common)
        elif method == "POST":
            resp = requests.post(url, **common, **body_args)
        elif method == "PUT":
            resp = requests.put(url, **common, **body_args)
        elif method == "PATCH":
            resp = requests.patch(url, **common, **body_args)
        else:                                   # DELETE - the list is checked above
            resp = requests.delete(url, **common)

        try:
            content, cut = _bounded_text(resp)
        finally:
            resp.close()
        if cut:
            content += (f"\n...[the response is larger than "
                        f"{NETWORK_MAX_BYTES // (1024 * 1024)}MB; only the beginning was read]")
        if not config.RETURN_ALL_FILE_CONTENT and len(content) > config.FILE_MAX_DISPLAY_LENGTH:
            content = content[:config.FILE_MAX_DISPLAY_LENGTH] + "\n...[Too long]..."

        result = (
            f"[API Response]\n"
            f"\u2022 Status: {resp.status_code} {resp.reason}\n"
            f"\u2022 Content-Type: {resp.headers.get('Content-Type', 'unknown')}\n\n"
            f"{content}"
        )
        return result
    except requests.exceptions.Timeout:
        return "[Error] API request timed out (30s)."
    except requests.exceptions.ConnectionError:
        return f"[Error] Could not connect to: {url}"
    except Exception as e:
        return f"[Error] API call failed: {e}"



def _get_ts_parser(lang_name: str):
    lang = _TS_LANGUAGES.get(lang_name)
    if lang is None:
        return None, None
    parser = Parser(lang)
    return parser, lang


def _detect_language(filepath: str) -> str | None:
    _, ext = os.path.splitext(filepath)
    return _EXT_TO_LANG.get(ext.lower())


def _extract_params_python(node, src: bytes) -> list[dict]:
    params = []
    param_node = None
    for child in node.children:
        if child.type == "parameters":
            param_node = child
            break
    if param_node is None:
        return params
    for child in param_node.children:
        if child.type in ("identifier",):
            params.append({"name": child.text.decode(), "type": None, "default": None})
        elif child.type == "typed_parameter":
            name = default = ptype = None
            for sub in child.children:
                if sub.type == "identifier" and name is None:
                    name = sub.text.decode()
                elif sub.type == "type":
                    ptype = sub.text.decode()
            params.append({"name": name, "type": ptype, "default": None})
        elif child.type == "default_parameter":
            name = default = None
            for sub in child.children:
                if sub.type == "identifier" and name is None:
                    name = sub.text.decode()
                elif sub.type not in ("=",):
                    default = sub.text.decode()
            params.append({"name": name, "type": None, "default": default})
        elif child.type == "typed_default_parameter":
            name = default = ptype = None
            for sub in child.children:
                if sub.type == "identifier" and name is None:
                    name = sub.text.decode()
                elif sub.type == "type":
                    ptype = sub.text.decode()
                elif sub.type not in ("=", ":"):
                    default = sub.text.decode()
            params.append({"name": name, "type": ptype, "default": default})
        elif child.type in ("list_splat_pattern", "dictionary_splat_pattern"):
            prefix = "*" if child.type == "list_splat_pattern" else "**"
            for sub in child.children:
                if sub.type == "identifier":
                    params.append({"name": prefix + sub.text.decode(), "type": None, "default": None})
    return params


def _extract_params_generic(node, src: bytes) -> list[dict]:
    params = []
    for child in node.children:
        if child.type in ("formal_parameters", "parameter_list", "parameters",
                          "formal_parameter_list"):
            for p in child.named_children:
                params.append({"name": p.text.decode(), "type": None, "default": None})
            break
    return params


def _extract_return_type(node, src: bytes) -> str | None:
    for child in node.children:
        if child.type == "type":
            return child.text.decode()
        if child.type == "return_type":
            return child.text.decode()
    return None


def _extract_decorators(node, src: bytes) -> list[str]:
    decorators = []
    sibling = node.prev_named_sibling
    while sibling and sibling.type == "decorator":
        decorators.insert(0, sibling.text.decode())
        sibling = sibling.prev_named_sibling
    for child in node.children:
        if child.type == "decorator":
            decorators.append(child.text.decode())
    return decorators


_FUNC_TYPES = {
    "function_definition", "function_declaration",
    "method_definition", "method_declaration",
    "arrow_function", "generator_function_declaration",
    "function_item",
}
_CLASS_TYPES = {
    "class_definition", "class_declaration",
    "struct_item", "enum_item", "impl_item",
    "interface_declaration", "enum_declaration",
    "type_declaration",
}


def _walk_skeleton(node, src: bytes, lang: str) -> list[dict]:
    results = []
    for child in node.children:
        entry = None
        if child.type in _FUNC_TYPES:
            name_node = child.child_by_field_name("name")
            name = name_node.text.decode() if name_node else "<anonymous>"
            if lang == "python":
                params = _extract_params_python(child, src)
            else:
                params = _extract_params_generic(child, src)
            ret = _extract_return_type(child, src)
            decorators = _extract_decorators(child, src) if lang == "python" else []
            entry = {
                "type": "function",
                "name": name,
                "line": child.start_point[0] + 1,
                "end_line": child.end_point[0] + 1,
                "parameters": params,
                "return_type": ret,
            }
            if decorators:
                entry["decorators"] = decorators
            body = child.child_by_field_name("body")
            if body:
                inner = _walk_skeleton(body, src, lang)
                if inner:
                    entry["children"] = inner

        elif child.type in _CLASS_TYPES:
            name_node = child.child_by_field_name("name")
            name = name_node.text.decode() if name_node else "<anonymous>"
            bases = []
            for sub in child.children:
                if sub.type in ("argument_list", "superclass", "type_list",
                                "class_heritage"):
                    bases.append(sub.text.decode())
            entry = {
                "type": "class",
                "name": name,
                "line": child.start_point[0] + 1,
                "end_line": child.end_point[0] + 1,
                "bases": bases,
            }
            body = child.child_by_field_name("body")
            if body:
                inner = _walk_skeleton(body, src, lang)
                if inner:
                    entry["methods"] = inner
            else:
                inner = _walk_skeleton(child, src, lang)
                if inner:
                    entry["methods"] = inner

        if entry:
            results.append(entry)
        else:
            deeper = _walk_skeleton(child, src, lang)
            results.extend(deeper)
    return results


def handle_get_code_skeleton(file_path: str) -> str:
    if not TREE_SITTER_AVAILABLE:
        return "[Error] tree-sitter is not installed. Run: pip install tree-sitter tree-sitter-python (and other language grammars)"

    if not file_path:
        return "[Error] file_path is required."

    if not os.path.isfile(file_path):
        return f"[Error] File not found: {file_path}"

    lang_name = _detect_language(file_path)
    if lang_name is None:
        return (f"[Error] Unsupported file extension. "
                f"Supported: {', '.join(sorted(set(_EXT_TO_LANG.values())))}")

    parser, lang = _get_ts_parser(lang_name)
    if parser is None:
        return f"[Error] Language grammar not available: {lang_name}"

    try:
        with open(file_path, "rb") as f:
            src = f.read()
    except Exception as e:
        return f"[Error] Failed to read file: {e}"

    tree = parser.parse(src)
    skeleton = _walk_skeleton(tree.root_node, src, lang_name)

    result = {
        "file": file_path,
        "language": lang_name,
        "skeleton": skeleton,
    }
    return json.dumps(result, indent=2, ensure_ascii=False)


def handle_query_ast_node(file_path: str, pattern: str, language: str = "") -> str:
    if not TREE_SITTER_AVAILABLE:
        return "[Error] tree-sitter is not installed."

    if not file_path:
        return "[Error] file_path is required."
    if not pattern:
        return "[Error] pattern is required."

    if not os.path.isfile(file_path):
        return f"[Error] File not found: {file_path}"

    lang_name = language.strip().lower() if language else _detect_language(file_path)
    if not lang_name or lang_name not in _TS_LANGUAGES:
        return (f"[Error] Could not determine language. "
                f"Supported: {', '.join(sorted(_TS_LANGUAGES.keys()))}")

    parser, lang = _get_ts_parser(lang_name)
    if parser is None:
        return f"[Error] Language grammar not available: {lang_name}"

    try:
        with open(file_path, "rb") as f:
            src = f.read()
    except Exception as e:
        return f"[Error] Failed to read file: {e}"

    tree = parser.parse(src)

    try:
        query = Query(lang, pattern)
    except Exception as e:
        return f"[Error] Invalid Tree-sitter query pattern: {e}"

    cursor = QueryCursor(query)
    matches_raw = list(cursor.matches(tree.root_node))

    if not matches_raw:
        return json.dumps({"file": file_path, "pattern": pattern, "matches": []},
                          indent=2, ensure_ascii=False)

    lines = src.split(b"\n")
    results = []
    max_results = 50

    for pattern_idx, captures in matches_raw:
        if len(results) >= max_results:
            break
        for cap_name, nodes in captures.items():
            for node in nodes:
                if len(results) >= max_results:
                    break
                start_line = node.start_point[0]
                end_line = node.end_point[0]
                ctx_start = max(0, start_line - 1)
                ctx_end = min(len(lines) - 1, end_line + 1)
                if ctx_end - ctx_start > 10:
                    ctx_end = ctx_start + 10
                snippet_lines = []
                for i in range(ctx_start, ctx_end + 1):
                    prefix = ">>>" if start_line <= i <= end_line else "   "
                    snippet_lines.append(f"{prefix} {i + 1}: {lines[i].decode(errors='replace')}")
                results.append({
                    "capture": cap_name,
                    "node_type": node.type,
                    "text": node.text.decode(errors="replace"),
                    "start_line": start_line + 1,
                    "end_line": end_line + 1,
                    "start_col": node.start_point[1],
                    "end_col": node.end_point[1],
                    "snippet": "\n".join(snippet_lines),
                })

    output = {
        "file": file_path,
        "language": lang_name,
        "pattern": pattern,
        "total_matches": len(results),
        "matches": results,
    }
    return json.dumps(output, indent=2, ensure_ascii=False)


def handle_submit_plan_for_approval(context_discovered: str, diff_blueprint: str, verification_steps: str) -> str:
    print(f"\n  {S.INFO}?{S.R} {S.BOLD}Plan Approval Required{S.R}")
    print(f"  {S.MUTED}│{S.R}  {S.ACCENT}[Context Discovered]{S.R}")
    for line in context_discovered.splitlines(): print(f"  {S.MUTED}│{S.R}    {line}")
    print(f"  {S.MUTED}│{S.R}  {S.ACCENT}[Diff Blueprint]{S.R}")
    for line in diff_blueprint.splitlines(): print(f"  {S.MUTED}│{S.R}    {line}")
    print(f"  {S.MUTED}│{S.R}  {S.ACCENT}[Verification Steps]{S.R}")
    for line in verification_steps.splitlines(): print(f"  {S.MUTED}│{S.R}    {line}")
    
    if config.AUTO_ALLOW:
        print(f"  {S.MUTED}│{S.R}  {S.OK}Auto-approved due to AUTOMODE.{S.R}")
        return "[System] AUTOMODE is ON. Plan automatically approved. Proceed strictly with execution and verification."

    print(f"  {S.MUTED}│{S.R}")
    print(f"  {S.MUTED}│{S.R}  {S.BOLD}1.{S.R} Approve (Proceed with execution)")
    print(f"  {S.MUTED}│{S.R}  {S.BOLD}2.{S.R} Reject (Abort task)")
    print(f"  {S.MUTED}│{S.R}  {S.BOLD}3.{S.R} Revise (Provide custom feedback)")
    print(f"  {S.MUTED}│{S.R}")
    
    while True:
        try:
            choice_str = input(f"  {S.MUTED}╰─{S.R} {S.INFO}Select{S.R} {S.MUTED}(1~3){S.R} {S.INFO}›{S.R} ").strip()
            if not choice_str: continue
            choice = int(choice_str)
            if choice == 1:
                return "[System] Plan Approved by User. You may now execute the plan strictly within the approved blueprint. Conclude by executing the verification steps."
            elif choice == 2:
                return "[System] Plan Rejected by User. Abort the task."
            elif choice == 3:
                feedback = input(f"  {S.INFO}  › Please enter feedback: {S.R}").strip()
                return f"[System] Plan Rejected with feedback: {feedback}\nPlease revise your plan and submit again."
            else:
                print(f"  {S.ERR}    Input 1 to 3.{S.R}")
        except ValueError:
            print(f"  {S.ERR}    Input correct number.{S.R}")


def handle_mcp_tool_call(function_name: str, arguments: dict) -> str:
    """Run a tool that lives on an attached MCP server."""
    resolved = mcp_client.resolve_tool(function_name)
    if resolved is None:
        available = ", ".join(mcp_client.available_tool_names()) or "(none)"
        return f"[Error] No MCP tool named '{function_name}'. Available MCP tools: {available}"

    server, tool = resolved
    tool_name = str(tool.get("name", ""))

    # Reaching a tool from a server whose schemas are not being sent means the
    # model got the name from somewhere and wants it. Hiding the parameters for
    # the *next* call after that would only have it guessing them.
    mcp_client.note_call(function_name)

    if not mcp_client.auto_approved(server, tool_name):
        details = [("server", server.name), ("tool", tool_name)]
        for key, value in arguments.items():
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            details.append((key, text))
        if not _approval_prompt("MCP Tool Call", details):
            return f"[System] User denied the MCP call '{tool_name}' on server '{server.name}'."

    return mcp_client.call_tool(server, tool_name, arguments)


def handle_list_mcp_resources(server_name: str = "") -> str:
    return mcp_client.list_resources_text(server_name)


def handle_read_mcp_resource(uri: str, server_name: str = "") -> str:
    return mcp_client.read_resource_text(uri, server_name)


def handle_spawn_agent(task: str, context: str = "", model: str = "") -> str:
    """Delegate a self-contained job to a fresh agent. See subagent.py.

    Approval is asked for here rather than left to the sub-agent's own tool
    calls. Those are checked too, but a sub-agent costs a stretch of time and,
    on a hosted model, real money before it reaches its first tool - so the
    decision to hire one is the user's, the same as running a command is.
    """
    from aetheris import subagent
    first_line = (task or "").strip().splitlines()[0] if (task or "").strip() else ""
    details = [("task", first_line), ("model", model or "same as this one")]
    if not _approval_prompt("Hire Sub-agent", details, rule="spawn_agent"):
        return ("[System] The user declined to start a sub-agent. Do this part of "
                "the work yourself.")
    return subagent.run(task, context, model)


# ---------------------------------------------------------------------------
# talking to the other agents working here - see channel.py
# ---------------------------------------------------------------------------

def _channel_off() -> str:
    return ("[System] The agent channel is off, so this session cannot see or "
            "reach other agents. Tell the user; /agents on turns it back on.")


def handle_list_agents() -> str:
    """Who else is working in this project, and on what."""
    if not channel.enabled():
        return _channel_off()
    here = channel.agents()
    mine = channel.me()
    if len(here) <= 1:
        return (f"[Agents] You ({mine or 'this session'}) are the only agent working "
                f"in {channel.workspace()} right now. Nothing to coordinate.")
    lines = []
    for record in here:
        holds = ", ".join(record.get("holds") or []) or "nothing"
        who = f"{record['id']} (you)" if record["id"] == mine else record["id"]
        lines.append(f"  {who} - {record.get('label') or 'unknown model'} - "
                     f"started {channel.ago(record.get('started'))} - "
                     f"holding: {holds}")
    return (f"[Agents] {len(here)} agents are working in {channel.workspace()}:\n"
            + "\n".join(lines)
            + "\nA file another agent is holding cannot be edited from here until "
              "they release it. Ask them with send_agent_message.")


# The parameter order is the toolspec's, not a preference: `_run_tool` binds
# positionally (`handler(*tool.bind(arguments))`), so these names must stay in
# the same order as `Tool.params` or every argument lands one place over.
def handle_send_agent_message(kind: str = "", message: str = "", to: str = "") -> str:
    """Say something to another agent. Nothing is queued for one that is not here.

    Refuses the politeness loop. Two 4B models given one job spent the entire
    run handing it back and forth - "could you test it?" / "yes, could you test
    it?" - neither ever running anything, both recording that the other was on
    it. Talking is the cheap move and doing the work is the expensive one, and a
    model told to answer its messages has been told to take the cheap one. So
    after `CHANNEL_MAX_IDLE_REPLIES` messages to the same peer with nothing done
    in between, the tool stops being available until something is done. Asking
    the model not to loop is not enough, for the same reason it is never enough
    anywhere else in this codebase.
    """
    if not channel.enabled():
        return _channel_off()

    # The kind is what stops this being a chat room. A model that has to
    # classify its own move has nowhere to put "let us rethink the parser":
    # there is no kind for it, because coordination is always about something
    # on disk. `[Error]`, not `[System]` - the call was malformed and making it
    # again correctly is exactly what should happen next.
    kind = str(kind or "").strip().lower()
    if kind not in channel.KINDS:
        legal = "; ".join(f"{name} = {what}" for name, what in channel.KINDS.items())
        return (f"[Error] '{kind or '(none)'}' is not a kind of message. Send it "
                f"again with kind set to one of: {legal}. If what you wanted to "
                f"say is none of these - debating an approach, agreeing a plan, "
                f"dividing up the work - it does not belong on this channel at "
                f"all. Do your own part and say what you did when it is done.")
    if kind == "question":
        # A question is legitimate about a file somebody else is *holding*, and
        # at no other time - see `channel.mentions_held_file`. Checked before
        # the plain path test below because it is the stronger form of it.
        named, claim = channel.mentions_held_file(message)
        if not claim:
            print(f"  {S.WARN}✗ nothing you named is held by anybody{S.R}"
                  f"  {S.GRAY}this reads as asking them to do the work{S.R}")
            free = (" Nothing you named is claimed by anyone, so there is "
                    "nothing to ask about: a free file needs no permission - "
                    "writing it takes the claim for you, and a file somebody "
                    "else holds refuses your write by name and tells you who to "
                    "ask. Go and do it." if named else "")
            return ("[System] That is not a question, it is asking another agent "
                    "to do your work - and there is no way to send that, because "
                    "there is no kind for it." + free + " A question here asks "
                    "about a file another agent is holding: \"are you finished "
                    "with server.js?\". If what you wanted was for somebody to "
                    "write, run or test something: do it yourself, now. Testing "
                    "above all - this harness runs the project's own check after "
                    "your edits by itself, so asking another agent to test your "
                    "work asks for something that already happens. And if you "
                    "only wanted to know who is here or who holds what, that is "
                    "list_agents, not a message.")
    if kind in channel.NEEDS_PATH and not re.search(r"[\w-]+[./][\w./-]+", message or ""):
        if kind == "question":
            # The delegation refusal, and the one that matters most. Two 4B
            # models put on "make a node server that returns the caller's IP"
            # spent the entire run on "could you test it?" - "yes, could you
            # test it?". A question about state can always name its file; a
            # request that somebody else do the work never does. `[System]`
            # rather than `[Error]`: the answer will not change on a retry, and
            # §4's counter should end a turn that keeps knocking.
            print(f"  {S.WARN}✗ a question has to be about a named file{S.R}"
                  f"  {S.GRAY}this reads as asking them to do the work{S.R}")
            return ("[System] That is not a question, it is asking another agent "
                    "to do your work - and there is no way to send that, because "
                    "there is no kind for it. A question here asks about the "
                    "state of a file you name: \"are you finished with "
                    "server.js?\". If what you wanted was for somebody to write, "
                    "run or test something: do it yourself, now. Testing above "
                    "all - this harness runs the project's own check after your "
                    "edits by itself, so asking another agent to test your work "
                    "asks for something that already happens. And if you only "
                    "wanted to know who is here or who holds what, that is "
                    "list_agents, not a message.")
        return (f"[Error] A '{kind}' has to name the file it is about. Send it "
                f"again with the path in the message, e.g. "
                f"'{'taking' if kind == 'claim' else 'releasing'} src/parser.py'.")

    limit = int(getattr(config, "CHANNEL_MAX_IDLE_REPLIES", 3) or 0)
    if limit > 0 and channel.talk_streak(to) >= limit:
        who = to or "everyone"
        print(f"  {S.WARN}✗ {limit} messages to {who} with nothing done between "
              f"them{S.R}  {S.GRAY}talking instead of working{S.R}")
        # `[System]`, so the refusal counter in `chat_turn` ends a turn that
        # keeps knocking (ARCHITECTURE 5.9) rather than spending the budget here.
        return (f"[System] That is {limit} messages to {who} in a row with "
                f"nothing done in between. This is the loop where two agents "
                f"ask each other to do the work and neither does it. Stop "
                f"messaging and do the job yourself now - read the file, make "
                f"the change, run the test. If you genuinely cannot, say so in "
                f"your answer to the user and stop; do not say it is done when "
                f"it is not. You can message {who} again once you have actually "
                f"done something.")

    ok, said = channel.send(message, to, kind)
    if not ok:
        # `[System]`, not `[Error]`: there is nobody to deliver to, so this is a
        # closed door rather than a call that went wrong, and the turn should end
        # if the model keeps knocking on it (ARCHITECTURE 5.9).
        return (f"[System] The message was not sent: {said}. Use list_agents to see "
                f"who is actually here, and do not retry the same message.")
    # Whoever was waiting on an answer has had one. Not only the agent named:
    # a model that replies to everybody has still replied.
    channel.clear_asked()
    channel.note_message(to, kind)
    print(f"  {S.MUTED}\u2192 {channel.me()} to {to or 'everyone'} [{kind}]: "
          f"{' '.join(str(message).split())[:70]}{S.R}")
    return (f"[Success] Message {said}. They will read it while they work - an "
            f"answer, if one comes, will reach you the same way. Now carry on "
            f"with the job yourself: this was a message, not a handover, and "
            f"nothing about it means somebody else is doing your part.")


def handle_claim_files(paths: str, reason: str = "") -> str:
    """Take the files, or report who already has them. Never takes some of them."""
    if not channel.enabled():
        return _channel_off()
    if not channel.me():
        return ("[System] This session is not on the agent board, so it cannot claim "
                "files. Nothing else is claiming them either - carry on.")
    wanted = channel.split_paths(paths)
    if not wanted:
        return "[Error] No file path was given to claim."
    taken, refused = channel.claim(paths, reason)
    if refused:
        held = "; ".join(f"{c['path']} is held by {c['agent']} ({c.get('reason') or 'no reason given'})"
                         for c in refused)
        return (f"[System] Nothing was claimed - {held}. Nothing is claimed at all "
                f"when any one file is taken, so you are not half-holding the rest. "
                f"Ask them with send_agent_message, or work on something else.")
    print(f"  {S.MUTED}\u25c6 claimed {', '.join(taken)}{S.R}")
    return (f"[Success] Claimed {', '.join(taken)}. No other agent can edit them "
            f"until you call release_files, so release them as soon as you are done.")


def handle_release_files(paths: str) -> str:
    """Give files back. Only this agent's own claims are dropped."""
    if not channel.enabled():
        return _channel_off()
    dropped = channel.release(paths)
    if not dropped:
        return ("[System] Nothing was released - none of those files are claimed by "
                "you. Use list_agents to see what you are holding.")
    return f"[Success] Released {', '.join(dropped)}."


# Which argument of which tool names a file that ends up changed. `create_dir`
# is absent on purpose: git does not track an empty directory, so there would
# be nothing to commit.
_WRITES_FILES = {
    "write_file": ("filepath",),
    "edit_file": ("filepath",),
    "delete_file": ("filepath",),
    "copy_file": ("dst",),
}


# Anything that changes the world rather than reporting on it. `run_cmd` and
# `send_input` are here because a command can write as easily as it can read,
# and a stage that is meant to be read-only cannot check which.
_CHANGES_THINGS = frozenset(_WRITES_FILES) | {
    "create_dir", "run_cmd", "send_input", "end_process", "run_python",
    "write_memory", "edit_memory", "delete_memory", "call_api",
    "write_note", "edit_note", "delete_note",
}


def _claimed_by_another(function_name: str, arguments: dict) -> tuple:
    """The other agent's claim standing in front of this call, or ("", {}).

    Read from `_WRITES_FILES`, so a tool that changes a file is covered here the
    moment it is added there - there is no second list of file tools to keep in
    step. `run_cmd` is not covered and cannot be: what a shell command touches
    is not knowable from the call.
    """
    for key in _WRITES_FILES.get(function_name, ()):
        path = arguments.get(key)
        if isinstance(path, str) and path:
            conflict = channel.holder(path)
            if conflict:
                return path, conflict
    return "", {}


def _paths_written(function_name: str, arguments: dict, result) -> list:
    """The files a call actually changed. Empty unless the tool reported success.

    A write the user declined at the approval prompt, or one that failed, has
    changed nothing - nothing to commit, and nothing to tell the other agents
    about. Both of the things that happen after a write read this, so they
    cannot disagree about what was written.
    """
    keys = _WRITES_FILES.get(function_name)
    if not keys or not isinstance(result, str) or not result.startswith("[Success"):
        return []
    return [arguments[key] for key in keys
            if isinstance(arguments.get(key), str) and arguments[key]]


def _commit_if_changed(function_name: str, arguments: dict, result) -> None:
    """Commit what a tool just wrote, so it can be taken back later."""
    if not git_ops.enabled():
        return
    paths = _paths_written(function_name, arguments, result)
    if not paths:
        return
    sha = git_ops.auto_commit(paths, function_name)
    if sha:
        print(f"  {S.MUTED}⎇ committed {sha}{S.R}  {S.GRAY}/undo to take it back{S.R}")


# An error used to arrive as the error and nothing else: "[Error] Command failed
# (exit code 1)." followed by a traceback. Which command? Which file? The model
# had to work that out from its own memory of what it asked for, and a small one
# often gets it wrong - it fixes the file it was thinking about rather than the
# one that broke. Naming the call costs one line and removes the guess.
_ERROR_PREFIX = config.TOOL_ERROR_PREFIX
_ARG_VALUE_CHARS = 60          # a file body must not push the error off the top
_ARG_LINE_CHARS = 200


def _describe_call(function_name: str, arguments: dict) -> str:
    """`run_cmd(command='python3 boom.py')` - the call, short enough to read."""
    parts = []
    for key, value in (arguments or {}).items():
        text = value if isinstance(value, str) else json.dumps(
            value, ensure_ascii=False, default=str)
        text = " ".join(str(text).split())       # a whole file on one line
        if len(text) > _ARG_VALUE_CHARS:
            text = text[:_ARG_VALUE_CHARS] + "..."
        parts.append(f"{key}={text!r}")
    joined = ", ".join(parts)
    if len(joined) > _ARG_LINE_CHARS:
        joined = joined[:_ARG_LINE_CHARS] + "..."
    return f"{function_name}({joined})"


def _name_the_failure(function_name: str, arguments: dict, result):
    """Put the call that failed in front of what it said about failing.

    Only `[Error]`. A `[System]` result is a refusal that already says which
    tool it refused, and `llm_client` counts those to stop a model knocking on
    a closed door - rewriting that prefix would break the counter.
    """
    if not isinstance(result, str) or not result.startswith(_ERROR_PREFIX):
        return result
    said = result[len(_ERROR_PREFIX):].lstrip()
    return f"{_ERROR_PREFIX} {_describe_call(function_name, arguments)}: {said}"


def dispatch_tool(function_name: str, arguments: dict) -> str | None:
    # Small models sometimes emit "arguments" as a JSON string rather than an
    # object. Normalise once here so no handler has to defend against it.
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}

    _fmt_tool_call(function_name, arguments)

    if config.DEEPTHINK_READONLY and function_name in _CHANGES_THINGS:
        # Telling a model not to edit yet is not enough - it edits anyway, and
        # then the stages that were meant to review a plan are reviewing work
        # nobody planned. So the planning stages are read-only for real.
        print(f"  {S.MUTED}✗ not yet: this stage is for working it out, "
              f"not changing it{S.R}")
        return (f"[System] '{function_name}' is not available during this stage. "
                "You are working out what to do, not doing it - say what you would "
                "change and why. You will be asked to make the change in a later "
                "stage, and everything you write now carries over to it.")

    blocked, conflict = _claimed_by_another(function_name, arguments)
    if conflict:
        # Another harness, in another terminal, is in the middle of this file.
        # Letting the write through would throw their work away silently, which
        # is the whole reason the board exists.
        print(f"  {S.WARN}\u2717 {conflict['agent']} is working on "
              f"{conflict['path']}{S.R}  {S.GRAY}/agents to see who{S.R}")
        return channel.refusal(function_name, blocked, conflict)

    verdict, rule = permissions.decide(function_name, arguments)
    if verdict == "deny":
        print(f"  {S.ERR}✗ Blocked by permission rule: {rule}{S.R}")
        return (f"[System] '{function_name}' is blocked by your permission rules "
                f"(rule: {rule}). Do not retry it; tell the user it is blocked.")

    config.POLICY_AUTO_ALLOW = verdict == "allow"
    # Counted here, past every refusal above, so that a tool which was blocked
    # does not read as work done. This is what tells the board the difference
    # between an agent getting on with the job and one talking about it - see
    # `channel.note_tool`.
    channel.note_tool(function_name)
    try:
        # The handler is the only thing that sees a real secret. `arguments` is
        # what was displayed, is what goes on being displayed, and is what the
        # conversation keeps - so the expansion lives in a copy and dies with
        # this call. See vault.py.
        result = _run_tool(function_name, vault.fill_in(function_name, arguments))
    except KeyboardInterrupt:
        # The person stopping a tool is not a tool that went wrong. It belongs
        # to the turn loop, which knows how to end.
        raise
    except Exception as error:
        # A handler that raises used to escape all the way to `app.cli`, where
        # the whole turn ended on one line of red with no tool result at all -
        # so the model was never told, and the conversation lost the thread of
        # what it had been doing. The same reasoning as the unknown-tool case
        # below: hand the model the failure and let it correct itself.
        #
        # A malformed argument is what actually reaches this. Small models send
        # `{"filepath": {"path": "x"}}` and `{"content": ["a", "b"]}` often
        # enough that the handlers coerce what they can, and this is the floor
        # under everything they cannot.
        result = (f"[Error] '{function_name}' raised {type(error).__name__}: {error}. "
                  "Nothing was completed. Check the arguments against the tool's "
                  "parameters and call it again.")
    finally:
        config.POLICY_AUTO_ALLOW = False
    written = _paths_written(function_name, arguments, result)
    _commit_if_changed(function_name, arguments, result)
    channel.note_write(written)
    # Noted, not run: the check belongs after the whole turn's tool calls, not
    # between two of them. A model that writes three files in one reply should
    # pay for one run of the suite against the finished state, not three
    # against states it was halfway through. `llm_client` runs what is noted.
    verify.note_written(written)
    # Last, so nothing downstream of here can carry a value back: whatever the
    # tool read, ran or printed, what reaches the model - and through it the
    # provider, and the session file on disk - names the secret instead of
    # spelling it.
    return vault.redact(_name_the_failure(function_name, arguments, result))


def _handlers() -> dict:
    """Tool name to the function that runs it.

    `session` and `notes` are imported here rather than at the top only because
    it keeps the memory and note handlers next to the rest of the table; there
    is no cycle either way.
    """
    global _HANDLERS
    if _HANDLERS is None:
        from aetheris.session import (handle_write_memory, handle_get_memory_list,
                             handle_read_memory, handle_delete_memory,
                             handle_edit_memory)
        from aetheris.notes import (handle_write_note, handle_read_note,
                                          handle_list_notes, handle_edit_note,
                                          handle_delete_note)
        _HANDLERS = {
            "search_web": handle_search_web,
            "get_url": handle_get_url,
            "run_cmd": safe_run_cmd,
            "send_input": handle_send_input,
            "end_process": handle_end_process,
            "run_python": handle_run_python,
            "list_dir": handle_list_dir,
            "read_file": handle_read_file,
            "write_file": handle_write_file,
            "edit_file": handle_edit_file,
            "delete_file": handle_delete_file,
            "copy_file": handle_copy_file,
            "create_dir": handle_create_dir,
            "view_image": handle_view_image,
            "git_status": handle_git_status,
            "git_diff": handle_git_diff,
            "write_memory": handle_write_memory,
            "get_memory_list": handle_get_memory_list,
            "read_memory": handle_read_memory,
            "delete_memory": handle_delete_memory,
            "edit_memory": handle_edit_memory,
            "write_note": handle_write_note,
            "read_note": handle_read_note,
            "list_notes": handle_list_notes,
            "edit_note": handle_edit_note,
            "delete_note": handle_delete_note,
            "get_user_input": handle_get_input,
            "get_system_info": handle_get_system_info,
            "search_in_file": handle_search_in_file,
            "call_api": handle_call_api,
            "get_code_skeleton": handle_get_code_skeleton,
            "query_ast_node": handle_query_ast_node,
            "submit_plan_for_approval": handle_submit_plan_for_approval,
            "use_skill": handle_use_skill,
            "use_mcp_server": mcp_client.use_server,
            "spawn_agent": handle_spawn_agent,
            "list_agents": handle_list_agents,
            "send_agent_message": handle_send_agent_message,
            "claim_files": handle_claim_files,
            "release_files": handle_release_files,
        }
        _check_registry(_HANDLERS)
    return _HANDLERS


_HANDLERS = None


def _check_registry(handlers: dict) -> None:
    """Refuse to run with a table and a prompt that disagree.

    This is the whole point of the registry: a tool the model is told about but
    that nothing can run, or a handler the model is never told exists, is a
    silent failure at the worst possible moment. Here it is a startup error.
    """
    described = set(toolspec.names())
    implemented = set(handlers)
    unrunnable = described - implemented
    unadvertised = implemented - described
    problems = []
    if unrunnable:
        problems.append(f"described to the model but not implemented: "
                        f"{', '.join(sorted(unrunnable))}")
    if unadvertised:
        problems.append(f"implemented but not described to the model: "
                        f"{', '.join(sorted(unadvertised))}")
    for tool in toolspec.TOOLS:
        handler = handlers.get(tool.name)
        if handler is None:
            continue
        try:
            wanted = len(inspect.signature(handler).parameters)
        except (TypeError, ValueError):
            continue
        if wanted != len(tool.params):
            problems.append(f"{tool.name}: the spec lists {len(tool.params)} "
                            f"parameter(s), {handler.__name__} takes {wanted}")
    if problems:
        raise RuntimeError("tool registry is inconsistent - "
                           + "; ".join(problems))


def _run_tool(function_name: str, arguments: dict) -> str | None:
    tool = toolspec.get(function_name)
    if tool is not None:
        handler = _handlers().get(tool.name)
        if handler is not None:
            return handler(*tool.bind(arguments))

    if mcp_client.is_mcp_tool(function_name):
        return handle_mcp_tool_call(function_name, arguments)
    if function_name == "list_mcp_resources":
        return handle_list_mcp_resources(arguments.get("server", "")
                                         or arguments.get("server_name", ""))
    if function_name == "read_mcp_resource":
        return handle_read_mcp_resource(arguments.get("uri", ""),
                                        arguments.get("server", "")
                                        or arguments.get("server_name", ""))

    print(f"  {S.WARN}\u26a0 Unknown tool: {function_name}{S.R}")
    return None
