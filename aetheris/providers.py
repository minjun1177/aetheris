"""Model providers: Ollama, Anthropic, OpenAI (and compatible), Google Gemini.

The harness asks for tool calls as `<tool_call>` text rather than through each
vendor's function-calling API, so a provider only has to do one thing: turn a
list of messages into a stream of text. That is why each of these is a few
dozen lines and needs no SDK - the wire formats differ, the job does not.

Every provider yields the same events, whatever it is talking to:

    {"text": "..."}                        a piece of the reply
    {"thinking": "..."}                    reasoning, where the provider sends it
    {"done": True, "prompt_tokens": N, "completion_tokens": M,
     "total_seconds": f, "eval_seconds": f}

Credentials come from the environment first, then `~/.aetheris/providers.json`,
which is written with owner-only permissions. Nothing is ever stored in the
project directory - that is a place people commit from.
"""

import asyncio
import contextlib
import json
import os
import re
import threading
import time

from aetheris import paths

from aetheris import atomic
from aetheris import keystore
from aetheris.sse import iter_sse


CONFIG_DIR = paths.home()
CONFIG_PATH = os.path.join(CONFIG_DIR, "providers.json")

HTTP_TIMEOUT = (10, 600)          # (connect, read): a long reply is not a hang


def _requests():
    import requests
    return requests


# ---------------------------------------------------------------------------
# message shaping
# ---------------------------------------------------------------------------

class _PartialCalls:
    """Collects tool calls that arrive as a stream of JSON fragments.

    Anthropic and OpenAI both send a tool call's arguments a few characters at
    a time, so nothing can be parsed until the last fragment has landed. Each
    call is kept under the index or id the provider tags it with, and turned
    into an event only when the provider says it is finished.
    """

    def __init__(self):
        self.calls = {}

    def start(self, key, name: str = "", call_id: str = "") -> None:
        entry = self.calls.setdefault(key, {"name": "", "id": "", "json": ""})
        if name:
            entry["name"] = name
        if call_id:
            entry["id"] = call_id

    def feed(self, key, fragment: str) -> None:
        if key in self.calls and fragment:
            self.calls[key]["json"] += fragment

    def take(self, key):
        entry = self.calls.pop(key, None)
        return _decode_call(entry) if entry else None

    def drain(self):
        for key in list(self.calls):
            event = self.take(key)
            if event:
                yield event


def _decode_call(entry: dict):
    """One collected call as a `tool_call` event, or None if it has no name."""
    name = (entry.get("name") or "").strip()
    if not name:
        return None
    blob = (entry.get("json") or "").strip()
    call = {"name": name, "id": entry.get("id", ""), "arguments": {}}
    if not blob:
        return {"tool_call": call}         # a tool that takes no arguments
    try:
        arguments = json.loads(blob)
    except json.JSONDecodeError as error:
        # The provider built this JSON, so this should not happen - but saying
        # so beats calling the tool with nothing and reporting a bad result.
        call["error"] = f"the arguments were not valid JSON ({error})"
        return {"tool_call": call}
    call["arguments"] = arguments if isinstance(arguments, dict) else {}
    return {"tool_call": call}


def token_budget(max_tokens: int | None) -> int:
    """The output cap to send, as an int.

    Every hosted API rejects a float here, and the setting is easy to write as
    one (`4096 * 1.5` is 6144.0), so it is coerced in the one place they all
    pass through rather than trusted at four call sites.
    """
    from aetheris import config       # lazily, like everywhere else here - see the header
    return int(max_tokens or config.NUM_PREDICT)


def split_system(messages: list) -> tuple:
    """Separate the system prompt from the conversation.

    Ollama and OpenAI take it as a message; Anthropic and Gemini take it as its
    own field.
    """
    system = "\n\n".join(m.get("content", "") for m in messages
                         if m.get("role") == "system").strip()
    rest = [m for m in messages if m.get("role") != "system"]
    return system, rest


def merge_runs(messages: list) -> list:
    """Collapse consecutive same-role messages into one.

    The harness appends every tool result as its own user message, so a turn
    routinely ends up with several in a row. Some APIs reject that.
    """
    merged = []
    for message in messages:
        content = message.get("content", "")
        if merged and merged[-1]["role"] == message.get("role"):
            merged[-1]["content"] += "\n\n" + content
        else:
            merged.append({"role": message.get("role", "user"), "content": content})
    return merged


def carried_images(message: dict) -> list:
    """The image paths a message carries, capped, as a list.

    Images live beside `content` rather than inside it (see `images.py`), so
    every function above this one goes on seeing the plain string it expects.
    """
    from aetheris import config
    found = message.get("images")
    if not isinstance(found, list):
        return []
    return [p for p in found if isinstance(p, str) and p][:config.IMAGE_MAX_PER_MESSAGE]


def has_images(messages: list) -> bool:
    return any(carried_images(m) for m in messages)


def _encoded(message: dict) -> list:
    """(base64, media type) for each image on a message that could be read.

    A file that has gone since it was attached is skipped rather than fatal: a
    resumed conversation whose screenshot was tidied away should still answer
    about everything else that was said.
    """
    from aetheris import images
    out = []
    for path in carried_images(message):
        data, kind, _note = images.encode(path)
        if data:
            out.append((data, kind))
    return out


def merge_runs_with_images(messages: list) -> list:
    """`merge_runs`, keeping the image paths of everything it merged.

    Merging drops keys other than role and content, which would silently throw
    an attachment away the moment a tool result landed after it.
    """
    merged = []
    for message in messages:
        content = message.get("content", "")
        pictures = carried_images(message)
        if merged and merged[-1]["role"] == message.get("role"):
            merged[-1]["content"] += "\n\n" + content
            merged[-1]["images"] = merged[-1].get("images", []) + pictures
        else:
            merged.append({"role": message.get("role", "user"),
                           "content": content, "images": pictures})
    return [{k: v for k, v in m.items() if k != "images" or v} for m in merged]


