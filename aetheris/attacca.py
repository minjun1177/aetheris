"""Attacca - an agent hosted elsewhere, working through this machine's tools.

Every other provider is a model: the harness sends the conversation and gets
words and tool calls back, and runs the loop itself. Attacca is not a model.
It runs the loop on its own servers - its own prompt, its own context, its own
model - and reaches back into this machine over Zyris (`zyris.py`) whenever
it wants a file read or a command run. So connecting to it changes who drives
a turn, not just who answers it.

What stays the harness's own is everything that touches this machine. A call
from Attacca's agent is run by `tools.dispatch_tool`, exactly as a call from a
local model is: the permission rules, the approval prompts, the vault, the
undo log and the file claims on the agent channel all see it the same way. The
agent is somewhere else; the guard rails are not.

Three parts:

- **Pairing** (`pair`). RFC 8628 device flow: an eight-character code is
  shown here and typed into Attacca on any device with a browser. What comes
  back is a long-lived `zc_` credential, kept in `providers.json` like an API
  key and asked for with the narrowest scopes that work.
- **The link** (`link`). One `zyris.Link`, announcing this machine's tool
  table as the capability `aetheris`.
- **The turn** (`run_turn`). The person's line goes to an Attacca session; the
  reply streams back through `llm_client.stream_reply`, so it is drawn the
  way every reply is drawn, and stops wherever the agent wants a tool run.

`pip install "aetheris[attacca]"` - none of this is imported until Attacca is
the provider.
"""
import asyncio
import os
import platform
import queue
import socket
import threading
import time

from aetheris import config, providers, zyris
from aetheris.tui import S

NAME = "attacca"
PROGRAM = "aetheris"          # what the credential is issued to, fixed on it
CAPABILITY = "aetheris"       # what the agent calls: aetheris.read_file, ...

# Least privilege. zyris-code asks for every scope there is; this asks for what
# a turn uses - read the agent list, open a session, post to it, watch it.
# Jobs, works, files and peers are not touched, so they are not asked for.
SCOPES = ("agents:read", "sessions:read", "sessions:write", "events:read")

# Tools that exist for the harness's *own* loop and mean nothing to an agent
# that runs one of its own. `spawn_agent` starts a sub-agent on the local model;
# `view_image` hangs a picture on the local model's next message.
NOT_OFFERED = ("spawn_agent", "view_image")

# Every tool answers with text, so every tool is described as answering with
# text. One shape the far end can read without knowing anything about us.
RESPONSE_SCHEMA = {"type": "object",
                   "properties": {"output": {"type": "string"}},
                   "required": ["output"]}

# Attacca measures a result as JSON and refuses one over 1,000,000 bytes
# (`ZYRIS_MAX_RESULT_BYTES`, spec §6). Kept well under it, with the cut said in
# the result itself so the agent knows there was more.
RESULT_BYTES = 900_000

POLL_FLOOR, POLL_CEILING, SLOW_DOWN_STEP = 1, 60, 5
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
IDLE_POLL = 0.2               # how often the prompt looks for a call from Attacca


class NotPaired(RuntimeError):
    """No credential: `/connect attacca` pairs this machine."""


# ---------------------------------------------------------------------------
# pairing
# ---------------------------------------------------------------------------

def http_base(ws_url: str) -> str:
    """The device endpoints' base, derived from the websocket URL (spec §7).

    Derived rather than configured, so this machine cannot be paired with one
    deployment and then connect to another: the two can only ever agree.
    """
    head = ws_url.split("/zyris/", 1)[0]
    if head.startswith("wss://"):
        return "https://" + head[len("wss://"):]
    if head.startswith("ws://"):
        return "http://" + head[len("ws://"):]
    return head


def machine_name() -> str:
    """What this machine calls itself. The approval screen preselects it."""
    return (socket.gethostname() or platform.node() or "computer").split(".")[0]


def _post(url: str, body: dict):
    return providers._requests().post(url, json=body, timeout=30)


