"""The conversation loop: stream a reply, read the tool calls out of it, run them.

Nothing here knows which provider is answering - `providers.py` hands over the
same events whether they came from a local Ollama model or a hosted one.
"""

import sys
import re
import json
import asyncio
import itertools
import time
import random
from aetheris import config
from aetheris.config import S
from aetheris.tui import _fmt_tool_call, _fmt_tool_result, _fmt_tokens
from aetheris.renderer import _render_line, _format_table, _render_full
from aetheris.tools import dispatch_tool
from aetheris import channel
from aetheris import images
from aetheris import context
from aetheris import mcp_client
from aetheris import providers
from aetheris import toolspec
from aetheris import verify


TOOL_CALL_TAG = "<tool_call>"
THINK_OPEN = ("<think>", "<thinking>")
THINK_CLOSE = ("</think>", "</thinking>")

_THINK_BLOCK = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)
_THINK_START = re.compile(r"<think(?:ing)?>", re.IGNORECASE)


def _holdback(buffer: str, markers: tuple[str, ...]) -> int:
    """Length of the tail that could still grow into one of `markers`."""
    longest = max(len(m) for m in markers) - 1
    for size in range(min(len(buffer), longest), 0, -1):
        tail = buffer[-size:]
        if any(m.startswith(tail) for m in markers):
            return size
    return 0


class StreamFilter:
    """Splits a model's raw stream into prose, reasoning, and the tool call.

    Reasoning models (qwen3, deepseek-r1, gpt-oss) wrap their scratch work in
    `<think>` tags, and every model here announces a tool call with
    `<tool_call>`. Neither is prose and neither belongs on screen as prose. The
    tags arrive a few characters at a time, so a tail that could still grow into
    one is held back rather than printed and regretted.
    """

    def __init__(self):
        self._buffer = ""
        self._state = "prose"        # prose | think | tool
        self.saw_tool_call = False

    @property
    def thinking(self) -> bool:
        return self._state == "think"

    def _markers(self) -> tuple[str, ...]:
        if self._state == "think":
            # A tool call also closes an unterminated think block, so a model
            # that forgets </think> does not swallow its own tool call.
            return THINK_CLOSE + (TOOL_CALL_TAG,)
        return (TOOL_CALL_TAG,) + THINK_OPEN

    def feed(self, text: str) -> list[tuple[str, str]]:
        self._buffer += text
        return self._drain(final=False)

    def close(self) -> list[tuple[str, str]]:
        return self._drain(final=True)

    def _drain(self, final: bool) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        while self._buffer and self._state != "tool":
            markers = self._markers()
            hits = [(self._buffer.find(m), m) for m in markers if m in self._buffer]
            if hits:
                index, marker = min(hits, key=lambda hit: hit[0])
                if index:
                    out.append((self._state, self._buffer[:index]))
                self._buffer = self._buffer[index + len(marker):]
                if marker == TOOL_CALL_TAG:
                    self._state = "tool"
                    self.saw_tool_call = True
                    self._buffer = ""
                else:
                    self._state = "think" if marker in THINK_OPEN else "prose"
                continue

            hold = 0 if final else _holdback(self._buffer, markers)
            keep = len(self._buffer) - hold
            if keep > 0:
                out.append((self._state, self._buffer[:keep]))
                self._buffer = self._buffer[keep:]
            break
        return [(kind, text) for kind, text in out if text]


def strip_thinking(text: str) -> str:
    """Remove reasoning from a response before it is stored or exported."""
    cleaned = _THINK_BLOCK.sub("", text)
    start = _THINK_START.search(cleaned)
    if start:
        # An unterminated block runs to the end - except for a tool call, which
        # still has to survive.
        tail = cleaned[start.end():]
        tool = tail.find(TOOL_CALL_TAG)
        cleaned = cleaned[:start.start()] + (tail[tool:] if tool != -1 else "")
    return cleaned.strip()


def native_tools(exclude: tuple = ()) -> list:
    """Every tool the model may call, in the vendor-neutral native shape.

    The built-ins and the MCP tools have to travel together: with native tool
    calling the prompt no longer lists either, so a tool missing from here is a
    tool the model cannot see at all.
    """
    return toolspec.native_schema(exclude=exclude) + mcp_client.native_tool_schemas()


def native_enabled() -> bool:
    if not getattr(config, "NATIVE_TOOLS", True):
        return False
    return bool(providers.current().supports_native_tools)