# ---------------------------------------------------------------------------
# prompt caching
# ---------------------------------------------------------------------------
#
# Every request re-sends the whole conversation, and the front of it never
# changes: about 6,000 tokens of system prompt and tool schemas, of which the
# 33 tool descriptions are two thirds. A turn that takes sixteen requests sends
# that sixteen times. Against Ollama it is re-counted but not re-computed -
# llama.cpp reuses the KV cache for an unchanged prefix - so it costs context
# budget and nothing else. Against a hosted API it is billed every time.
#
# The three hosted providers divide into two kinds, and only one needs code:
#
#   Anthropic  explicit. A `cache_control` breakpoint marks where the cacheable
#              prefix ends. Nothing is cached without one. `_cached_system`.
#   OpenAI     automatic, on by default above a per-model minimum. Nothing to
#              send; the only gap was that nobody could see whether it worked.
#   Gemini     automatic ("implicit caching") on 2.5 and newer, same story.
#
# So what all three needed was the *reporting*: `cached_tokens` on the `done`
# event. Caching is a prefix match, and a single byte that moves invalidates
# everything after it - which fails silently, as a bill rather than an error.
# The number that says whether it is working is the only thing that separates
# "cached" from "assumed to be cached" (invariant 5.10).
#
# What must stay true for any of it to work, in this codebase:
#   * `systemprompt()` must be byte-identical between requests. No clock, no
#     uuid, no set iterated into text. It is, and `test_caching.py` checks it.
#   * the tool list must be sent in the same order every time. It is: both
#     `toolspec.native_schema` and `mcp_client.native_tool_schemas` walk fixed
#     lists.
#   * rewriting history rewrites the prefix. `context._trim_tool_results` and
#     the `<SUMMARY>` that compression puts into `messages[0]` both do, and
#     both cost one cache miss when they happen. They converge - a result is
#     not trimmed twice - so this is a miss, not a permanent one.

_CACHE_BREAKPOINT = {"type": "ephemeral"}       # the 5-minute TTL; a read refreshes it


def _openai_messages(messages: list) -> list:
    """OpenAI's messages, with any images as content parts beside the text.

    A message with no image keeps its plain string content. OpenAI accepts the
    part list everywhere, but sending one for every message would change the
    shape of every request this harness has ever made for no reason - and the
    OpenAI-compatible servers behind `base_url` are not all as forgiving.
    """
    if not has_images(messages):
        return merge_runs(messages)
    out = []
    for message in merge_runs_with_images(messages):
        pictures = _encoded(message)
        if not pictures:
            out.append({k: v for k, v in message.items() if k != "images"})
            continue
        parts = [{"type": "text", "text": message.get("content", "")}]
        parts.extend({"type": "image_url",
                      "image_url": {"url": f"data:{kind};base64,{data}"}}
                     for data, kind in pictures)
        out.append({"role": message["role"], "content": parts})
    return out


def _gemini_parts(message: dict) -> list:
    """One Gemini `parts` list: the images it carries, then its text."""
    parts = [{"inline_data": {"mime_type": kind, "data": data}}
             for data, kind in _encoded(message)]
    parts.append({"text": message.get("content", "")})
    return parts


def _cached_system(system: str) -> list:
    """The system prompt as one cacheable block.

    Anthropic renders `tools` -> `system` -> `messages`, so a breakpoint on the
    last system block covers the tool schemas as well - which is where two
    thirds of the fixed cost is. One breakpoint, both halves.
    """
    return [{"type": "text", "text": system, "cache_control": dict(_CACHE_BREAKPOINT)}]


def _anthropic_messages(conversation: list) -> list:
    """Anthropic's messages, with any images as blocks ahead of the text.

    Images first: that is the documented placement, and a question reads as a
    question about the picture above it rather than about nothing yet seen.
    A message with no image keeps its plain string content, so the ordinary
    request is byte for byte what it was before images existed.
    """
    out = []
    for message in merge_runs_with_images(conversation):
        pictures = _encoded(message)
        if not pictures:
            out.append({k: v for k, v in message.items() if k != "images"})
            continue
        blocks = [{"type": "image",
                   "source": {"type": "base64", "media_type": kind, "data": data}}
                  for data, kind in pictures]
        blocks.append({"type": "text", "text": message.get("content", "")})
        out.append({"role": message["role"], "content": blocks})
    return out


def _cache_tail(conversation: list) -> list:
    """Mark the end of the conversation so far as a second read point.

    The system prefix alone leaves the growing half uncached, and the growing
    half is what a tool loop re-sends sixteen times. This is the documented
    multi-turn placement: a breakpoint on the last block of the most recently
    appended turn, so the next request reads everything before it.

    The list is rebuilt rather than mutated - `merge_runs` hands back fresh
    dicts, but the caller's `messages` must not grow an Anthropic-shaped
    content block either way (invariant 5.3: history is plain text).
    """
    if not conversation:
        return conversation
    marked = list(conversation)
    last = dict(marked[-1])
    content = last.get("content")
    if isinstance(content, list) and content:
        # Already blocks, because the message carries an image. The breakpoint
        # goes on the final block, which is the text - marking the image would
        # leave the question it belongs to outside the cached prefix.
        blocks = [dict(b) for b in content]
        blocks[-1] = {**blocks[-1], "cache_control": dict(_CACHE_BREAKPOINT)}
        last["content"] = blocks
        marked[-1] = last
        return marked
    if not isinstance(content, str) or not content:
        return conversation          # not a shape this knows how to mark
    last["content"] = [{"type": "text", "text": content,
                        "cache_control": dict(_CACHE_BREAKPOINT)}]
    marked[-1] = last
    return marked


# ---------------------------------------------------------------------------
# what a provider said when it refused
# ---------------------------------------------------------------------------
#
# An API key is the thing that goes wrong most often here, and it is also the
# thing this used to be worst at reporting. `raise_for_status` gives the status
# line and discards the body, so "Incorrect API key provided: sk-proj-…" - which
# names the key and links the page to fix it - became "401 Client Error:
# Unauthorized". The streaming path did keep the body and then cut it at 400
# characters, which is enough for a short message and not for a long one, and
# `/connect` cut whatever survived that to 160.
#
# All three hosted APIs answer an error the same way, so one reader serves all
# of them:
#
#   Anthropic  {"type": "error", "error": {"type": …, "message": …}}
#   OpenAI     {"error": {"message": …, "type": …, "code": …}}
#   Gemini     {"error": {"code": …, "message": …, "status": …}}