def _error_of(response) -> dict:
    try:
        body = response.json()
    except ValueError:
        body = {}
    return body if isinstance(body, dict) else {}


def authorize(ws_url: str) -> dict:
    """Ask for a code. Returns device_code, user_code, verification_uri, ..."""
    response = _post(http_base(ws_url) + "/zyris/v1/device/authorize", {
        "program": PROGRAM,
        "system_hint": machine_name(),
        "platform": platform.system().lower() or "unknown",
        "scopes": list(SCOPES),
        "client_hint": {"hostname": machine_name(),
                        "os": platform.platform(terse=True),
                        "agent": f"aetheris/{_version()}"},
    })
    if response.status_code >= 400:
        detail = _error_of(response)
        raise RuntimeError(
            f"Attacca would not start pairing (HTTP {response.status_code}"
            + (f": {detail.get('error_description') or detail.get('error')}"
               if detail else "") + ")")
    return response.json()


def poll_once(ws_url: str, device_code: str) -> tuple:
    """One poll of the token endpoint, read into what to do next.

    Returns ("granted", token) | ("wait", seconds or None) | ("slow", seconds or
    None) | ("expired", None) | ("denied", reason). `authorization_pending` is
    the normal answer for almost every poll and is not an error; a 5xx or 429
    means the server could not judge the request, which is also a "wait".
    """
    response = _post(http_base(ws_url) + "/zyris/v1/device/token",
                     {"device_code": device_code, "grant_type": GRANT_TYPE})
    if response.status_code < 400:
        return "granted", response.json()
    body = _error_of(response)
    error = body.get("error") or ""
    interval = body.get("interval")
    if response.status_code == 429 or response.status_code >= 500:
        return "wait", interval
    if error == "authorization_pending":
        return "wait", interval
    if error == "slow_down":
        return "slow", interval
    if error == "expired_token":
        return "expired", None
    return "denied", body.get("error_description") or error or f"HTTP {response.status_code}"


def _clamp(seconds) -> float:
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        seconds = POLL_FLOOR
    return max(POLL_FLOOR, min(POLL_CEILING, seconds))


def pair(provider) -> bool:
    """`/connect attacca` on a machine with no credential: show a code, wait.

    Ctrl+C cancels. A code that expires before anyone typed it is replaced by
    a fresh one rather than ending the attempt - walking to the other room
    for a laptop is not a reason to start over by hand.
    """
    from aetheris import qr
    url = provider.base_url
    print(f"\n  {S.BOLD}{S.ACCENT}Pair this machine with Attacca{S.R}")
    try:
        while True:
            grant = authorize(url)
            where, code = grant.get("verification_uri", ""), grant.get("user_code", "")
            print(f"  {S.GRAY}Open{S.R}  {S.WHITE}{where}{S.R}")
            print(f"  {S.GRAY}and enter{S.R}  {S.BOLD}{S.ACCENT}{code}{S.R}")
            if where and qr.fits(where):
                print(qr.render(where, colour=bool(S.R)))
            print(f"  {S.MUTED}Approve every scope it asks for: {', '.join(SCOPES)}.{S.R}")
            print(f"  {S.MUTED}⟳ waiting for approval… (Ctrl+C to cancel){S.R}")
            interval = _clamp(grant.get("interval"))
            deadline = time.time() + float(grant.get("expires_in") or 600)
            while time.time() < deadline:
                time.sleep(interval)
                outcome, value = poll_once(url, grant.get("device_code", ""))
                if outcome == "granted":
                    return _paired(provider, value)
                if outcome == "wait":
                    if value:
                        interval = _clamp(value)
                elif outcome == "slow":
                    interval = _clamp(value if value else interval + SLOW_DOWN_STEP)
                elif outcome == "expired":
                    break
                else:
                    print(f"  {S.ERR}✗ Pairing was refused: {value}{S.R}\n")
                    return False
            print(f"  {S.WARN}⚠ That code expired; here is a fresh one.{S.R}\n")
    except KeyboardInterrupt:
        print(f"\n  {S.GRAY}Pairing cancelled.{S.R}\n")
        return False
    except Exception as error:
        from aetheris.connect import _print_problem
        _print_problem("Could not pair with Attacca", error)
        return False