async def stream_reply(messages: list[dict], tools: list | None = None,
                       calls_out: list | None = None) -> str:
    """Stream one reply. Returns the text; native tool calls land in `calls_out`.

    The two protocols meet here. A provider that speaks text puts its calls in
    the reply, where `parse_tool_calls` finds them; a provider with a real tool
    interface reports them as events instead, and they are appended to
    `calls_out` as the same `(name, arguments)` pairs. Everything downstream -
    dispatch, display, history, session files - sees no difference.
    """

    async def spinner():
        """· ✢ ✳ ✶ ✻ ✽"""
        frames = [
            f"{S.ACCENT}·{S.R}",
            f"{S.ACCENT}✢\uFE0E{S.R}",
            f"{S.ACCENT}*\uFE0E{S.R}",
            f"{S.ACCENT}✶\uFE0E{S.R}",
            f"{S.ACCENT}✻\uFE0E{S.R}",
            f"{S.ACCENT}✽\uFE0E{S.R}",
        ]
        cycle = itertools.cycle(frames)
        try:
            while True:
                frame = next(cycle)
                sys.stdout.write(f'\r  {frame} {S.GRAY}thinking…{S.R}  ')
                sys.stdout.flush()
                await asyncio.sleep(random.randint(2, 4) * 0.1)
        except asyncio.CancelledError:
            sys.stdout.write('\r\033[K')
            sys.stdout.flush()

    spin_task = asyncio.create_task(spinner())
    
    full_text = ""
    thinking_text = ""
    stream = StreamFilter()
    printing = False        # prose has started, so the spinner is gone
    thought_open = False    # a reasoning block is being shown
    think_buffer = ""
    
    line_buffer = ""
    in_code = False
    
    table_buffer = []
    in_latex_block = False

    def flush_table():
        if table_buffer:
            for t_line in _format_table(table_buffer): print(t_line)
            table_buffer.clear()

    def process_line(line: str, in_c: bool) -> bool:
        nonlocal in_latex_block
        stripped = line.strip()
        if not in_c and not in_latex_block and stripped.startswith('|') and stripped.endswith('|'):
            table_buffer.append(line)
            return in_c
        else:
            flush_table()
            rendered, out_c, in_latex_block = _render_line(line, in_c, in_latex_block)
            for r_line in rendered.split('\n'):
                print(r_line)
            return out_c

    def emit(text: str, in_c: bool) -> bool:
        """Buffer streamed prose and render it a completed line at a time."""
        nonlocal line_buffer
        for char in text:
            if char == '\n':
                sys.stdout.write('\r\033[K')
                sys.stdout.flush()
                in_c = process_line(line_buffer, in_c)
                line_buffer = ""
            else:
                line_buffer += char
        return in_c

    def emit_thought(text: str) -> None:
        """Reasoning, shown dimmed and unrendered - it is not the answer."""
        nonlocal think_buffer
        for char in text:
            if char == '\n':
                sys.stdout.write('\r\033[K')
                sys.stdout.flush()
                print(f"  {S.MUTED}│ {think_buffer}{S.R}")
                think_buffer = ""
            else:
                think_buffer += char

    def close_thought() -> None:
        nonlocal think_buffer, thought_open
        if not thought_open:
            return
        if think_buffer.strip():
            sys.stdout.write('\r\033[K')
            sys.stdout.flush()
            print(f"  {S.MUTED}│ {think_buffer}{S.R}")
        think_buffer = ""
        print(f"  {S.MUTED}╰─{S.R}")
        thought_open = False

    prompt_tokens = 0
    completion_tokens = 0
    cached_tokens = 0
    total_duration = 0.0
    eval_duration = 0.0

    start_time = None
    chunk_count = 0

    try:
        # Only mentioned when there is something to send: `tools=None` tells a
        # provider nothing, and not every provider has to know the word.
        reply = (providers.current().stream(messages, tools=tools) if tools
                 else providers.current().stream(messages))

        async for chunk in reply:
            if start_time is None:
                start_time = time.time()

            chunk_count += 1

            content = chunk.get('text', '') or ''
            full_text += content

            native_call = chunk.get('tool_call')
            if native_call is not None and calls_out is not None:
                calls_out.append(native_call)

            if chunk.get('done'):
                prompt_tokens = chunk.get("prompt_tokens", 0)
                completion_tokens = chunk.get("completion_tokens", 0)
                # How much of the prompt was served from the provider's cache.
                # Ollama reports nothing here and does not need to: its prefix
                # is reused locally, at no charge and no report.
                cached_tokens = chunk.get("cached_tokens", 0) or 0
                total_duration = chunk.get("total_seconds", 0.0)
                eval_duration = chunk.get("eval_seconds", 0.0)

            # Providers that report reasoning separately hand it over in its own
            # field. It never enters `full_text`, so it stays out of the
            # conversation history for free.
            events = []
            native_thinking = chunk.get('thinking', '') or ''
            if native_thinking:
                events.append(("think", native_thinking))
            events.extend(stream.feed(content))

            for kind, text in events:
                if kind == "think":
                    thinking_text += text
                    if not config.SHOW_THINKING:
                        continue
                    if not printing:
                        printing = True
                        if not spin_task.done():
                            spin_task.cancel()
                            sys.stdout.write('\r\033[K')
                            sys.stdout.flush()
                    if not thought_open:
                        thought_open = True
                        print(f"  {S.MUTED}╭─ thinking{S.R}")
                    emit_thought(text)
                    continue

                close_thought()
                if not printing:
                    printing = True
                    if not spin_task.done():
                        spin_task.cancel()
                        sys.stdout.write('\r\033[K')
                        sys.stdout.flush()
                in_code = emit(text, in_code)

            if stream.saw_tool_call and not line_buffer.strip():
                line_buffer = ""     # drop the blank line the model left before the tag

            if printing and not stream.saw_tool_call:
                elapsed = time.time() - start_time
                if elapsed > 0.1:
                    tps = chunk_count / elapsed
                    sys.stdout.write(f"\r\033[K  {S.MUTED}TPS: {tps:.1f}{S.R}")
                    sys.stdout.flush()

        # A tail that never grew into a tag is just text after all.
        for kind, text in stream.close():
            if kind == "think":
                thinking_text += text
                if config.SHOW_THINKING and thought_open:
                    emit_thought(text)
            else:
                close_thought()
                in_code = emit(text, in_code)
        close_thought()

        if printing:
            sys.stdout.write('\r\033[K')
            sys.stdout.flush()
        if line_buffer:
            in_code = process_line(line_buffer, in_code)
        flush_table()

    finally:
        if not spin_task.done():
            spin_task.cancel()
            try: await spin_task
            except asyncio.CancelledError: pass
            
    config.token_history.append({"prompt": prompt_tokens,
                                 "completion": completion_tokens,
                                 "cached": cached_tokens,
                                 "turn": config.turn_index})
    # `messages` is still exactly what was sent - the reply is appended by the
    # caller - so this is a clean sample to calibrate the estimator against.
    context.observe_usage(messages, prompt_tokens)
    
    if not stream.saw_tool_call:
        # The turn this request ends, so what is reported is what the question
        # cost rather than what its last request cost. `/usage` groups the same
        # way; the two would otherwise disagree about the same conversation.
        turns = context.token_turns()
        _fmt_tokens(prompt_tokens, completion_tokens, total_duration, eval_duration,
                    turns[-1] if turns else None)
        
    return full_text