ERROR_DETAIL_CHARS = 2000       # a message the provider wrote: show it whole
RAW_DETAIL_CHARS = 400          # an HTML page nobody wrote for a person: show a little

_TAGS = re.compile(r"<[^>]+>")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.DOTALL)


def _error_detail(response, limit: int = ERROR_DETAIL_CHARS) -> str:
    """The readable part of a failed response's body.

    Two very different things arrive here and they get different room. A
    provider's own error message is a sentence written to be read, and is worth
    every character - that is the one that was being cut. A 502 page from a
    proxy in front of the API is markup, repeated boilerplate and no
    information beyond its title, and printing all of it would bury the request
    that failed under thirty lines of nothing.
    """
    try:
        body = response.text or ""
    except Exception:
        return ""

    detail = ""
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        payload = None

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            detail = str(error.get("message") or "").strip()
            label = error.get("code") or error.get("type") or error.get("status") or ""
            if detail and label:
                detail = f"{detail} [{label}]"
        elif isinstance(error, str):
            detail = error.strip()
        if not detail:
            for key in ("message", "detail", "error_description"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    detail = value.strip()
                    break
    if detail:
        return _cut(detail, limit)

    # Not JSON, or JSON with nothing named in it. The title of an error page is
    # usually the whole of what it has to say ("502 Bad Gateway"), so it leads.
    title = _TITLE.search(body)
    stripped = " ".join(_TAGS.sub(" ", body).split())
    if title:
        heading = " ".join(title.group(1).split())
        rest = stripped[len(heading):].strip() if stripped.startswith(heading) else stripped
        return _cut(f"{heading} - {rest}" if rest else heading, RAW_DETAIL_CHARS)
    return _cut(stripped, RAW_DETAIL_CHARS)


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"… (cut from {len(text)} characters)"


def _key_hint(provider, status: int) -> str:
    """The sentence that turns a status code into something to actually do."""
    if status in (401, 403):
        where = " or ".join(provider.key_env) if provider.key_env else "the API key"
        return (f"\n  This is an API key problem. The key came from "
                f"{provider.key_source or 'nowhere - none is set'}; set {where} in "
                f"the environment, or run /connect to paste one. "
                f"/connect forget {provider.name} deletes a saved key.")
    if status == 429:
        return ("\n  Rate limited, or the account is out of credit - the message "
                "above says which. Waiting fixes the first; only the provider's "
                "billing page fixes the second.")
    if status == 404:
        return (f"\n  Check the model id: /models lists what {provider.label} "
                f"actually offers.")
    return ""


def _as_stream(make_chunks):
    """Turn a blocking generator into an async one, off the event loop.

    Without this the spinner would freeze and the tokens-per-second counter
    would stop while the reply streams in.
    """
    async def stream():
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        done = object()

        def worker():
            try:
                for item in make_chunks():
                    loop.call_soon_threadsafe(queue.put_nowait, item)
            except BaseException as error:               # noqa: BLE001 - re-raised below
                loop.call_soon_threadsafe(queue.put_nowait, error)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, done)

        threading.Thread(target=worker, daemon=True).start()
        while True:
            item = await queue.get()
            if item is done:
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    return stream()


# ---------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------

class Provider:
    name = ""
    label = ""
    key_env: tuple = ()
    key_help = ""
    default_base_url = ""
    needs_key = True
    # True for a provider that runs the agent loop itself, somewhere else, and
    # only calls back for tools - Attacca. `app` hands such a turn to the
    # provider whole instead of driving `chat_turn` against it.
    drives_turns = False
    # True where the credential comes from pairing with a code rather than
    # from a key someone pastes - `/connect` shows the code instead of asking.
    pairs = False

    def __init__(self, settings: dict | None = None):
        self.settings = settings or {}

    # Whether this provider takes tool schemas over its own API. Ollama stays
    # False on purpose: the text <tool_call> protocol and its JSON repair are
    # what make small local models usable, and nothing here should disturb them.
    supports_native_tools = False

    def encode_tools(self, schemas: list) -> list:
        """Reshape `toolspec.native_schema()` into this provider's wire format."""
        raise NotImplementedError

    # -- configuration -----------------------------------------------------

    @property
    def api_key(self) -> str:
        for variable in self.key_env:
            value = os.environ.get(variable)
            if value:
                return value.strip()
        return str(self.settings.get("api_key") or "").strip()

    @property
    def base_url(self) -> str:
        return str(self.settings.get("base_url") or self.default_base_url).rstrip("/")

    def sees_images(self) -> bool:
        """Whether this model can be sent an image at all.

        True for the hosted three: every current model of theirs accepts one,
        the list moves weekly, and when one does not the API says so plainly.
        Ollama overrides it, because there the answer is per model and nothing
        fails when it is no - see `OllamaProvider.sees_images`.
        """
        return True

    @property
    def model(self) -> str:
        return str(self.settings.get("model") or "")

    @property
    def key_source(self) -> str:
        for variable in self.key_env:
            if os.environ.get(variable):
                return f"${variable}"
        return "saved" if self.settings.get("api_key") else ""

    def ready(self) -> str:
        """"" when this provider can be used, otherwise what is missing."""
        if self.needs_key and not self.api_key:
            return f"no API key ({' or '.join(self.key_env)})"
        if not self.model:
            return "no model chosen"
        return ""

    # -- what a subclass implements ---------------------------------------

    def list_models(self) -> list:
        """[{"name": ..., "detail": ...}], newest/most useful first."""
        raise NotImplementedError

    def stream(self, messages: list, max_tokens: int | None = None):
        raise NotImplementedError

    # -- shared helpers ----------------------------------------------------

    def _failed(self, response) -> str:
        """Why a request was refused, in the words the provider used."""
        detail = _error_detail(response)
        return (f"{self.label} returned HTTP {response.status_code}"
                + (f": {detail}" if detail else "")
                + _key_hint(self, response.status_code))

    def _get_json(self, url: str, headers: dict) -> dict:
        response = _requests().get(url, headers=headers, timeout=30)
        # Not `raise_for_status`, which reports the status line and throws the
        # body away - and the body is the whole of what a person needs here.
        # This is the call `/connect` and `/models` make, so it is the first
        # place a wrong API key shows up, and it used to show up as
        # "401 Client Error: Unauthorized" with no mention of a key at all.
        if response.status_code >= 400:
            raise RuntimeError(self._failed(response))
        try:
            return response.json()
        except ValueError as error:
            raise RuntimeError(f"{self.label} sent something that is not JSON "
                               f"({error}): {_error_detail(response, 400)}")

    def _post_sse(self, url: str, headers: dict, payload: dict):
        response = _requests().post(url, headers=headers,
                                    data=json.dumps(payload).encode("utf-8"),
                                    stream=True, timeout=HTTP_TIMEOUT)
        if response.status_code >= 400:
            raise RuntimeError(self._failed(response))
        return response