def _paired(provider, token: dict) -> bool:
    credential = str(token.get("credential") or "")
    if not credential:
        print(f"  {S.ERR}✗ Attacca approved the pairing but sent no credential.{S.R}\n")
        return False
    providers.settings_for(NAME)["api_key"] = credential
    # Saved now, not when a model is picked: a pairing is a trip to another
    # device, and cancelling the agent list afterwards should not undo it.
    providers.save_state()
    system = (token.get("system") or {}).get("name") or machine_name()
    owner = token.get("owner_email") or ""
    # The account it landed in is worth reading once: over SSH, against a URL
    # typed from memory, this line is the only chance to notice the wrong one.
    print(f"  {S.OK}✓ Paired: {system}/{PROGRAM}{S.R}"
          + (f" {S.MUTED}for {owner}{S.R}" if owner else ""))
    granted = set(token.get("scopes") or ())
    missing = [scope for scope in SCOPES if scope not in granted]
    if missing:
        # Scopes are fixed at approval. A grant without one of these fails
        # later as an empty list or a refused call, which looks like a bug.
        print(f"  {S.WARN}⚠ Not granted: {', '.join(missing)}. Turns may be "
              f"refused; /connect forget attacca and pair again to fix it.{S.R}")
    return True


def _version() -> str:
    from aetheris import __version__
    return __version__


# ---------------------------------------------------------------------------
# the link
# ---------------------------------------------------------------------------

_link: zyris.Link | None = None
_link_key: tuple = ()
_link_lock = threading.Lock()


def capability() -> dict:
    """This machine's tool table, as the agent will see it (spec §5).

    The same schemas native tool calling sends, so the agent is told about
    exactly the tools a local model is told about - MCP ones included -
    minus the two that only mean something to the local loop.
    """
    from aetheris import mcp_client, toolspec
    schemas = (toolspec.native_schema(exclude=NOT_OFFERED)
               + mcp_client.native_tool_schemas())
    return {"name": CAPABILITY, "version": 1, "tools": [
        {"name": s["name"], "description": s["description"], "transfer": "unary",
         "request_schema": s["input_schema"], "response_schema": RESPONSE_SCHEMA}
        for s in schemas]}


def offered() -> set:
    return {tool["name"] for tool in capability()["tools"]}


def link() -> zyris.Link:
    """The live link, dialling it first if there is none. Raises with the fix."""
    # One at a time: the background connect at startup and a first turn typed
    # straight after it would otherwise both dial, and announce, twice.
    with _link_lock:
        return _link_unlocked()


def connect_in_background() -> None:
    """Dial at startup, so an agent in the web app can reach this terminal
    before anyone here has typed. Off the main thread: an unreachable server
    must not hold up the prompt. A failure waits to be said at the first turn,
    where there is somebody to read it."""
    def dial():
        try:
            link()
        except Exception:
            pass
    threading.Thread(target=dial, name="aetheris-attacca-dial", daemon=True).start()


def _link_unlocked() -> zyris.Link:
    global _link, _link_key
    provider = providers.build(NAME)
    credential = provider.api_key
    if not credential:
        raise NotPaired("This machine is not paired with Attacca yet. "
                        "Run /connect attacca to pair it.")
    key = (provider.base_url, credential)
    if _link is not None and _link_key == key and _link.state != "unauthorized":
        # Possibly between two connections: the reader thread redials on its
        # own, so this waits for it rather than dialling a second link.
        if not _link.ready and not _link.wait_ready(zyris.DIAL_TIMEOUT * 2):
            raise zyris.ZyrisError("connection_lost", "Attacca is not answering; "
                                   "the link keeps trying in the background.",
                                   retriable=True)
        _ensure_announced(_link)
        return _link
    if _link is not None and _link.state == "unauthorized":
        disconnect()
        _refused(provider)
    disconnect()
    try:
        fresh = zyris.Link(provider.base_url, credential,
                           node_name=os.path.basename(os.getcwd()) or "aetheris").start()
    except zyris.Unauthorized:
        _refused(provider)
    _link, _link_key = fresh, key
    _ensure_announced(fresh)
    return fresh