_CLOSERS = {"{": "}", "[": "]"}


def _json_tail_state(blob: str) -> tuple[list[str], bool]:
    """Walk a JSON fragment and report (unclosed brackets, still inside a string)."""
    depth: list[str] = []
    in_string = False
    escaped = False
    for char in blob:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in _CLOSERS:
            depth.append(char)
        elif char in ("}", "]"):
            if depth:
                depth.pop()
    return depth, in_string


def _close_brackets(blob: str) -> dict | None:
    """Recover a tool call whose brackets the model failed to close.

    Small models are good at escaping a long payload and then miscounting the
    braces around it - one missing `}` at the very end throws away a whole
    generation.
    """
    depth, _ = _json_tail_state(blob)
    if not depth:
        return None
    closers = "".join(_CLOSERS[bracket] for bracket in reversed(depth))
    try:
        repaired, _ = json.JSONDecoder().raw_decode(blob + closers)
    except json.JSONDecodeError:
        return None
    return repaired if isinstance(repaired, dict) else None


# Parameters that carry a whole file or document, where the model has to escape
# hundreds of quotes and backslashes correctly - and often does not.
_LONG_TEXT_KEYS = ("content", "new_content", "old_content", "text", "body",
                   "payload", "stdin")

_JSON_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b",
                 "f": "\f", "n": "\n", "r": "\r", "t": "\t"}

_FIELD_PATTERN = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*"')
_SCALAR_PATTERN = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _lenient_unescape(raw: str) -> str:
    """Undo JSON escaping, keeping anything that is not a JSON escape verbatim."""
    out = []
    i = 0
    while i < len(raw):
        char = raw[i]
        if char != "\\" or i + 1 >= len(raw):
            out.append(char)
            i += 1
            continue
        nxt = raw[i + 1]
        if nxt in _JSON_ESCAPES:
            out.append(_JSON_ESCAPES[nxt])
            i += 2
        elif nxt == "u" and i + 6 <= len(raw):
            try:
                out.append(chr(int(raw[i + 2:i + 6], 16)))
                i += 6
            except ValueError:
                out.append(char)
                i += 1
        else:
            # `\}` and friends are not JSON escapes; the model meant both
            # characters literally, so keep them.
            out.append(char)
            out.append(nxt)
            i += 2
    return "".join(out)


