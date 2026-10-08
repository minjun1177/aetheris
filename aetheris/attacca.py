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

# How long a closing window gives Attacca to hear that the turn is over.
# Past it the window closes regardless.
CLOSING_GRACE = 3

# The project's own instructions, in the session preamble, are cut to this:
# a CLAUDE.md written for a person can be long, and it is sent once per session.
PREAMBLE_CONVENTIONS = 24_000

# How long to keep listening after a turn ends for the titles still owed.
# attacca.cc titles a reasoning block, and heads a stretch of work, by
# rewriting the event once a side model has read it - live check #4 saw the
# last block's title land 0.35s after `running: false`. A turn that ended on
# the end of the turn would never show its last title, and a short turn none.
LATE_TITLES = 2.0

# What goes back to Attacca's `question` tool for a step skipped, a question
# refused and a typed answer.
TYPED_MARK = "Typed:"
NOT_ANSWERING = "I won't answer this question."
ALL_SKIPPED = "Skipped them all."


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
    """What the agent is told once, when the session is made.

    Where it is, and then what a local model here would have been told about
    this project: its own instructions file, the skills on disk and the `.env`
    names. Attacca's agent never sees the local system prompt, so without
    these it worked blind to a project's conventions, never reached for a
    skill it was not told existed, and read `{{env:NAME}}` as a broken file.
    Fixed for the session's life, as the session's own preamble is - a changed
    CLAUDE.md is picked up by /clear.
    """
    from aetheris import skills, systemprompt, vault
    parts = [f"You are working on a person's computer through Aetheris, a terminal "
             f"harness, on the node `{live.address}`. Its tools are the "
             f"`{CAPABILITY}` capability, and they act on {platform.system()} in "
             f"{os.getcwd()} - relative paths start there. Every call goes "
             f"through that person's permission rules and may be refused; a "
             f"refusal says why, and asking again gets the same answer."]
    for section in (systemprompt.load_context_file, skills.skills_catalog_prompt,
                    vault.prompt_section):
        try:
            text = section().strip()
        except Exception:
            text = ""              # a file that will not read is not worth a failed session
        if len(text) > PREAMBLE_CONVENTIONS:
            text = text[:PREAMBLE_CONVENTIONS] + "\n\n... (cut here - the rest was too long)"
        if text:
            parts.append(text)
    return "\n\n".join(parts)


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
        # Attacca writes an event and then rewrites it in place - a thinking
        # block gets its title later, a tool call its result, a work its
        # heading - and each rewrite arrives again. What was last shown for
        # each event, so a rewrite that changed nothing on screen draws nothing.
        self.shown: dict = {}
        self.question = None          # steps of a question waiting on the person
        self.untitled: set = set()    # thinking blocks and works whose title is still owed
        self.closed_by = 0            # the signal that closed the window, if one did

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


def _tool_name(name: str) -> str:
    """`question` from `question` or from `zyris__<node>__<capability>__question`.

    Attacca's own tools arrive unprefixed and a node's arrive prefixed, and
    matching on the tail keeps this from depending on which a deployment sends.
    """
    return name.rpartition("__")[2]


def questions(arguments) -> list:
    """The steps of a `question` call: `{"questions": [{question, options, ...}]}`."""
    raw = arguments.get("questions") if isinstance(arguments, dict) else None
    steps = []
    for item in raw if isinstance(raw, list) else ():
        if not isinstance(item, dict) or not isinstance(item.get("question"), str):
            continue
        options = [{"label": str(o["label"]), "description": str(o.get("description") or "")}
                   for o in item.get("options") or () if isinstance(o, dict) and o.get("label")]
        steps.append({"header": str(item.get("header") or ""), "question": item["question"],
                      "multi": bool(item.get("multiSelect")), "options": options})
    return steps


def _answered(payload: dict) -> bool:
    """Whether a `question` call's answer arrived - not whether the call returned.

    Attacca's waiter returns `status: "timeout"` as an ordinary success when
    nobody replied in time, and `run_in_background` returns at once, and in
    both the answer is still wanted. A call that failed will take none.
    """
    if payload.get("error") is not None:
        return True
    result = payload.get("result")
    if not isinstance(result, dict):
        return False
    if "status" in result:
        return result.get("status") == "answered"
    return result.get("answer") is not None