def _refused(provider):
    """The credential is dead. Say so once, and do not keep a dead one around."""
    saved = providers.settings_for(NAME).get("api_key")
    if saved and not provider.key_source.startswith("$"):
        providers.forget_key(NAME)
    raise NotPaired("Attacca refused this machine's credential - it was revoked, "
                    "or its system was deleted. Run /connect attacca to pair again.")


def _ensure_announced(live: zyris.Link) -> None:
    """Announce again when the tool table moved - an MCP server came or went."""
    wanted = capability()
    current = live.capabilities[0] if live.capabilities else None
    if current != wanted:
        live.announce([wanted])


def disconnect() -> None:
    global _link, _link_key
    if _link is not None:
        _link.close()
    _link, _link_key = None, ()


def connected() -> bool:
    """Whether a link is up - which is when the idle prompt listens for calls."""
    return _link is not None and _link.ready


# ---------------------------------------------------------------------------
# serving a call
# ---------------------------------------------------------------------------

def _fit(text: str) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= RESULT_BYTES:
        return text
    kept = raw[:RESULT_BYTES].decode("utf-8", errors="ignore")
    return (kept + f"\n\n[Cut: the full result was {len(raw):,} bytes and only "
            f"the first {RESULT_BYTES:,} fit in one reply. Ask for less - a line "
            f"range, a narrower search.]")


def serve(request: zyris.Request, from_outside: bool = False) -> None:
    """Run one call from Attacca's agent the way a local model's call is run.

    `from_outside` is a call that arrived while nobody here had asked anything
    - an agent in the web app reaching this terminal. It runs under the same
    rules and the same prompts; the header only says where it came from, so a
    permission prompt is never a surprise without an explanation.
    """
    from aetheris import images, tools
    if request.capability != CAPABILITY or request.tool not in offered():
        request.respond(error=zyris.ZyrisError(
            "method_not_found", f"{request.method} is not offered by this machine"))
        return
    if from_outside:
        print(f"\n  {S.ACCENT}◆{S.R} {S.GRAY}an Attacca agent called "
              f"{S.WHITE}{request.method}{S.GRAY} (not from this terminal){S.R}")
    try:
        result = tools.dispatch_tool(request.tool, dict(request.params))
    except KeyboardInterrupt:
        # The person stopping it is an answer the agent should get, not a
        # silence it has to time out on.
        request.respond(error=zyris.ZyrisError(
            "canceled", "the person at this machine stopped the call"))
        raise
    finally:
        # A picture left by a tool must not ride along on the next local turn.
        images.forget_pending()
    if result is None:
        result = f"[Error] There is no tool named '{request.tool}'."
    request.respond({"output": _fit(str(result))})


def call_waiting() -> bool:
    """Whether a call from Attacca is queued. Looks without taking it."""
    return _link is not None and not _link.inbox.empty()


def serve_waiting() -> int:
    """Serve every call that arrived while the prompt was open. Returns how many."""
    served = 0
    while True:
        request = zyris.take_request(_link)
        if request is None:
            return served
        serve(request, from_outside=True)
        served += 1


# ---------------------------------------------------------------------------
# the turn
# ---------------------------------------------------------------------------

def session_id() -> str:
    return str(getattr(config, "ATTACCA_SESSION", "") or "")


def forget_session() -> None:
    """A new conversation here is a new session there."""
    config.ATTACCA_SESSION = ""