def _lenient_tool_call(blob: str) -> dict | None:
    """Rebuild a call whose long text payload broke the JSON around it.

    Models leave a bare `"` inside the file they are writing, or write an
    invalid escape, and the value ends early. The structure is still readable:
    the short parameters sit before the payload, and the payload runs to the
    last quote.

    Only attempted when exactly one long-text parameter is present and it is the
    final field - with two of them (`edit_file`) there is no way to tell where
    the first was meant to end, and guessing could corrupt a file.
    """
    name_match = re.search(r'"name"\s*:\s*"([A-Za-z0-9_]+)"', blob)
    if not name_match:
        return None

    long_fields = [(m.group(1), m.end()) for m in _FIELD_PATTERN.finditer(blob)
                   if m.group(1) in _LONG_TEXT_KEYS]
    if len(long_fields) != 1:
        return None
    key, value_start = long_fields[0]

    close_quote = blob.rfind('"')
    if close_quote <= value_start:
        return None
    if blob[close_quote + 1:].strip(" \t\r\n}]),"):
        return None          # something other than brackets follows: not the last field

    arguments = {}
    for match in _SCALAR_PATTERN.finditer(blob[:value_start]):
        if match.group(1) != "name":
            arguments[match.group(1)] = _lenient_unescape(match.group(2))
    arguments[key] = _lenient_unescape(blob[value_start:close_quote])

    return {"name": name_match.group(1), "arguments": arguments}


def _normalise_arguments(tool_data: dict) -> dict:
    """Take the parameters from wherever the model actually put them.

    The prompt asks for them nested under `arguments`, but models routinely
    flatten them to the top level next to `name`. Reading only `arguments` then
    hands the tool an empty dict - which is why `write_file` was failing with
    "No such file or directory: ''".
    """
    arguments = tool_data.get("arguments")
    if isinstance(arguments, dict) and arguments:
        return arguments
    flattened = {key: value for key, value in tool_data.items()
                 if key not in _NAME_KEYS and key != "arguments"}
    if flattened:
        return flattened
    return arguments if isinstance(arguments, dict) else {}


# A long text parameter may be sent as a raw block after the JSON instead of
# inside it. Escaping a whole source file into a JSON string is the single thing
# small models get wrong most often - a bare quote, a lost backslash before a
# line continuation, one uncounted brace - and this removes the need entirely.
_RAW_BLOCK_PATTERN = re.compile(
    r"<(content|new_content|old_content|text|body|stdin)>\r?\n?(.*?)\r?\n?</\1>",
    re.DOTALL)


_RAW_OPEN_PATTERN = re.compile(r"<(content|new_content|old_content|text|body|stdin)>\r?\n?")


# A model whose template has no tool-call format of its own reaches for the
# nearest thing it knows, and that is a markdown fence. Gemma writes
#
#     ```tool_call
#     {"name": "write_file", "arguments": {"filepath": "hello.py"}}
#     ```
#     <content>
#     print('hi')
#     </content>
#
# where the JSON is byte-perfect and the raw block is byte-perfect - only the
# envelope is wrong. Reading `<tool_call>` alone found nothing there, so the
# turn ended with no tool run, no error, and nothing said. That silence is the
# one failure the text protocol exists to prevent, and gemma3 is the whole of
# its audience: those are the models Ollama reports as having no tool support.
#
# The raw block sits *outside* the fence, so the text after it comes along too.
_FENCED_CALL = re.compile(
    r"```[ \t]*(?P<lang>[A-Za-z_][A-Za-z0-9_]*)?[ \t]*\r?\n(?P<body>.*?)\r?\n?```",
    re.DOTALL)

# Info strings that say "this is a call" outright. Anything else - a bare fence,
# ```json, ```python - is only read as one if what it decodes to actually names
# a tool that exists, so an ordinary code block in an answer stays an answer.
_CALL_FENCE_LANGS = frozenset(
    ("tool_call", "tool_code", "tool_use", "toolcall", "function_call", "functioncall"))


def _take_raw_blocks(blob: str, complete: bool) -> tuple[str, dict, bool]:
    """Pull `<content>…</content>` style blocks out of a tool call.

    Returns the remaining JSON, the blocks, and whether a block was left open by
    a reply that stopped early - in which case the call must be thrown away. The
    JSON around it parses perfectly well on its own, and running it would write
    a file with no content at all.
    """
    blocks = {}

    def _collect(match):
        # An empty block is nothing to go on; keep whatever the JSON had.
        if match.group(2).strip():
            blocks[match.group(1)] = match.group(2)
        return ""

    stripped = _RAW_BLOCK_PATTERN.sub(_collect, blob)

    opened = _RAW_OPEN_PATTERN.search(stripped)
    if opened:
        if not complete:
            return stripped, blocks, True
        # The model closed `</tool_call>` but forgot `</content>`: the text is
        # all there, so take it to the end.
        blocks[opened.group(1)] = stripped[opened.end():].rstrip()
        stripped = stripped[:opened.start()]

    return stripped, blocks, False