# What each local model says it can do, asked once per model. `ollama.show`
# is a local call, but the prompt is rebuilt often enough that asking every
# time would be wasteful, and a daemon that is down must not stall startup.
# Keyed by (host, model): the same model name on another machine is another
# model, and answering from a cache filled against localhost is how the
# harness would decide a model cannot do something it can.
_ollama_capability_lists: dict = {}


def ollama_client(host: str = "", timeout: float | None = None):
    """A synchronous Ollama client pointed at `host`.

    One place that knows how to build one, because three callers used to reach
    for `ollama.Client()` with no host at all - so pointing the harness at
    another machine moved the chat and left the model list, the capability
    probe and the summariser's sizing behind, talking to localhost.
    """
    import ollama
    arguments = {}
    if host:
        arguments["host"] = host
    if timeout is not None:
        arguments["timeout"] = timeout
    return ollama.Client(**arguments)


def ollama_supports_tools(model: str, host: str = "") -> bool:
    """Whether this local model was built with a tool-calling template.

    Ollama reports it outright, and it is worth asking: a model whose template
    cannot format a tool call will simply never make one, while a model that
    can is more accurate through that interface than through text. Models
    differ - of the twenty installed here, five have no tool support at all -
    so this is per model, not per provider.

    Anything that goes wrong means no: the text protocol works everywhere, so
    falling back to it is always safe.
    """
    return "tools" in ollama_capabilities(model, host)


def ollama_capabilities(model: str, host: str = "") -> list:
    """What Ollama says this local model can do, or [] if it will not say.

    Cached per (host, model) for the reason `ollama_supports_tools` gives: the
    same name on another machine is another model, and an answer from the wrong
    cache is how the harness decides a model cannot do something it can.
    """
    if not model:
        return []
    key = (host, model)
    if key in _ollama_capability_lists:
        return _ollama_capability_lists[key]
    found = []
    try:
        shown = ollama_client(host, timeout=2.0).show(model)
        found = list((shown.get("capabilities") if isinstance(shown, dict)
                      else getattr(shown, "capabilities", None)) or [])
    except Exception:
        found = []
    _ollama_capability_lists[key] = found
    return found


def ollama_vision_models(host: str = "") -> list:
    """Installed local models that report `vision`, so a refusal can name one."""
    try:
        listed = ollama_client(host, timeout=2.0).list()
    except Exception:
        return []
    # The client hands back a typed response on some versions and a plain dict
    # on others, and the same is true of each entry - so both are read, the way
    # `ollama_capabilities` reads `show()`.
    entries = (listed.get("models") if isinstance(listed, dict)
               else getattr(listed, "models", None)) or []
    names = []
    for entry in entries:
        if isinstance(entry, dict):
            name = entry.get("model") or entry.get("name") or ""
        else:
            name = getattr(entry, "model", "") or getattr(entry, "name", "") or ""
        if name and "vision" in ollama_capabilities(name, host):
            names.append(name)
    return names


def _ollama_messages(messages: list) -> list:
    """The conversation with image paths turned into what Ollama wants.

    Ollama's own wire format already carries images beside the text, under the
    same key name this harness stores them under - so this swaps paths for
    base64 and changes nothing else. Messages with no image are handed back
    untouched, which is all of them in the ordinary case.
    """
    if not has_images(messages):
        return messages
    out = []
    for message in messages:
        encoded = [data for data, _kind in _encoded(message)]
        if encoded:
            out.append({**{k: v for k, v in message.items() if k != "images"},
                        "images": encoded})
        else:
            out.append({k: v for k, v in message.items() if k != "images"})
    return out


def _ollama_calls(message):
    """Tool calls out of one Ollama chunk, however the client wrapped them.

    Ollama sends a call whole and has already parsed its arguments, so there
    is nothing to accumulate. The client returns pydantic objects rather than
    plain dicts, and older versions returned dicts, so both are read here.
    """
    raw = (message.get("tool_calls") if isinstance(message, dict)
           else getattr(message, "tool_calls", None)) or []
    events = []
    for call in raw:
        function = (call.get("function") if isinstance(call, dict)
                    else getattr(call, "function", None)) or {}
        name = (function.get("name") if isinstance(function, dict)
                else getattr(function, "name", "")) or ""
        arguments = (function.get("arguments") if isinstance(function, dict)
                     else getattr(function, "arguments", None))
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}
        if not name:
            continue
        events.append({"tool_call": {
            "name": name, "id": "",
            "arguments": dict(arguments) if isinstance(arguments, dict) else {}}})
    return events