def _agent_id(live: zyris.Link, provider) -> str:
    """The chosen agent, found by name - and no quiet fallback to another.

    A status line showing one agent while turns go to a different one is the
    hardest kind of wrong to notice, so a name that no longer exists is an
    error that names it.
    """
    wanted = provider.model
    agents = live.call("attacca_api.list_agents", {}) or []
    for agent in agents:
        if agent.get("name") == wanted or agent.get("id") == wanted:
            return agent["id"]
    names = ", ".join(a.get("name", "?") for a in agents) or "none"
    raise RuntimeError(f"Attacca has no agent called '{wanted}' (it has: {names}). "
                       f"Pick one with /connect attacca.")


def _preamble(live: zyris.Link) -> str:
    return (f"You are working on a person's computer through Aetheris, a terminal "
            f"harness, on the node `{live.address}`. Its tools are the "
            f"`{CAPABILITY}` capability, and they act on {platform.system()} in "
            f"{os.getcwd()} - relative paths start there. Every call goes "
            f"through that person's permission rules and may be refused; a "
            f"refusal says why, and asking again gets the same answer.")


def _ensure_session(live: zyris.Link, provider) -> str:
    if session_id():
        return session_id()
    created = live.call("attacca_api.create_session_with", {"session": {
        "agent_id": _agent_id(live, provider),
        # No title: Attacca names a session from its first message, in the
        # language it was written in, and a title given here would stop that.
        "preamble": _preamble(live)}})
    config.ATTACCA_SESSION = str((created or {}).get("id") or "")
    if not config.ATTACCA_SESSION:
        raise RuntimeError("Attacca created a session but did not say its id.")
    return config.ATTACCA_SESSION


class Turn:
    """One turn's state, shared by the segment reader and the turn loop."""

    def __init__(self, live: zyris.Link, session: str):
        self.link = live
        self.session = session
        self.stream: zyris.Stream | None = None
        self.cursor = None            # the last durable event seen, for resuming
        self.request: zyris.Request | None = None
        self.notices: list = []       # lines to print between segments
        self.finished = False
        # Set once this turn is known to have started. Until then a
        # `running: false` is the session as it was before the message went in
        # - attacca.cc sends exactly that as the stream opens - and taking it
        # for the end of the turn ends the turn before its first word.
        self.armed = False
        self.cancelled = False
        self.printed = 0              # characters of answer delivered, for cancel_turn
        self.final = ""               # the answer as Attacca stored it
        self.streamed = ""            # the answer as it arrived, if no final came
        self.stop = False

    def open(self) -> None:
        self.stream = self.link.open_stream(
            "attacca_api.turn_events",
            {"session_id": self.session, "after": self.cursor})


current_turn: Turn | None = None


def _summary(payload: dict) -> str:
    name = str(payload.get("name") or "tool")
    error = payload.get("error")
    if error:
        text = error if isinstance(error, str) else (error.get("message") if
                                                     isinstance(error, dict) else str(error))
        return f"  {S.WARN}◇ {name}{S.R} {S.MUTED}failed: {str(text)[:160]}{S.R}"
    return f"  {S.MUTED}◇ {name} (on Attacca){S.R}"


def _is_ours(name: str) -> bool:
    """A `tool_call` event for a call this machine served - already printed.

    The agent knows a node's tools as `zyris__<capability>_v<version>__<tool>`
    (read off attacca.cc's own events), not as the `capability.tool` method
    that reaches this end.
    """
    prefix = f"zyris__{CAPABILITY}_v"
    if name.startswith(prefix):
        return name.rpartition("__")[2] in offered()
    head, _, tail = name.rpartition(".")
    return head == CAPABILITY and tail in offered()