def _report(payload: dict) -> tuple:
    """(ok, summary) of a `report_result` call, or None when it says nothing."""
    args = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
    summary = args.get("summary")
    if not isinstance(summary, str):
        summary = payload.get("result") if isinstance(payload.get("result"), str) else ""
    summary = summary.strip()
    if not summary:
        return None
    status = args.get("status")
    ok = status == "success" if isinstance(status, str) else payload.get("error") is None
    return ok, summary


def _once(turn: Turn, key, value) -> bool:
    """True the first time `value` is seen for `key` - so a rewrite draws once."""
    if turn.shown.get(key) == value:
        return False
    turn.shown[key] = value
    return True


def _event(turn: Turn, event: dict) -> bool:
    """Fold one durable event into the turn. True ends the segment."""
    kind = event.get("kind")
    payload = event.get("payload") or {}
    key = event.get("id") or event.get("seq")
    if kind == "chat_user":
        # This message, as Attacca filed it: the turn is under way.
        turn.armed = True
        return False
    if kind == "chat_agent":
        turn.final = str(payload.get("content") or "")
        return False
    if kind == "thinking":
        # A small model on Attacca titles each reasoning block, and the title
        # lands by rewriting the event - after the block, or never if that
        # model is off. The reasoning itself already streamed as deltas.
        title = str(payload.get("title") or "").strip()
        if not title:
            turn.untitled.add(("thinking", key))
            return False
        turn.untitled.discard(("thinking", key))
        if _once(turn, ("thinking", key), title):
            turn.notices.append(f"  {S.ACCENT}✻{S.R} {S.GRAY}{title}{S.R}")
            return True
        return False
    if kind == "work_summary":
        # The heading of a stretch of work, empty until decided and rewritten
        # as the work moves on ("retrying the node" → "writing the report").
        heading = str(payload.get("content") or "").strip()
        if not heading:
            turn.untitled.add(("work", key))
            return False
        turn.untitled.discard(("work", key))
        # A one-block stretch is headed with its block's title, word for word
        # (live check #4); the same line twice in a row says nothing new.
        if heading in turn.shown.values():
            turn.shown[("work", key)] = heading
            return False
        if _once(turn, ("work", key), heading):
            turn.notices.append(f"  {S.ACCENT}▾{S.R} {S.WHITE}{heading}{S.R}")
            return True
        return False
    if kind == "error":
        message = str(payload.get("message") or "").strip()
        if message and _once(turn, ("error", key), message):
            turn.notices.append(f"  {S.ERR}✗ Attacca: {message}{S.R}")
            return True
        return False
    if kind == "tool_call":
        name = str(payload.get("name") or "")
        tail = _tool_name(name)
        # Written when the call starts and rewritten when it returns: pending
        # until there is a result or an error (zyris-code's `event.rs`).
        settled = payload.get("result") is not None or payload.get("error") is not None
        if tail == "question":
            steps = questions(payload.get("arguments"))
            if not steps or _answered(payload) or not _once(turn, ("question", key), True):
                return False
            if not settled:
                # The agent is waiting on Attacca for the person's next message,
                # inside this turn. Answered here and now, between segments -
                # the prompt does not come back until the turn ends, and the
                # turn does not end until it is answered.
                turn.question = steps
            else:
                # Its waiter gave up or never waited: the answer is the next
                # message, typed at the prompt like any other.
                turn.notices.append(_question_text(steps) + f"\n  {S.MUTED}Answer at "
                                    f"the prompt - your next message is the answer.{S.R}")
            return True
        if tail == "report_result":
            report = _report(payload)
            if report and _once(turn, ("report", key), report):
                ok, summary = report
                mark = f"{S.OK}◆ done" if ok else f"{S.ERR}◆ did not work out"
                turn.notices.append(f"  {mark}{S.R} {S.WHITE}{summary}{S.R}")
                return True
            return False
        if settled and not _is_ours(name) and _once(turn, ("tool", key), True):
            turn.notices.append(_summary(payload))
            return True
        return False
    if kind == "subagent_update":
        summary = str(payload.get("summary") or "").strip()
        # Written when the delegation starts and rewritten when it ends.
        # Anything but the two endings is still running: saying "done" over
        # work that is going on is the one mistake worth avoiding here.
        status = {"completed": "done", "failed": "failed"}.get(payload.get("status"), "running")
        if summary and _once(turn, ("subagent", key), status):
            colour = S.ERR if status == "failed" else S.MUTED
            turn.notices.append(f"  {colour}↳ sub-agent {status}:{S.R} {S.GRAY}{summary}{S.R}")
            return True
        return False
    # chat_system, recall, todo_change: the model's own bookkeeping.
    return False