class OllamaProvider(Provider):
    name = "ollama"
    label = "Ollama"
    needs_key = False
    key_help = "Runs locally - no API key needed."

    @property
    def model(self) -> str:
        # Someone who never runs /connect keeps the model from config.py.
        from aetheris import config
        return str(self.settings.get("model") or config.MODEL or "")

    @property
    def host(self) -> str:
        """Where the daemon is, most explicit first.

        A `base_url` saved by `/connect` is the narrowest instruction, then
        `OLLAMA_HOST` changed with `/set`, then the environment variable the
        Ollama tools themselves read, then the default. The setting is checked
        against its default rather than for emptiness so that leaving it alone
        does not silently override a `$OLLAMA_HOST` somebody already relies on.
        """
        from aetheris import config
        saved = str(self.settings.get("base_url") or "").strip()
        if saved:
            return saved.rstrip("/")
        chosen = str(getattr(config, "OLLAMA_HOST", "") or "").strip()
        default = config.defaults().get("OLLAMA_HOST", "")
        if chosen and chosen != default:
            return chosen.rstrip("/")
        return (os.environ.get("OLLAMA_HOST") or chosen or default).strip().rstrip("/")

    @property
    def supports_native_tools(self) -> bool:
        """Unlike the hosted providers, this depends on the model, not the API."""
        return ollama_supports_tools(self.model, self.host)

    def sees_images(self) -> bool:
        """Whether this local model was built to take an image.

        Ollama reports it in the same `capabilities` list that says whether the
        model can call a tool. Worth asking, because unlike the hosted APIs
        nothing here fails: an image sent to a model without `vision` is
        dropped on the way in, and the model then answers about the sentence
        alone - confidently, and about a picture it never saw. A wrong answer
        with no error in it is the worst outcome available.
        """
        return "vision" in ollama_capabilities(self.model, self.host)

    def encode_tools(self, schemas: list) -> list:
        # Ollama takes OpenAI's shape.
        return [{"type": "function",
                 "function": {"name": s["name"], "description": s["description"],
                              "parameters": s["input_schema"]}} for s in schemas]

    def _client(self):
        import ollama
        return ollama.AsyncClient(host=self.host) if self.host else ollama.AsyncClient()

    def list_models(self) -> list:
        listing = ollama_client(self.host).list()
        entries = listing.get("models", []) if isinstance(listing, dict) \
            else getattr(listing, "models", [])
        models = []
        for entry in entries:
            name = entry.get("model", entry.get("name")) if isinstance(entry, dict) \
                else getattr(entry, "model", None)
            size = entry.get("size", 0) if isinstance(entry, dict) else getattr(entry, "size", 0)
            if not name:
                continue
            detail = f"{size / 1024 ** 3:.1f}GB" if size >= 1024 ** 3 else \
                (f"{size / 1024 ** 2:.0f}MB" if size else "")
            models.append({"name": name, "detail": detail})
        return models

    async def stream(self, messages: list, max_tokens: int | None = None,
                     tools: list | None = None):
        from aetheris import config
        options = {"num_ctx": config.NUM_CTX,
                   "num_predict": token_budget(max_tokens)}
        request = {"model": self.model, "messages": _ollama_messages(messages),
                   "stream": True, "options": options}
        if tools:
            request["tools"] = self.encode_tools(tools)
        response = await self._client().chat(**request)
        async for chunk in response:
            message = chunk.get("message") or {}
            text = message.get("content", "") or ""
            thinking = message.get("thinking", "") or ""
            if thinking:
                yield {"thinking": thinking}
            if text:
                yield {"text": text}
            for call in _ollama_calls(message):
                yield call
            if chunk.get("done"):
                yield {"done": True,
                       "prompt_tokens": chunk.get("prompt_eval_count", 0) or 0,
                       "completion_tokens": chunk.get("eval_count", 0) or 0,
                       "total_seconds": (chunk.get("total_duration", 0) or 0) / 1e9,
                       "eval_seconds": (chunk.get("eval_duration", 0) or 0) / 1e9}


class AnthropicProvider(Provider):
    name = "anthropic"
    label = "Anthropic"
    key_env = ("ANTHROPIC_API_KEY",)
    key_help = "console.anthropic.com -> API keys"
    default_base_url = "https://api.anthropic.com"
    api_version = "2023-06-01"
    supports_native_tools = True

    def encode_tools(self, schemas: list) -> list:
        # The canonical shape is already Anthropic's, so this is a copy rather
        # than a translation - kept explicit so the three stay symmetrical.
        return [{"name": s["name"], "description": s["description"],
                 "input_schema": s["input_schema"]} for s in schemas]

    def _headers(self) -> dict:
        return {"x-api-key": self.api_key,
                "anthropic-version": self.api_version,
                "content-type": "application/json"}

    def list_models(self) -> list:
        data = self._get_json(f"{self.base_url}/v1/models?limit=100", self._headers())
        return [{"name": item["id"], "detail": item.get("display_name", "")}
                for item in data.get("data", []) if item.get("id")]

    def stream(self, messages: list, max_tokens: int | None = None,
               tools: list | None = None):
        system, conversation = split_system(messages)
        # Two of the four breakpoints: the end of the fixed prefix (tools and
        # system together - see `_cached_system`), and the end of the
        # conversation so far, which is what a tool loop re-sends every time.
        payload = {
            "model": self.model,
            # Anthropic requires max_tokens; it is a cap, not a target.
            "max_tokens": token_budget(max_tokens),
            "messages": _cache_tail(_anthropic_messages(conversation))
                        or [{"role": "user", "content": "."}],
            "stream": True,
        }
        if system:
            payload["system"] = _cached_system(system)
        if tools:
            payload["tools"] = self.encode_tools(tools)

        def chunks():
            response = self._post_sse(f"{self.base_url}/v1/messages",
                                      self._headers(), payload)
            prompt_tokens = completion_tokens = cached_tokens = 0
            pending = _PartialCalls()
            started = time.time()
            for _event, data in iter_sse(response):
                if not data.strip():
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue          # a bare value: nothing to read from it
                kind = event.get("type")
                if kind == "message_start":
                    usage = (event.get("message") or {}).get("usage") or {}
                    prompt_tokens = usage.get("input_tokens", 0) or 0
                    # `input_tokens` counts only what was NOT served from the
                    # cache, so the three are added up to get what the request
                    # actually carried - otherwise a working cache reads as a
                    # prompt that suddenly shrank.
                    cached_tokens = usage.get("cache_read_input_tokens", 0) or 0
                    prompt_tokens += cached_tokens + (
                        usage.get("cache_creation_input_tokens", 0) or 0)
                elif kind == "content_block_start":
                    block = event.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        pending.start(event.get("index"), block.get("name", ""),
                                      block.get("id", ""))
                elif kind == "content_block_delta":
                    delta = event.get("delta") or {}
                    if delta.get("type") == "thinking_delta":
                        yield {"thinking": delta.get("thinking", "")}
                    elif delta.get("type") == "input_json_delta":
                        pending.feed(event.get("index"), delta.get("partial_json", ""))
                    elif delta.get("text"):
                        yield {"text": delta["text"]}
                elif kind == "content_block_stop":
                    finished = pending.take(event.get("index"))
                    if finished:
                        yield finished
                elif kind == "message_delta":
                    completion_tokens = (event.get("usage") or {}).get(
                        "output_tokens", completion_tokens) or completion_tokens
                elif kind == "error":
                    detail = (event.get("error") or {}).get("message", "unknown error")
                    raise RuntimeError(f"Anthropic: {detail}")
            yield from pending.drain()      # a stream cut short still reports
            elapsed = time.time() - started
            yield {"done": True, "prompt_tokens": prompt_tokens,
                   "completion_tokens": completion_tokens, "cached_tokens": cached_tokens,
                   "total_seconds": elapsed, "eval_seconds": elapsed}

        return _as_stream(chunks)