def _event(turn: Turn, event: dict) -> bool:
    """Fold one durable event into the turn. True ends the segment."""
    kind = event.get("kind")
    payload = event.get("payload") or {}
    if kind == "chat_user":
        # This message, as Attacca filed it: the turn is under way.
        turn.armed = True
        return False
    if kind == "chat_agent":
        turn.final = str(payload.get("content") or "")
        return False
    if kind == "tool_call":
        name = str(payload.get("name") or "")
        # Written when the call starts and rewritten when it returns: pending
        # until there is a result or an error (zyris-code's `event.rs`).
        settled = payload.get("result") is not None or payload.get("error") is not None
        if name == "question":
            args = payload.get("arguments") or {}
            asked = args.get("question") or args.get("text") or args
            turn.notices.append(f"  {S.ACCENT}?{S.R} {S.WHITE}{asked}{S.R}\n"
                                f"  {S.MUTED}Answer at the prompt.{S.R}")
            return True
        if settled and not _is_ours(name):
            turn.notices.append(_summary(payload))
            return True
        return False
    if kind == "subagent_update":
        summary = payload.get("summary") or ""
        status = payload.get("status") or ""
        if summary or status:
            turn.notices.append(f"  {S.MUTED}↳ sub-agent {status}: {summary}{S.R}")
            return True
    # chat_user, chat_system, recall, todo_change: the model's own bookkeeping.
    return False


def _segment(turn: Turn):
    """Chunks for `stream_reply`, until the agent wants something else.

    A segment ends at a tool call this machine has to run, at a line worth
    printing between replies, or at the end of the turn. It runs on a worker
    thread (`providers._as_stream`), so the spinner keeps turning.
    """
    started = False               # whether this segment has drawn any answer yet
    while not turn.stop:
        request = zyris.take_request(turn.link)
        if request is not None:
            turn.request = request
            yield {"done": True}
            return
        try:
            frame = turn.stream.get(timeout=0.05)
        except queue.Empty:
            continue
        except zyris.ZyrisError as error:
            if not error.retriable:
                raise
            # The connection dropped mid-turn. The turn itself is still running
            # on Attacca; pick its events up again from the last cursor (§3.4).
            if not turn.link.wait_ready(zyris.DIAL_TIMEOUT * 2):
                raise
            turn.open()
            continue
        if frame is None:             # the stream ended: so did the turn
            turn.finished = True
            yield {"done": True}
            return
        kind = frame.get("type")
        if kind == "delta":
            text = str(frame.get("text") or "")
            if frame.get("kind") == "reasoning":
                yield {"thinking": text}
            else:
                # Counted before anything is trimmed: `cancel_turn` cuts
                # Attacca's own copy of the answer, which still has them.
                turn.printed += len(text)
                if not started:
                    # Agents on attacca.cc open a reply with "\n\n" - live
                    # check #2 - which would draw as blank lines under the
                    # tool call that came before it.
                    text = text.lstrip("\n")
                    if not text:
                        continue
                    started = True
                turn.streamed += text
                yield {"text": text}
        elif kind == "event":
            turn.cursor = frame.get("cursor", turn.cursor)
            if _event(turn, frame.get("event") or {}):
                yield {"done": True}
                return
        elif kind == "cancelled":
            turn.cancelled = True
        elif kind == "status":
            # The end of a turn is the edge from running to not running, the
            # way zyris-code reads it - never a `false` on its own.
            if frame.get("running"):
                turn.armed = True
            elif turn.armed:
                turn.finished = True
                yield {"done": True}
                return


def stream_segment():
    """What `AttaccaProvider.stream` hands `stream_reply`."""
    if current_turn is None:
        raise RuntimeError("no Attacca turn is running")
    return providers._as_stream(lambda: _segment(current_turn))