def _question_text(steps: list) -> str:
    lines = []
    for step in steps:
        head = f"[{step['header']}] " if step["header"] else ""
        lines.append(f"  {S.ACCENT}?{S.R} {S.WHITE}{head}{step['question']}{S.R}")
        for number, option in enumerate(step["options"], 1):
            more = f" {S.MUTED}- {option['description']}{S.R}" if option["description"] else ""
            lines.append(f"    {S.ACCENT}{number}{S.R} {option['label']}{more}")
    return "\n".join(lines)


def _picks(reply: str, step: dict) -> list:
    """What a reply chose: option numbers, an option's own label, or neither."""
    labels = [option["label"] for option in step["options"]]
    if reply in labels:
        return [labels.index(reply)]
    tokens = [t for t in reply.replace(",", " ").split() if t]
    if not tokens or not all(t.isdigit() and 1 <= int(t) <= len(labels) for t in tokens):
        return []
    chosen = sorted({int(t) - 1 for t in tokens})
    return chosen if step["multi"] or len(chosen) == 1 else []


def answer(steps: list):
    """Ask each step here, and say what was picked.

    The question is carried with the answer, and a picked option keeps its
    description, so the agent can tell what was answered from the answer
    alone. A typed answer is marked as typed - that it was none of the
    options is worth knowing too. None when nobody answered at all.
    """
    from aetheris import tui
    print(_question_text(steps))
    parts = []
    for step in steps:
        hint = ("numbers, or " if step["multi"] else "a number, or ") if step["options"] else ""
        reply = tui.ask_the_driver(
            "Attacca asks", [("question", step["question"])],
            [o["label"] for o in step["options"]],
            f"  {S.INFO}›{S.R} {S.MUTED}({hint}your own answer; blank skips){S.R} ",
            free_text=True)
        if reply is None:
            return None
        reply = reply.strip()
        if not reply:
            continue
        chosen = _picks(reply, step)
        if chosen:
            picked = [(f"{step['options'][i]['label']} ({step['options'][i]['description']})"
                       if step["options"][i]["description"] else step["options"][i]["label"])
                      for i in chosen]
        else:
            picked = [f"{TYPED_MARK} {reply}"]
        head = f"[{step['header']}] {step['question']}" if step["header"] else step["question"]
        body = ("\n".join(f"  - {p}" for p in picked) if len(picked) > 1
                else f"  {picked[0]}")
        parts.append(f"{head}\n{body}")
    return "\n\n".join(parts) if parts else ALL_SKIPPED


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
    watched = _watch_for_closing(turn)
    try:
        if (turn.stream.head or {}).get("running"):
            # Outside the cancel below on purpose: Ctrl+C while waiting on a
            # turn somebody else started must not cancel *their* turn.
            await _wait_for_quiet(turn)
        await _drive(turn, messages, user_line)
        await _late_titles(turn)
    except asyncio.CancelledError:
        if turn.closed_by:
            # The window is going. `_drive` has told Attacca already; a
            # traceback on the way out would be the last thing on the screen.
            raise SystemExit(128 + turn.closed_by) from None
        raise
    finally:
        _stop_watching(watched)
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


async def _late_titles(turn: Turn) -> None:
    """The titles that land after the turn has ended, drawn under its answer.

    Only while one is still owed, and never longer than `LATE_TITLES`: a
    deployment whose side model is off owes them for ever.
    """
    deadline = time.monotonic() + LATE_TITLES
    while turn.untitled and time.monotonic() < deadline and turn.stream is not None:
        try:
            frame = turn.stream.get(timeout=0)
        except queue.Empty:
            await asyncio.sleep(0.05)
            continue
        except zyris.ZyrisError:
            return
        if frame is None:
            return
        if frame.get("type") == "event":
            _event(turn, frame.get("event") or {})
            for line in turn.notices:
                print(line)
            turn.notices.clear()