# A model that has been shown `<tool_call>{...}</tool_call>` sometimes keeps the
# tag as a *key* and nests the real call under it:
#
#     {"tool_call": {"name": "write_file", "arguments": {...}}}
#
# There is exactly one thing that can mean, so unwrapping it invents nothing.
# The test is deliberately narrow - a lone key whose only value is a dict that
# carries a name - because a call with real arguments beside it is not an
# envelope and must not be flattened.
_ENVELOPE_KEYS = frozenset(
    ("tool_call", "tool_use", "toolcall", "function_call", "functioncall",
     "function", "tool", "call", "action"))


# The same models also rename the key that holds the tool's name. There is no
# ambiguity in any of these - a call has exactly one name - so the value is
# read from whichever one the model used.
_NAME_KEYS = ("name", "tool_name", "toolName", "function_name", "functionName",
              "tool", "function", "recipient_name")


def _unwrap_envelope(data: dict) -> dict:
    """A call the model nested one level too deep, brought back up."""
    if data.get("name") or len(data) != 1:
        return data
    key, inner = next(iter(data.items()))
    if key.lower() not in _ENVELOPE_KEYS or not isinstance(inner, dict):
        return data
    return inner if inner.get("name") else data


def _rename_to_name(data: dict) -> dict:
    """The tool's name, taken from whichever key the model put it under."""
    if data.get("name"):
        return data
    for key in _NAME_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            renamed = {k: v for k, v in data.items() if k != key}
            renamed["name"] = value.strip()
            return renamed
    return data


def _decode_tool_call(blob: str, quiet: bool, complete: bool = True) -> tuple[str, dict] | None:
    blob, raw_blocks, cut_block = _take_raw_blocks(blob.strip(), complete)
    blob = blob.strip()
    note = ""

    if cut_block:
        if not quiet:
            print(f"  {S.ERR}✗ The tool call was cut off before it finished; "
                  f"nothing was run.{S.R}")
            print(f"  {S.MUTED}  The reply probably hit num_predict "
                  f"({config.NUM_PREDICT}). Raise it in config.py.{S.R}")
        return None

    try:
        # raw_decode stops at the end of the first JSON value, so a missing or
        # duplicated closing tag does not matter.
        tool_data, _ = json.JSONDecoder().raw_decode(blob)
        if not isinstance(tool_data, dict):
            tool_data = None
    except json.JSONDecodeError:
        tool_data = None

    depth, in_string = _json_tail_state(blob)
    if tool_data is None and not in_string and complete:
        # Repair only what the model finished writing. If it stopped mid-string,
        # or never closed the tag, the payload is genuinely incomplete and
        # closing the brackets would invent arguments that were never sent.
        tool_data = _close_brackets(blob)
        if tool_data is not None:
            note = f"{len(depth)} bracket(s) were left unclosed"
        else:
            tool_data = _lenient_tool_call(blob)
            if tool_data is not None:
                note = "the payload broke the JSON around it and was read by position"

    if tool_data is None:
        if not quiet:
            if in_string or not complete:
                print(f"  {S.ERR}✗ The tool call was cut off before it finished; "
                      f"nothing was run.{S.R}")
                print(f"  {S.MUTED}  The reply probably hit num_predict "
                      f"({config.NUM_PREDICT}). Raise it in config.py.{S.R}")
            else:
                print(f"  {S.ERR}✗ AI generated invalid JSON for the tool call.{S.R}")
        return None

    tool_data = _unwrap_envelope(tool_data)
    tool_data = _rename_to_name(tool_data)

    if not tool_data.get("name"):
        if not quiet:
            print(f"  {S.ERR}✗ The tool call is missing a tool name.{S.R}")
        return None

    arguments = _normalise_arguments(tool_data)
    if not isinstance(tool_data.get("arguments"), dict) or (
            not tool_data.get("arguments") and arguments):
        note = (note + "; " if note else "") + "the parameters were not nested under 'arguments'"
    arguments.update(raw_blocks)          # a raw block always wins over the JSON
    if note and not quiet:
        print(f"  {S.WARN}⚠ Malformed tool call: {note}. Repaired and continuing.{S.R}")
    return tool_data["name"], arguments


def parse_tool_calls(response_text: str, quiet: bool = False) -> list[tuple[str, dict]]:
    """Every tool call in a response, in the order the model wrote them.

    Models routinely ask for two or three things at once. Reading only the first
    threw the rest away and cost a whole extra round trip each - expensive when
    the model is running on your own GPU.
    """
    calls = []
    closed_block = False
    for match in re.finditer(r"<tool_call>(.*?)</tool_call>", response_text, re.DOTALL):
        closed_block = True
        call = _decode_tool_call(match.group(1), quiet)
        if call:
            calls.append(call)
    if calls or closed_block:
        # A properly closed block was already judged on its own merits; retrying
        # it as an unterminated one would only report the same failure twice.
        return calls

    start = response_text.find(TOOL_CALL_TAG)
    if start != -1:
        # The model opened a tool call and never closed it. The tag is hidden
        # from the user either way, so read the JSON that follows rather than
        # letting the turn die silently.
        call = _decode_tool_call(response_text[start + len(TOOL_CALL_TAG):],
                                 quiet, complete=False)
        return [call] if call else []

    return _fenced_tool_calls(response_text, quiet)