async def run_turn(messages: list) -> str:
    """Send the last user message to Attacca and see the turn through.

    `messages` already ends with the person's line (app.py appends it, then
    saves the session, before any provider is asked). The answer is appended
    the same way a local answer is, so a saved session reads back identically.
    """
    global current_turn
    provider = providers.build(NAME)
    live = link()
    session = _ensure_session(live, provider)
    user_line = str(messages[-1].get("content") or "") if messages else ""

    turn = Turn(live, session)
    # Opened before the message is sent: `after: None` means live frames only,
    # so a stream opened afterwards could miss the start of a fast reply.
    turn.open()
    current_turn = turn
    try:
        if (turn.stream.head or {}).get("running"):
            # Outside the cancel below on purpose: Ctrl+C while waiting on a
            # turn somebody else started must not cancel *their* turn.
            await _wait_for_quiet(turn)
        await _drive(turn, messages, user_line)
    finally:
        turn.stop = True
        current_turn = None
        if turn.stream is not None:
            turn.stream.cancel()

    answer = (turn.final or turn.streamed).strip()
    if turn.cancelled and not answer:
        answer = "[The turn was stopped on Attacca before it answered.]"
    messages.append({"role": "assistant", "content": answer})
    _report_usage(live, session)
    return answer


async def _wait_for_quiet(turn: Turn) -> None:
    """Let a turn already running on the session finish before sending this one.

    zyris-code queues the same way. Posting into a running turn races its end,
    and that turn's `running: false` would be read as the end of this one. Its
    calls into this machine are served meanwhile - nobody here asked for them,
    so they are served as calls from outside.
    """
    print(f"  {S.MUTED}⟳ a turn is already running on this Attacca session; "
          f"waiting for it to finish…{S.R}")
    while True:
        request = zyris.take_request(turn.link)
        if request is not None:
            serve(request, from_outside=True)
            continue
        try:
            frame = turn.stream.get(timeout=0)
        except queue.Empty:
            await asyncio.sleep(IDLE_POLL)
            continue
        if frame is None or (frame.get("type") == "status" and not frame.get("running")):
            return


async def _drive(turn: Turn, messages: list, user_line: str) -> None:
    """Send the line, then stream segments and serve calls until the turn ends."""
    from aetheris import llm_client
    live, session = turn.link, turn.session
    try:
        live.call("attacca_api.send_message",
                  {"session_id": session, "message": user_line, "data": []})
        while True:
            turn.request = None
            await llm_client.stream_reply(messages)
            for line in turn.notices:
                print(line)
            turn.notices.clear()
            if turn.request is not None:
                serve(turn.request)
                continue
            if turn.finished:
                break
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Ctrl+C arrives as either: `asyncio.run` turns it into a cancellation
        # of whatever is being awaited, and it stays a KeyboardInterrupt while
        # something synchronous - an approval prompt - holds the thread. Both
        # mean the person stopped, and the agent must not run on without them,
        # calling tools on a machine that is about to stop answering.
        turn.stop = True
        try:
            # Keep only what the person actually saw - the stored answer is cut
            # to the characters delivered, so the next turn does not build on
            # text nobody read.
            live.call("attacca_api.cancel_turn", {
                "session_id": session,
                "delivered": {"cursor": None, "chars": turn.printed}}, timeout=5)
        except Exception:
            pass
        raise


def _report_usage(live: zyris.Link, session: str) -> None:
    """What the session has cost, once per turn - Attacca counts per session."""
    try:
        usage = live.call("attacca_api.session_usage", {"session_id": session},
                          timeout=5) or {}
    except Exception:
        return            # a deployment that does not meter, or a slow one
    # `ZUsage` (zyris-attacca): every field optional, credits a string because
    # the unit and precision are the deployment's business, not ours.
    parts = []
    if usage.get("model"):
        parts.append(str(usage["model"]))
    if usage.get("context_tokens"):
        parts.append(f"{int(usage['context_tokens']):,} tokens in context")
    if usage.get("credits_used"):
        # Shown exactly as sent: attacca.cc sends a share of the plan ("0.42%"),
        # and another deployment may send something else entirely.
        parts.append(f"credits used {usage['credits_used']}")
    if parts:
        print(f"\n  {S.MUTED}─ Attacca · {' · '.join(parts)}{S.R}\n")


if __name__ == "__main__":
    print("This file can not run directly.")