def _watch_for_closing(turn: Turn) -> list:
    """Stop the turn on Attacca if the window closes in the middle of it.

    The turn runs there, not here. Left alone after the terminal is closed it
    goes on thinking, fails every call to a node that is no longer there, and
    spends credit doing it. SIGHUP (the terminal closed) and SIGTERM end the
    turn the way Ctrl+C does - cancelled on Attacca, with
    `CLOSING_GRACE` seconds to hear it - and then the program. POSIX only:
    Windows has neither signal, and closing its console kills the process
    outright.
    """
    import signal
    try:
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
    except RuntimeError:
        return []
    watched = []
    for name in ("SIGHUP", "SIGTERM"):
        number = getattr(signal, name, None)
        if number is None or task is None:
            continue

        def closing(number=number):
            turn.closed_by = int(number)
            task.cancel()
        try:
            loop.add_signal_handler(number, closing)
        except (NotImplementedError, RuntimeError, ValueError):
            continue
        watched.append((loop, number))
    return watched


def _stop_watching(watched: list) -> None:
    for loop, number in watched:
        try:
            loop.remove_signal_handler(number)
        except Exception:
            pass


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
            if turn.question is not None:
                steps, turn.question = turn.question, None
                reply = answer(steps)
                # Closing it quietly would leave the agent waiting on an answer
                # that never comes; saying so lets it carry on without one.
                live.call("attacca_api.send_message", {
                    "session_id": session, "message": reply or NOT_ANSWERING, "data": []})
                print(f"  {S.MUTED}⇢ answer sent{S.R}")
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
                "delivered": {"cursor": None, "chars": turn.printed}},
                timeout=CLOSING_GRACE if turn.closed_by else 5)
        except Exception:
            pass
        raise


def _usage(live: zyris.Link, session: str) -> dict:
    try:
        return live.call("attacca_api.session_usage", {"session_id": session},
                         timeout=5) or {}
    except Exception:
        return {}         # a deployment that does not meter, or a slow one


def _report_usage(live: zyris.Link, session: str) -> None:
    """What the session has cost, once per turn - Attacca counts per session."""
    usage = _usage(live, session)
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


def show_usage() -> None:
    """`/usage` while Attacca drives the turns: the session as Attacca counts it.

    Nothing here can count it. The stream carries no token counts, and what
    Attacca's model reads - its own prompt, the tool table, every tool result,
    every round of every turn - stays on Attacca; the message list here holds
    the questions and the final answers. The local graph and context estimate
    said "no data" and ~8,000 tokens of a session Attacca had metered at
    864,077.
    """
    print(f"\n  {S.BOLD}Attacca Session Usage{S.R}")
    session = session_id()
    if not session:
        print(f"  {S.GRAY}No Attacca session yet - it starts with the first message.{S.R}\n")
        return
    try:
        live = link()
    except Exception as error:
        print(f"  {S.ERR}{error}{S.R}\n")
        return
    usage = _usage(live, session)
    if not usage:
        print(f"  {S.GRAY}Attacca did not say what this session has used.{S.R}\n")
        return

    def count(key):
        try:
            return f"{int(usage[key]):,}"
        except (KeyError, TypeError, ValueError):
            return "-"
    if usage.get("model"):
        print(f"  {S.GRAY}model{S.R}    {S.WHITE}{usage['model']}{S.R}")
    if usage.get("context_tokens") is not None:
        print(f"  {S.GRAY}context{S.R}  {S.WHITE}{count('context_tokens')}{S.R} "
              f"{S.MUTED}tokens the agent reads with its next request{S.R}")
    if any(usage.get(key) is not None for key in
           ("input_tokens", "output_tokens", "total_tokens")):
        # Summed over every request: one question is a request after each tool
        # result, each re-reading the whole context, so input dwarfs context.
        print(f"  {S.GRAY}tokens{S.R}   {S.GRAY}input{S.R} {S.WHITE}{count('input_tokens')}{S.R}  "
              f"{S.GRAY}output{S.R} {S.WHITE}{count('output_tokens')}{S.R}  "
              f"{S.GRAY}total{S.R} {S.BOLD}{S.WHITE}{count('total_tokens')}{S.R}")
        print(f"  {S.MUTED}         every request of every turn, tool rounds included{S.R}")
    if usage.get("credits_used"):
        print(f"  {S.GRAY}credits{S.R}  {S.WHITE}{usage['credits_used']}{S.R} {S.MUTED}used{S.R}")
    print(f"  {S.MUTED}Counted by Attacca: the conversation, its prompt and the tool "
          f"results live there, not here.{S.R}\n")


if __name__ == "__main__":
    print("This file can not run directly.")