def _fenced_tool_calls(response_text: str, quiet: bool) -> list[tuple[str, dict]]:
    """Tool calls a model wrapped in a markdown fence instead of the tag.

    Only reached when there is no `<tool_call>` anywhere in the reply, so this
    can never override a call the model formatted correctly. A fence that does
    not decode to a tool that exists is left alone as the code block it is.
    """
    calls = []
    for match in _FENCED_CALL.finditer(response_text):
        lang = (match.group("lang") or "").lower()
        if lang and lang not in _CALL_FENCE_LANGS and lang != "json":
            continue
        # The raw block that carries a file body is written after the closing
        # fence, so hand over everything up to the next one.
        tail = response_text[match.end():]
        next_fence = tail.find("```")
        blob = match.group("body") + "\n" + (tail if next_fence == -1 else tail[:next_fence])

        call = _decode_tool_call(blob, quiet=True)
        if not call or not toolspec.get(call[0]):
            continue
        if not quiet:
            print(f"  {S.WARN}\u26a0 The tool call came wrapped in a markdown fence "
                  f"instead of {TOOL_CALL_TAG}. Read it anyway.{S.R}")
        calls.append(call)
    return calls


def _render_call(call: dict) -> str:
    """A native call written the way the text protocol would have written it."""
    body = {"name": call.get("name", ""), "arguments": call.get("arguments") or {}}
    return (f"{TOOL_CALL_TAG}\n"
            f"{json.dumps(body, ensure_ascii=False)}\n</tool_call>")


NATIVE_ERROR = "__native_error__"


def _from_native(calls: list) -> list:
    """Native tool-call events as the (name, arguments) pairs dispatch takes.

    A call whose arguments would not parse keeps its name and carries the
    reason under `NATIVE_ERROR` instead. The turn loop reports that to the
    model rather than running the tool - calling `delete_file` with no
    arguments because its JSON arrived broken is not a recoverable mistake.
    """
    pairs = []
    for call in calls:
        name = call.get("name", "")
        if call.get("error"):
            pairs.append((name, {NATIVE_ERROR: call["error"]}))
        else:
            arguments = call.get("arguments")
            pairs.append((name, arguments if isinstance(arguments, dict) else {}))
    return pairs


MAX_PARSE_RETRIES = 2
MAX_EMPTY_REPLIES = 1      # one nudge, then say so

# A refused tool is not an error the model can fix by trying again, and some
# models will try again anyway until the turn's budget is gone. Every refusal
# in this harness - a deny rule, a stage that is read-only, a tool a sub-agent
# does not have, an approval the user declined - starts with this, so counting
# them needs no special case per source.
REFUSAL_PREFIX = config.TOOL_REFUSAL_PREFIX
MAX_REFUSALS_IN_A_ROW = 3

# Auto-verify feeds a failing check back and lets the model fix it. Three goes,
# for the same reason as above: by the fourth it is not converging, and another
# round of guesses is worth less to the person watching than being told plainly
# what is broken. See verify.py.
MAX_VERIFY_FAILURES = 3


def _last_said(messages: list[dict]) -> str:
    """The most recent thing the model actually said, with its calls stripped.

    What a turn hands back when it is cut short: the answer so far, rather than
    the tool result that happens to be last in the list.
    """
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        spoken = re.sub(r"<tool_call>.*?</tool_call>", "", content, flags=re.DOTALL).strip()
        if spoken:
            return spoken
    return ""