class OpenAIProvider(Provider):
    name = "openai"
    label = "OpenAI"
    key_env = ("OPENAI_API_KEY",)
    key_help = "platform.openai.com -> API keys"
    default_base_url = "https://api.openai.com/v1"
    supports_native_tools = True

    def encode_tools(self, schemas: list) -> list:
        return [{"type": "function",
                 "function": {"name": s["name"], "description": s["description"],
                              "parameters": s["input_schema"]}} for s in schemas]

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"}

    def list_models(self) -> list:
        data = self._get_json(f"{self.base_url}/models", self._headers())
        names = sorted(item["id"] for item in data.get("data", []) if item.get("id"))
        return [{"name": name, "detail": ""} for name in names]

    def stream(self, messages: list, max_tokens: int | None = None,
               tools: list | None = None):
        payload = {
            "model": self.model,
            "messages": _openai_messages(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_completion_tokens": token_budget(max_tokens),
        }
        if tools:
            payload["tools"] = self.encode_tools(tools)

        def chunks():
            try:
                response = self._post_sse(f"{self.base_url}/chat/completions",
                                          self._headers(), payload)
            except RuntimeError as error:
                # Older models and most OpenAI-compatible servers only know the
                # original parameter name.
                if "max_completion_tokens" not in str(error):
                    raise
                retry = dict(payload)
                retry["max_tokens"] = retry.pop("max_completion_tokens")
                response = self._post_sse(f"{self.base_url}/chat/completions",
                                          self._headers(), retry)

            prompt_tokens = completion_tokens = cached_tokens = 0
            pending = _PartialCalls()
            started = time.time()
            for _event, data in iter_sse(response):
                data = data.strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue          # a bare value: nothing to read from it
                if event.get("error"):
                    raise RuntimeError(f"OpenAI: {event['error'].get('message', 'error')}")
                usage = event.get("usage")
                if usage:
                    prompt_tokens = usage.get("prompt_tokens", prompt_tokens) or prompt_tokens
                    completion_tokens = usage.get("completion_tokens",
                                                  completion_tokens) or completion_tokens
                    # Caching here is automatic above a per-model minimum -
                    # there is nothing to send. Both spellings are read because
                    # the field moved between API surfaces, and an
                    # OpenAI-compatible server may report neither.
                    details = (usage.get("prompt_tokens_details")
                               or usage.get("input_tokens_details") or {})
                    if isinstance(details, dict):
                        cached_tokens = details.get("cached_tokens", cached_tokens) or cached_tokens
                for choice in event.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("reasoning_content"):
                        yield {"thinking": delta["reasoning_content"]}
                    if delta.get("content"):
                        yield {"text": delta["content"]}
                    for call in delta.get("tool_calls") or []:
                        # `index` is what ties the fragments of one call
                        # together; the id and name arrive only in the first.
                        key = call.get("index", 0)
                        function = call.get("function") or {}
                        pending.start(key, function.get("name", ""), call.get("id", ""))
                        pending.feed(key, function.get("arguments", ""))
            # OpenAI marks the end with finish_reason rather than a per-call
            # stop event, so the calls are read out once the stream is over.
            yield from pending.drain()
            elapsed = time.time() - started
            yield {"done": True, "prompt_tokens": prompt_tokens,
                   "completion_tokens": completion_tokens, "cached_tokens": cached_tokens,
                   "total_seconds": elapsed, "eval_seconds": elapsed}

        return _as_stream(chunks)


class GeminiProvider(Provider):
    name = "gemini"
    label = "Google Gemini"
    key_env = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
    key_help = "aistudio.google.com -> Get API key"
    default_base_url = "https://generativelanguage.googleapis.com"
    supports_native_tools = True

    def encode_tools(self, schemas: list) -> list:
        """One declaration per tool, inside the single `tools` entry Gemini takes.

        Gemini validates the schema against its own OpenAPI subset and rejects
        a parameter object with nothing in it, so a tool that takes no
        arguments is declared without a schema at all.
        """
        declarations = []
        for schema in schemas:
            entry = {"name": schema["name"], "description": schema["description"]}
            parameters = schema["input_schema"]
            if parameters.get("properties"):
                entry["parameters"] = {
                    "type": "object",
                    "properties": parameters["properties"],
                }
                if parameters.get("required"):
                    entry["parameters"]["required"] = parameters["required"]
            declarations.append(entry)
        return [{"functionDeclarations": declarations}]

    def _headers(self) -> dict:
        return {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}

    def list_models(self) -> list:
        data = self._get_json(f"{self.base_url}/v1beta/models?pageSize=200",
                              self._headers())
        models = []
        for item in data.get("models", []):
            name = str(item.get("name", "")).removeprefix("models/")
            methods = item.get("supportedGenerationMethods") or []
            if name and (not methods or "generateContent" in methods):
                models.append({"name": name, "detail": item.get("displayName", "")})
        return models

    def stream(self, messages: list, max_tokens: int | None = None,
               tools: list | None = None):
        system, conversation = split_system(messages)
        contents = [{"role": "model" if m["role"] == "assistant" else "user",
                     "parts": _gemini_parts(m)}
                    for m in merge_runs_with_images(conversation)]
        payload = {
            "contents": contents or [{"role": "user", "parts": [{"text": "."}]}],
            "generationConfig": {"maxOutputTokens": token_budget(max_tokens)},
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            payload["tools"] = self.encode_tools(tools)

        url = (f"{self.base_url}/v1beta/models/{self.model}"
               ":streamGenerateContent?alt=sse")

        def chunks():
            response = self._post_sse(url, self._headers(), payload)
            prompt_tokens = completion_tokens = cached_tokens = 0
            started = time.time()
            for _event, data in iter_sse(response):
                if not data.strip():
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue          # a bare value: nothing to read from it
                if event.get("error"):
                    raise RuntimeError(f"Gemini: {event['error'].get('message', 'error')}")
                usage = event.get("usageMetadata") or {}
                prompt_tokens = usage.get("promptTokenCount", prompt_tokens) or prompt_tokens
                completion_tokens = usage.get("candidatesTokenCount",
                                              completion_tokens) or completion_tokens
                # Implicit caching, on by default from 2.5 onwards; unlike
                # Anthropic, promptTokenCount already includes this.
                cached_tokens = usage.get("cachedContentTokenCount",
                                          cached_tokens) or cached_tokens
                for candidate in event.get("candidates") or []:
                    for part in (candidate.get("content") or {}).get("parts") or []:
                        call = part.get("functionCall")
                        if call and call.get("name"):
                            # Gemini sends a call whole, so there is nothing to
                            # accumulate and nothing that can arrive half-parsed.
                            arguments = call.get("args")
                            yield {"tool_call": {
                                "name": call["name"], "id": "",
                                "arguments": arguments if isinstance(arguments, dict) else {}}}
                            continue
                        text = part.get("text")
                        if not text:
                            continue
                        if part.get("thought"):
                            yield {"thinking": text}
                        else:
                            yield {"text": text}
            elapsed = time.time() - started
            yield {"done": True, "prompt_tokens": prompt_tokens,
                   "completion_tokens": completion_tokens, "cached_tokens": cached_tokens,
                   "total_seconds": elapsed, "eval_seconds": elapsed}

        return _as_stream(chunks)


class AttaccaProvider(Provider):
    """An agent hosted on Attacca, driving this machine's tools. See attacca.py.

    Not a model: there is nothing here to send a conversation to. `model` is
    the name of the Attacca agent turns go to, and `stream` reads the reply of
    a turn `attacca.run_turn` already started.
    """
    name = "attacca"
    label = "Attacca"
    key_env = ("ATTACCA_CREDENTIAL",)
    key_help = "/connect attacca pairs this machine with a code - no key to paste"
    default_base_url = "wss://attacca.cc/api/zyris/v1/ws"
    drives_turns = True
    pairs = True

    @property
    def base_url(self) -> str:
        # The server is chosen the way the key is: environment first.
        return (os.environ.get("ATTACCA_SERVER_URL", "").strip()
                or super().base_url)

    def sees_images(self) -> bool:
        return False

    def ready(self) -> str:
        if not self.api_key:
            return "not paired (/connect attacca)"
        if not self.model:
            return "no agent chosen"
        return ""

    def list_models(self) -> list:
        from aetheris import attacca
        agents = attacca.link().call("attacca_api.list_agents", {}) or []
        return [{"name": agent.get("name") or agent.get("id", ""),
                 "detail": agent.get("model") or agent.get("description") or ""}
                for agent in agents if agent.get("id")]

    def stream(self, messages: list, max_tokens: int | None = None, **_):
        from aetheris import attacca
        return attacca.stream_segment()


PROVIDERS = {p.name: p for p in
             (OllamaProvider, AnthropicProvider, OpenAIProvider, GeminiProvider,
              AttaccaProvider)}


# ---------------------------------------------------------------------------
# what is connected
# ---------------------------------------------------------------------------

_state: dict = {}
_active: Provider | None = None


def load_state(force: bool = False) -> dict:
    global _state
    if _state and not force:
        return _state
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("active", "ollama")
    data.setdefault("providers", {})
    if not isinstance(data["providers"], dict):
        data["providers"] = {}
    # A key kept in the keyring is read back into memory here, so everything
    # downstream goes on finding it in `settings["api_key"]` as it always has.
    # One kept there that cannot be read now (the package was removed, the
    # keyring is locked) leaves the provider keyless, and its marker in place
    # so the key comes back with the keyring.
    for name, settings in data["providers"].items():
        if (isinstance(settings, dict) and settings.get("key_store") == "keyring"
                and not settings.get("api_key")):
            key = keystore.get(name, CONFIG_PATH)
            if key:
                settings["api_key"] = key
    _state = data
    return _state


def _for_the_file(state: dict) -> dict:
    """`state` as it is written: each key moved to the keyring where it can be.

    The keyring first, the file only when the keyring will not take it. A key
    that has just been put in the file - because the keyring is gone, or was
    switched off - loses its marker, so the next load reads the file and not a
    stale keyring entry.
    """
    written = json.loads(json.dumps(state))
    for name, settings in written.get("providers", {}).items():
        if not isinstance(settings, dict):
            continue
        key = settings.get("api_key")
        if key and keystore.put(name, CONFIG_PATH, key):
            settings.pop("api_key")
            settings["key_store"] = "keyring"
        elif key:
            settings.pop("key_store", None)
        live = state["providers"].get(name)
        if isinstance(live, dict):
            if settings.get("key_store"):
                live["key_store"] = settings["key_store"]
            else:
                live.pop("key_store", None)
    return written


def save_state() -> str:
    """Write the config, its keys in the keyring where there is one.

    Owner-only either way: a key the keyring would not take is in this file.
    """
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    # `private` keeps the key owner-only for the whole write. Creating the file
    # and chmodding it afterwards leaves a moment where the key is readable.
    atomic.write_json(CONFIG_PATH, _for_the_file(load_state()), private=True)
    return CONFIG_PATH


def key_home() -> str:
    """Where a pasted key goes, in words - for the prompts that ask for one."""
    kept_in = keystore.backend_name()
    return kept_in if kept_in else f"{CONFIG_PATH} (owner-only)"


def has_saved_key(name: str) -> bool:
    settings = load_state()["providers"].get(name)
    return isinstance(settings, dict) and bool(settings.get("api_key")
                                               or settings.get("key_store"))


def key_variables() -> list:
    """Every environment variable a provider reads its key from."""
    return [variable for factory in PROVIDERS.values() for variable in factory.key_env]


def credentials() -> dict:
    """Every credential this harness holds, as {label: value}.

    For `vault.redact`, which keeps them out of anything a model is shown. The
    environment counts as much as the file: `env` prints a key from either.
    """
    found = {}
    for variable in key_variables():
        value = os.environ.get(variable, "").strip()
        if value:
            found.setdefault(value, f"${variable}")
    for name, settings in load_state()["providers"].items():
        if isinstance(settings, dict):
            value = str(settings.get("api_key") or "").strip()
            if value:
                found.setdefault(value, f"{name} key")
    return {label: value for value, label in found.items()}


def child_environment() -> dict:
    """`os.environ` without the provider keys - for what a model gets to run.

    A command the model runs inherits this process's environment, and `env`
    or `os.environ` would hand it every key set there. The output is redacted
    anyway; this is so there is nothing in it to redact, including in a form
    the redaction cannot recognise - base64, reversed, split across lines.
    A project that needs a key in its commands keeps it in `.env`, where the
    vault fills it in by name.
    """
    environment = dict(os.environ)
    for variable in key_variables():
        environment.pop(variable, None)
    return environment


@contextlib.contextmanager
def using_model(model: str):
    """Run a block against a different model on the current provider.

    Both places a model can be named have to move together: the provider's own
    saved setting, which wins, and `config.MODEL`, which the display and the
    session file read. Restoring is in a `finally` because a sub-agent that
    raises must not leave the assistant talking to the wrong model.
    """
    from aetheris import config
    provider = current()
    settings = settings_for(provider.name)
    had = "model" in settings
    previous_setting = settings.get("model")
    previous_config = config.MODEL
    try:
        if model:
            settings["model"] = model
            config.MODEL = model
        yield current()
    finally:
        if model:
            if had:
                settings["model"] = previous_setting
            else:
                settings.pop("model", None)
            config.MODEL = previous_config
            _active_cache_clear()


def _active_cache_clear() -> None:
    global _active
    _active = None


def settings_for(name: str) -> dict:
    return load_state()["providers"].setdefault(name, {})


def build(name: str) -> Provider | None:
    factory = PROVIDERS.get(name)
    return factory(settings_for(name)) if factory else None


def forget_key(name: str) -> tuple:
    """Delete a provider's saved API key. Returns (removed, detail).

    A key could be typed in and never taken back out: `_ensure_key` only asks
    when there is none, so a key pasted into the wrong provider, or one that
    has since been revoked, stayed in `providers.json` with nothing in the
    program able to remove it.

    Only the key goes. The model and base_url beside it are not secrets and
    are what makes reconnecting one step instead of three. An environment
    variable is not touched either - it is not ours to unset, and saying so is
    more use than quietly appearing to have done something.
    """
    if name not in PROVIDERS:
        return False, f"unknown provider '{name}' - try {', '.join(PROVIDERS)}"
    settings = settings_for(name)
    had_key = bool(settings.pop("api_key", None))
    if settings.pop("key_store", None):
        keystore.delete(name, CONFIG_PATH)
        had_key = True
    if not had_key:
        return False, ""
    save_state()
    # The live provider was built around the dict that just changed, but a
    # rebuild is what guarantees it rather than what happens to be true.
    _active_cache_clear()
    return True, CONFIG_PATH


def current() -> Provider:
    """The provider in use. Falls back to Ollama, which needs no account."""
    global _active
    if _active is None:
        _active = build(load_state().get("active", "ollama")) or OllamaProvider({})
    return _active


def connect(name: str, model: str = "", api_key: str = "",
            base_url: str = "") -> tuple:
    """Point the harness at a provider. Returns (provider, problem)."""
    global _active
    if name not in PROVIDERS:
        return None, f"unknown provider '{name}' - try {', '.join(PROVIDERS)}"

    settings = settings_for(name)
    if api_key:
        settings["api_key"] = api_key
    if base_url:
        settings["base_url"] = base_url
    if model:
        settings["model"] = model

    provider = build(name)
    load_state()["active"] = name
    _active = provider
    _sync_config(provider)
    save_state()
    return provider, provider.ready()


def _sync_config(provider: Provider) -> None:
    """Keep `config.MODEL` in step - the TUI and session files both read it."""
    try:
        from aetheris import config
        config.MODEL = provider.model or config.MODEL
    except Exception:
        pass


async def complete(messages: list, max_tokens: int) -> str:
    """One short non-streamed answer - used for session titles and summaries."""
    provider = current()
    if provider.drives_turns:
        # There is no model behind it to ask a side question of: everything
        # goes through the agent's session, which keeps its own context.
        raise RuntimeError(f"{provider.label} runs its own agent loop and cannot "
                           f"answer a one-off request like this one.")
    pieces = []
    async for chunk in current().stream(messages, max_tokens=max_tokens):
        if chunk.get("text"):
            pieces.append(chunk["text"])
    return "".join(pieces).strip()


def status_line() -> str:
    provider = current()
    problem = provider.ready()
    if problem:
        return f"{provider.label} ({problem})"
    where = f" · key {provider.key_source}" if provider.key_source else ""
    return f"{provider.label} · {provider.model}{where}"


def apply_startup() -> None:
    """Restore the last connection when the app starts.

    A key still in plaintext in `providers.json` moves into the keyring now
    that there is one, rather than waiting for the next /connect to do it.
    """
    state = load_state()
    if keystore.available() and any(
            isinstance(settings, dict) and settings.get("api_key")
            and settings.get("key_store") != "keyring"
            for settings in state["providers"].values()):
        try:
            save_state()
        except OSError:
            pass        # still in the file, still owner-only; tried again next start
    _sync_config(current())


if __name__ == "__main__":
    print("This file can not run directly.")