async def chat_turn(messages: list[dict]) -> str:
    call_count = 0
    parse_failures = 0
    empty_replies = 0
    refusals = 0
    refusal_nudges = 0
    verify_failures = 0
    verify_done = False        # set when this turn has spent its three goes
    last_failure = ""          # to tell a stuck model from one making progress
    channel_nudged = False     # another agent's question, asked about once
    first_request = True       # the user's message is still the last thing read
    # A turn does not inherit what the last one wrote - only a turn that ran
    # out of tries leaves anything behind. Guarded by the depth even though a
    # sub-agent runs its own loop today: if one is ever routed through here,
    # clearing would throw away the files its parent is waiting to check.
    if not config.SUBAGENT_DEPTH:
        verify.clear()
    while True:
        # Another agent's message is put into the conversation here as well as
        # at the top of the turn, because the turn is where the time goes: a
        # reply that waits for the *next* one waits for the person to type
        # again, and "I am holding parser.py" read twenty tool calls too late
        # is the message the board exists to deliver. Every gap between two
        # requests is offered it, so the longer the job the sooner it lands.
        #
        # Not on the first pass. `app` has just drained the board into the
        # history deliberately *ahead* of the user's message so that the
        # request stays the last thing the model reads, and appending here
        # would undo that on the one pass where it matters.
        #
        # The beat goes with it: nothing else says this agent is alive while a
        # long turn runs, and an agent presumed gone has its claims dropped -
        # the files it is in the middle of writing, exactly while it writes.
        if not first_request and not config.SUBAGENT_DEPTH:
            channel.heartbeat()
            arrived = channel.turn_note(mid_turn=True)
            if arrived:
                messages.append({"role": "user", "content": arrived})
                print(f"  {S.MUTED}↪ a message arrived from another agent; "
                      f"passed it to the model{S.R}")
        first_request = False
        # Text a console mangled cannot be encoded, so one bad character would
        # fail this request and every later one. Repair it before it is sent.
        config.repair_messages(messages)
        # `> 0` on the limit as well as on the count: `/set MAX_TOOL_CALLS 0` is
        # a number the settings accept, and dividing by it ended the turn in a
        # ZeroDivisionError rather than in an answer. Nought means no ceiling.
        limit = int(getattr(config, "MAX_TOOL_CALLS", 10) or 0)
        if limit > 0 and call_count > 0 and call_count % limit == 0:
            print(f"\n  {S.WARN}⚠  Tool call limit ({limit}) reached.{S.R}")
            try:
                user_choice = input(f"  {S.WARN}Continue? {S.MUTED}[{S.OK}y{S.MUTED}/{S.ERR}n{S.MUTED}]{S.R} {S.WARN}›{S.R} ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                user_choice = "n"
            if user_choice != 'y':
                # The last thing the *model* said, not `messages[-2]`, which at
                # this point in the loop is whatever tool result happens to sit
                # there - so stopping a long tool loop used to hand back
                # "[Tool Result for 'read_file']: ..." as the answer.
                return _last_said(messages) or "Tool usage stopped."
                
        native_calls: list = []
        use_native = native_enabled()
        response_text = await stream_reply(
            messages,
            tools=native_tools() if use_native else None,
            calls_out=native_calls if use_native else None)
        stored = response_text if config.STORE_THINKING else strip_thinking(response_text)

        if not stored.strip() and not native_calls:
            # A reasoning model can spend a whole turn thinking and then stop
            # without saying anything. Left alone that shows the user a blank
            # turn and no reason for it, so ask once, and say so if it happens
            # again rather than returning nothing at all.
            if empty_replies < MAX_EMPTY_REPLIES:
                empty_replies += 1
                print(f"  {S.MUTED}⟳ the model reasoned but said nothing; asking again{S.R}")
                messages.append({"role": "user", "content": (
                    "[System] Your last reply was empty - you reasoned but sent no "
                    "answer. Reply now: either the <tool_call> you decided on, or "
                    "your answer to the user. Do not think further.")})
                continue
            print(f"\n  {S.WARN}⚠  The model reasoned but produced no reply, twice. "
                  f"Try rewording the request.{S.R}")
            return ("[The model spent both attempts reasoning without producing a "
                    "reply. Nothing was run.]")

        empty_replies = 0
        if native_calls:
            # History stays plain text for every provider: sessions, the
            # summariser and the token estimate all read one format. Rendering
            # the call back into the tag it would have had over the text
            # protocol keeps that true without a second history shape.
            stored = (stored + "\n" if stored.strip() else "") + "\n".join(
                _render_call(call) for call in native_calls)
        messages.append({"role": "assistant", "content": stored})

        # Native calls come as events; text calls are dug out of the reply. A
        # provider does one or the other, so this is a choice, not a merge.
        parsed = _from_native(native_calls) if native_calls else parse_tool_calls(response_text)
        if not parsed:
            if (not use_native and TOOL_CALL_TAG in response_text
                    and parse_failures < MAX_PARSE_RETRIES):
                # The model meant to call a tool and produced something we could
                # not read. Say so instead of ending the turn in silence - the
                # model can correct itself, and usually does.
                parse_failures += 1
                messages.append({"role": "user", "content": (
                    "[Tool Error]: Your last <tool_call> could not be parsed. Send it "
                    "again as ONE JSON object of exactly this shape:\n"
                    '{"name": "<tool name>", "arguments": {"<param>": "<value>"}}\n'
                    "Every parameter goes inside \"arguments\". Inside a string value, "
                    "write \\\" for a quote, \\\\ for a backslash and \\n for a line break, "
                    "and close every brace you opened.")})
                continue

            # The turn is ending. If another agent asked this one something and
            # it never answered, say so once. A model that writes "you have my
            # agreement to proceed" into its own answer has addressed the other
            # agent and delivered it to nobody, and the asker is sitting there
            # waiting - which is exactly what two 4B models did to each other
            # when this was tried. One nudge, then let it go: being asked twice
            # is not going to work either.
            asked = channel.awaiting_reply() if not channel_nudged else []
            if asked:
                channel_nudged = True
                channel.clear_asked()
                who = ", ".join(asked)
                print(f"  {S.MUTED}↩ {who} asked something that has not been "
                      f"answered; asking the model to reply{S.R}")
                messages.append({"role": "user", "content": (
                    f"[System] {who} sent you a message and is waiting on an "
                    f"answer. Nothing you write here reaches them - your reply "
                    f"goes to the user. Call send_agent_message to answer "
                    f"{who}, then carry on. If you have nothing to say, send "
                    f"them that.")})
                continue
            return stored

        parse_failures = 0

        stop_after_tools = False
        for function_name, arguments in parsed:
            if NATIVE_ERROR in arguments:
                tool_result = (f"[Error] Your call to '{function_name}' could not be "
                               f"read: {arguments[NATIVE_ERROR]}. Nothing was run. "
                               "Make the call again.")
                _fmt_tool_result(function_name, tool_result)
                messages.append({"role": "user", "content":
                                 f"[Tool Result for '{function_name}']:\n{tool_result}"})
                call_count += 1
                continue
            tool_result = dispatch_tool(function_name, arguments)
            if tool_result is None:
                # An unknown tool used to end the turn in silence. Hand the
                # model the error so it can correct itself instead.
                tool_result = (f"[Error] There is no tool named '{function_name}'. "
                               "Use only the tools listed in your system prompt.")

            _fmt_tool_result(function_name, tool_result)

            result_message = {
                "role": "user",
                "content": f"[Tool Result for '{function_name}']:\n{tool_result}",
            }
            # `view_image` cannot hand a picture back through a string, so it
            # leaves the path behind and it is hung on this message - the one
            # the model reads next. Paths, never bytes: invariant 5.3 keeps the
            # stored history plain text, and `providers` encodes on the way out.
            looked_at = images.take_pending()
            if looked_at:
                result_message["images"] = looked_at
            messages.append(result_message)
            call_count += 1

            if isinstance(tool_result, str) and tool_result.startswith(REFUSAL_PREFIX):
                refusals += 1
                if refusals >= MAX_REFUSALS_IN_A_ROW:
                    refusals = 0
                    if refusal_nudges:
                        # Told once already and still knocking. End the turn on
                        # what it has rather than spending the rest of the
                        # budget on a door that is not going to open.
                        print(f"  {S.WARN}⚠  The model kept calling tools it is not "
                              f"allowed right now; ending the turn.{S.R}")
                        stop_after_tools = True
                        break
                    refusal_nudges += 1
                    messages.append({"role": "user", "content": (
                        f"[System] That is the {MAX_REFUSALS_IN_A_ROW}rd tool in a row "
                        "you have been refused. Trying again will be refused again - "
                        "the answer will not change. Stop calling tools and write out "
                        "in words what you wanted to do and why.")})
            else:
                refusals = 0

        if stop_after_tools:
            return stored

        # Every tool call in this reply has run, so the files are in the state
        # the model meant to leave them in. Now the project's own check gets to
        # disagree. A failure goes back as a message rather than as a tool
        # result: the refusal counter above reads `[System]` off tool results
        # to spot a model knocking on a closed door, and this is the opposite -
        # a door it should walk through.
        if verify_done:
            verify.clear()
        elif verify.pending():
            for report in verify.run_pending():
                if report.ok:
                    if verify_failures:
                        messages.append({"role": "user",
                                         "content": verify.recovered_message(report)})
                    verify_failures = 0
                    last_failure = ""
                    continue
                # The count is for a model that is *stuck*, not for a turn that
                # has several things wrong with it. A run against gemma4:e4b hit
                # three different failures - a module that was not callable, a
                # bug that was already in the tree, then a brace it had just
                # deleted - and was stopped on the third, one edit after being
                # handed exactly what it needed to fix it. A failure that reads
                # differently is progress, so the budget starts again; the same
                # text three times over is the door that is not going to open.
                if report.output != last_failure:
                    verify_failures = 0
                last_failure = report.output
                verify_failures += 1
                # `/tdd` exists to stay in this loop, so it gets a longer one.
                budget = (config.TDD_VERIFY_FAILURES if config.TDD_LOCK
                          else MAX_VERIFY_FAILURES)
                if verify_failures >= budget:
                    verify_done = True
                    print(f"  {S.WARN}⚠  The same failure came back {budget} "
                          f"times; asking the model to explain rather than "
                          f"guess again.{S.R}")
                    messages.append({"role": "user",
                                     "content": verify.gave_up_message(budget)})
                    break
                messages.append({"role": "user",
                                 "content": verify.failure_message(report)})
