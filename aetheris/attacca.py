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
import collections
import json
import os
import platform
import queue
import re
import socket
import threading
import time

from aetheris import config, providers, zyris
from aetheris.tui import S

NAME = "attacca"
PROGRAM = "aetheris"          # what the credential is issued to, fixed on it
CAPABILITY = "aetheris"       # what the agent calls: aetheris.read_file, ...

# Least privilege. zyris-code asks for every scope there is; this asks for what
# a turn uses - read the agent list, open a session, post to it, watch it - and
# for the one thing `/planmode` needs. Attacca's plan mode is entered by
# creating a job with `plan_mode` and in no other way a node has, so jobs are
# asked for to write. Works, files and peers are not touched, and not asked for.
PLAN_SCOPE = "jobs:write"
SCOPES = ("agents:read", "sessions:read", "sessions:write", "events:read", PLAN_SCOPE)

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

# A plan is decided by the message that follows it: Attacca reads the person's
# next message as the decision, so approving is sending, and anything else
# typed is what to change. These two are the messages for Enter and for nobody
# being there to press it.
PLAN_APPROVED = "Approved, go ahead with the plan."
PLAN_LEFT = "I am not deciding on this plan now - do not start on it."
APPROVALS = ("", "y", "yes", "ok", "approve", "approved")

# A turn somebody else started is over when the session stops running - and,
# for one read back out of the history, when nothing more has arrived for this
# long: history carries the events and no `running: false` to end them on.
FOLLOW_QUIET = 1.0
WATCH_RETRY = 5.0             # seconds before a watch that failed is opened again

# What a plan-mode job's opening message carries under the person's own line.
# A job is given no preamble of its own, so what a session is told once - where
# this machine is, the project's conventions - rides along here instead.
PLAN_CONTEXT = "[Where this was typed - context, not part of the request]"

# What goes in front of a message that follows `!commands`: they ran on this
# machine, and Attacca's copy of the conversation never saw them.
SHELL_MARK = "[Shell] $ "
SHELL_CONTEXT = "[Run by the person in their own terminal before this message]"

# How much of an artifact is shown under the call that made it; `/artifacts`
# shows the rest.
ARTIFACT_PREVIEW = 6

# Attacca names a session from its first message, with a side model, a moment
# after that message - so the first look is given this long to find it.
TITLE_TRIES, TITLE_PAUSE = 4, 0.6


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
    from aetheris import qr, tui
    url = provider.base_url
    print(f"\n  {S.BOLD}{S.ACCENT}Pair this machine with Attacca{S.R}")
    try:
        # Said to be cancellable, so it has to say it is listening: a Ctrl+C
        # in the middle of a slash command is otherwise put off, not raised.
        with tui.hearing_ctrl_c():
            return _pair(provider, url, qr)
    except KeyboardInterrupt:
        print(f"\n  {S.GRAY}Pairing cancelled.{S.R}\n")
        return False
    except Exception as error:
        from aetheris.connect import _print_problem
        _print_problem("Could not pair with Attacca", error)
        return False


def _pair(provider, url: str, qr) -> bool:
    """`pair`'s loop: a code, the wait for it, and a fresh one if it expires."""
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
    stop_watching_session()
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
    config.ATTACCA_CURSOR = 0
    config.ATTACCA_JOB = ""
    stop_watching_session()


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
    config.ATTACCA_CURSOR = 0
    # An untitled session reads back under a placeholder, not as untitled.
    # What it is called now is therefore the one name that is not its title.
    _placeholders[config.ATTACCA_SESSION] = str((created or {}).get("title") or "").strip()
    return config.ATTACCA_SESSION


_placeholders: dict = {}      # session id -> what it was called before it had a name
_title_waited: set = set()    # sessions whose title has been waited for once


def session_title(live: zyris.Link | None = None) -> str:
    """This session's name as Attacca gave it, or "" while it has none of its own."""
    session = session_id()
    if not session:
        return ""
    try:
        rows = (live or link()).call("attacca_api.list_sessions",
                                     {"filter": {"limit": 100}}, timeout=5) or []
    except Exception:
        return ""              # a title is not worth an error under an answer
    for row in rows:
        if isinstance(row, dict) and row.get("id") == session:
            title = str(row.get("title") or "").strip()
            return "" if title == _placeholders.get(session, "") else title
    return ""


async def title_when_named() -> str:
    """The session's title, waited for briefly the first time it is asked.

    Attacca writes the title; nothing here asks a model for one. It lands a
    moment after the first message, which is usually before that message's
    answer has finished - and when it is not, the next turn asks again, once
    and without waiting.
    """
    session = session_id()
    tries = 1 if session in _title_waited else TITLE_TRIES
    _title_waited.add(session)
    loop = asyncio.get_running_loop()
    for attempt in range(tries):
        title = await loop.run_in_executor(None, session_title)
        if title or session_id() != session:
            return title
        if attempt + 1 < tries:
            await asyncio.sleep(TITLE_PAUSE)
    return ""


_plan_scope_said = False      # that this pairing cannot plan was said already


def plans_here() -> bool:
    """Whether `/planmode` is Attacca's own plan mode on this pairing.

    It is when the pairing was granted `jobs:write`. One made before that was
    asked for was not, and scopes are fixed at approval - so that is said once,
    with the fix, and the plan is asked for the way it is asked of a local
    model: through this machine's `submit_plan_for_approval`.
    """
    global _plan_scope_said
    try:
        live = link()
    except Exception:
        return False              # the turn itself says what is wrong
    granted = account(live).get("scopes")
    if not isinstance(granted, list):
        return False              # a deployment that does not say: assume the old way
    if PLAN_SCOPE in granted:
        return True
    if not _plan_scope_said:
        _plan_scope_said = True
        print(f"  {S.WARN}◆ Attacca's own plan mode needs the {PLAN_SCOPE} scope, "
              f"which this pairing was not granted.{S.MUTED} /connect forget attacca "
              f"and pair again to get it; until then the plan is asked for through "
              f"this machine's submit_plan_for_approval.{S.R}")
    return False


def plan_anew() -> None:
    """`/planmode on`: the next message starts a plan of its own."""
    config.ATTACCA_JOB = ""


def _plan_job(live: zyris.Link, provider, line: str) -> str:
    """Start a plan-mode job for `line`. Returns its session, now this terminal's.

    A job runs on a session of its own, so what was said before it stays in
    the session before it - said here, because nothing else would say it.
    """
    moved = bool(session_id())
    job = live.call("attacca_api.create_job", {"job": {
        "message": f"{line}\n\n{PLAN_CONTEXT}\n{_preamble(live)}",
        "agent_id": _agent_id(live, provider),
        "plan_mode": True}}) or {}
    session = str(job.get("session_id") or "")
    if not session:
        raise RuntimeError("Attacca started a plan-mode job but did not say which "
                           "session runs it.")
    config.ATTACCA_SESSION, config.ATTACCA_CURSOR = session, 0
    config.ATTACCA_JOB = str(job.get("id") or session)
    # Titled in the background, like a session: what it is called now is the
    # one name that is not its title.
    _placeholders[session] = str(job.get("title") or "").strip()
    print(f"  {S.ACCENT}▤{S.R} {S.GRAY}plan mode: a job on Attacca that plans first "
          f"and waits for your decision{S.R}"
          + (f"{S.MUTED} - on a session of its own; what was said before this "
             f"stays in the earlier one{S.R}" if moved else ""))
    return session


def _with_shell(messages: list) -> str:
    """The person's line, under the `!commands` they ran since the last answer.

    A local model reads them in the conversation it is sent. Attacca keeps its
    own conversation and is sent one message, so they go in front of it - the
    ones not sent yet, which are the ones directly above the line.
    """
    line = str(messages[-1].get("content") or "") if messages else ""
    ran = []
    for message in reversed(messages[:-1]):
        content = message.get("content") if isinstance(message, dict) else None
        if (not isinstance(content, str) or message.get("role") != "user"
                or not content.startswith(SHELL_MARK)):
            break
        ran.insert(0, content[len("[Shell] "):])
    if not ran or not config.RETURN_SHELL_OUTPUT:
        return line
    return SHELL_CONTEXT + "\n" + "\n\n".join(ran) + "\n\n" + line


class Turn:
    """One turn's state, shared by the segment reader and the turn loop."""

    def __init__(self, live: zyris.Link, session: str, foreign: bool = False):
        self.link = live
        self.session = session
        # A turn nobody here started: a message typed into Attacca's web app,
        # followed here. It is drawn the same way and never sent, cancelled or
        # answered from this end.
        self.foreign = foreign
        self.stream: zyris.Stream | None = None
        self.backlog: collections.deque = collections.deque()   # frames read before it was a turn
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
        # The answer as Attacca stored it, a part per `chat_agent` event and in
        # their order: an agent that speaks, calls a tool and speaks again has
        # said two things, and keeping the last alone lost the first from the
        # saved conversation - and from the screen at the next redraw.
        self.said: dict = {}
        self.streamed = ""            # the answer as it arrived, if no final came
        self.unsaid = 0               # characters streamed since the last `chat_agent`
        self.sent: list = []          # what this end has sent in this turn
        self.user_seen = False        # its own message has come back as an event
        self.heard: list = []         # a foreign turn's exchanges: {"user", "said"}
        self.running = False          # the session's own flag, for a foreign turn
        self.last_frame = time.monotonic()
        self.parked = False           # a foreign turn is waiting on the person
        self.stop = False
        # Set whenever no segment is reading the stream. A segment runs on a
        # worker thread and notices `stop` only between two reads, so whoever
        # reads the stream next waits for this first - or shares it for a
        # moment with a reader that is on its way out, and loses a frame to it.
        self.released = threading.Event()
        self.released.set()
        # Attacca writes an event and then rewrites it in place - a thinking
        # block gets its title later, a tool call its result, a work its
        # heading - and each rewrite arrives again. What was last shown for
        # each event, so a rewrite that changed nothing on screen draws nothing.
        self.shown: dict = {}
        self.question = None          # steps of a question waiting on the person
        self.plan = None              # a plan waiting on the person's decision
        self.untitled: set = set()    # thinking blocks and works whose title is still owed
        self.closed_by = 0            # the signal that closed the window, if one did

    def open(self) -> None:
        self.stream = self.link.open_stream(
            "attacca_api.turn_events",
            {"session_id": self.session, "after": self.cursor})
        if self.foreign:
            _watch.stream = self.stream       # the watch goes on from this one

    @property
    def answer(self) -> str:
        """Everything the agent said this turn, as stored - or as it streamed."""
        return _joined(self.said) or self.streamed.strip()


def _joined(said: dict) -> str:
    return "\n\n".join(part.strip() for part in said.values() if part.strip())


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


def _answered(payload: dict, reply: str = "answer") -> bool:
    """Whether a `question` call's answer arrived - not whether the call returned.

    Attacca's waiter returns `status: "timeout"` as an ordinary success when
    nobody replied in time, and `run_in_background` returns at once, and in
    both the answer is still wanted. A call that failed will take none.

    `submit_plan` waits the same way and is read the same way, with its reply
    under `decision`.
    """
    if payload.get("error") is not None:
        return True
    result = payload.get("result")
    if not isinstance(result, dict):
        return False
    if "status" in result:
        return result.get("status") == "answered"
    return result.get(reply) is not None


def plan_of(arguments) -> str:
    """The plan a `submit_plan` call carries: `{"markdown": "..."}`. "" for none."""
    markdown = arguments.get("markdown") if isinstance(arguments, dict) else None
    return markdown.strip() if isinstance(markdown, str) else ""


def _plan_text(markdown: str) -> str:
    from aetheris.renderer import _render_full
    rule = f"  {S.MUTED}{'─' * max(8, min(config.tw() - 4, 60))}{S.R}"
    return (f"\n  {S.ACCENT}▤{S.R} {S.BOLD}{S.WHITE}Plan from Attacca{S.R}\n{rule}\n"
            f"{_render_full(markdown)}\n{rule}")


def _from_the_web(text: str) -> str:
    return (f"\n  {S.USER_CLR}{S.BOLD}❯{S.R} {text}\n"
            f"  {S.MUTED}↑ sent from Attacca's web app{S.R}")


_ours: set = set()            # `chat_user` events this terminal's own messages became


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
        # A message, as Attacca filed it: the turn is under way.
        text = str(payload.get("content") or "")
        # The first one of a turn started here is the message that started it,
        # whatever Attacca did to its spelling on the way in.
        mine = not turn.foreign and (text.strip() in turn.sent or not turn.user_seen)
        turn.armed = True
        if mine:
            turn.user_seen = True
            if text.strip() in turn.sent:
                turn.sent.remove(text.strip())
            _ours.add(key)
            return False
        # Typed somewhere else - the web app - into the session this terminal
        # is on. Shown as a line of the conversation, which is what it is.
        if not text.strip() or key in _ours or not _once(turn, ("user", key), text):
            return False
        turn.heard.append({"user": text, "said": {}})
        turn.unsaid = 0
        turn.notices.append(_from_the_web(text))
        return True
    if kind == "chat_agent":
        content = str(payload.get("content") or "")
        if turn.foreign and not turn.heard:
            turn.heard.append({"user": "", "said": {}})     # joined after its question
        (turn.heard[-1]["said"] if turn.foreign else turn.said)[key] = content
        streamed, turn.unsaid = turn.unsaid, 0
        # An answer that arrives whole was never drawn: one read back from the
        # history, or one whose deltas were lost with a dropped connection.
        if _once(turn, ("said", key), True) and content.strip() and not streamed:
            from aetheris.renderer import _render_full
            turn.notices.append(_render_full(content.strip()) + "\n")
            return True
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
            if not settled and not turn.foreign:
                # The agent is waiting on Attacca for the person's next message,
                # inside this turn. Answered here and now, between segments -
                # the prompt does not come back until the turn ends, and the
                # turn does not end until it is answered.
                turn.question = steps
            else:
                # Its waiter gave up or never waited: the answer is the next
                # message, typed at the prompt like any other. So is the answer
                # to a question asked in a turn that was started elsewhere -
                # asked here as well it would be asked twice, and answered in
                # the web app it would leave this terminal waiting on nobody.
                turn.parked = turn.parked or not settled
                turn.notices.append(_question_text(steps) + f"\n  {S.MUTED}Answer at "
                                    f"the prompt - your next message is the answer.{S.R}")
            return True
        if tail == "submit_plan":
            # Attacca's plan mode: the agent investigates, hands the whole plan
            # over as this call's argument and waits. There is no approve call -
            # the person's next message is the decision.
            markdown = plan_of(payload.get("arguments"))
            if (not markdown or _answered(payload, "decision")
                    or not _once(turn, ("plan", key), markdown)):
                return False
            if not settled and not turn.foreign:
                turn.plan = markdown
            else:
                turn.parked = turn.parked or not settled
                turn.notices.append(_plan_text(markdown) + f"\n  {S.MUTED}Decide at the "
                                    f"prompt - your next message is the decision: say "
                                    f"it is approved, or what to change.{S.R}")
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
            made = artifact_of(payload)
            turn.notices.append(_summary(payload) + (_artifact_note(made) if made else ""))
            if made:
                _made.setdefault(turn.session, {})[key] = made
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


def decide(markdown: str) -> str:
    """Show the plan and ask. Returns the message that is the decision.

    Enter approves it, as it stands. Anything typed is sent as written - what
    to change, or a question about it - and the agent revises and submits
    again. Nobody answering is said too, so the turn is not left parked.
    """
    from aetheris import tui
    print(_plan_text(markdown))
    opening = " ".join(markdown.split())[:160]
    reply = tui.ask_the_driver(
        "Attacca's plan", [("plan", opening)], [("approve", "Approve the plan")],
        f"  {S.INFO}›{S.R} {S.MUTED}(Enter approves; or say what to change){S.R} ",
        free_text=True)
    if reply is None:
        return PLAN_LEFT
    return PLAN_APPROVED if reply.strip().lower() in APPROVALS else reply.strip()


def _segment(turn: Turn):
    """Chunks for `stream_reply`, until the agent wants something else.

    A segment ends at a tool call this machine has to run, at a line worth
    printing between replies, or at the end of the turn. It runs on a worker
    thread (`providers._as_stream`), so the spinner keeps turning.
    """
    turn.released.clear()
    try:
        yield from _frames(turn)
    finally:
        turn.released.set()


def _frames(turn: Turn):
    """`_segment`'s reading: one frame at a time, until something ends it."""
    started = False               # whether this segment has drawn any answer yet
    while not turn.stop:
        request = zyris.take_request(turn.link)
        if request is not None:
            turn.request = request
            yield {"done": True}
            return
        try:
            frame = (turn.backlog.popleft() if turn.backlog
                     else turn.stream.get(timeout=0.05))
        except queue.Empty:
            if (turn.foreign and not turn.running
                    and time.monotonic() - turn.last_frame > FOLLOW_QUIET):
                turn.finished = True
                yield {"done": True}
                return
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
        turn.last_frame = time.monotonic()
        kind = frame.get("type")
        if kind == "delta":
            text = str(frame.get("text") or "")
            if turn.foreign:
                turn.armed = True     # words arriving: it is under way
            if frame.get("kind") == "reasoning":
                yield {"thinking": text}
            else:
                # Counted before anything is trimmed: `cancel_turn` cuts
                # Attacca's own copy of the answer, which still has them.
                turn.printed += len(text)
                turn.unsaid += len(text)
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
            turn.running = bool(frame.get("running"))
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
    user_line = _with_shell(messages)

    # One reader per session: the idle watch would hear this turn as well, and
    # show it a second time as somebody else's.
    stop_watching_session()
    # Plan mode is a job, and the job is how the message goes in. One per
    # stretch of conversation: what is typed after it goes to its session.
    planning = bool(config.PLANMODE and not config.ATTACCA_JOB and plans_here())
    session = (_plan_job(live, provider, user_line) if planning
               else _ensure_session(live, provider))
    waiting_on_us = session in _parked
    _parked.discard(session)
    turn = Turn(live, session)
    if planning:
        # Its turn began when it was created, so the stream is read from the
        # session's first event rather than from now.
        turn.cursor = 0
    # Opened before the message is sent: `after: None` means live frames only,
    # so a stream opened afterwards could miss the start of a fast reply.
    turn.open()
    current_turn = turn
    watched = _watch_for_closing(turn)
    try:
        if (turn.stream.head or {}).get("running") and not (waiting_on_us or planning):
            # Outside the cancel below on purpose: Ctrl+C while waiting on a
            # turn somebody else started must not cancel *their* turn.
            # Not waited for when that turn is parked on a question or a plan
            # shown here: this message is what it is waiting for.
            await _wait_for_quiet(turn)
        if planning and (turn.stream.head or {}).get("running"):
            turn.armed = turn.running = True      # under way before the stream opened
        await _drive(turn, messages, None if planning else user_line)
        await _late_titles(turn)
    except (KeyboardInterrupt, asyncio.CancelledError):
        if turn.closed_by:
            # The window is going. `_drive` has told Attacca already; a
            # traceback on the way out would be the last thing on the screen.
            raise SystemExit(128 + turn.closed_by) from None
        # Stopped with Ctrl+C, and the program goes on: what was said before
        # the stop is the answer the conversation keeps, as it is on Attacca.
        if turn.answer:
            messages.append({"role": "assistant", "content": turn.answer})
        raise
    finally:
        _stop_watching(watched)
        turn.stop = True
        turn.released.wait(1.0)       # so the cursor below is the last one read
        current_turn = None
        if turn.stream is not None:
            turn.stream.cancel()
        if turn.cursor:
            config.ATTACCA_CURSOR = turn.cursor

    answer = turn.answer
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


async def _drive(turn: Turn, messages: list, user_line) -> None:
    """Send the line, then stream segments and serve calls until the turn ends.

    `user_line` is None when it has gone already - as the message a plan-mode
    job was created with.
    """
    from aetheris import llm_client
    live, session = turn.link, turn.session

    def send(text: str) -> None:
        # Remembered, so the `chat_user` event it comes back as is known to be
        # this end's and not somebody typing in the web app mid-turn.
        turn.sent.append(text.strip())
        live.call("attacca_api.send_message",
                  {"session_id": session, "message": text, "data": []})
    try:
        if user_line is not None:
            send(user_line)
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
                send(reply or NOT_ANSWERING)
                print(f"  {S.MUTED}⇢ answer sent{S.R}")
            if turn.plan is not None:
                markdown, turn.plan = turn.plan, None
                decision = decide(markdown)
                send(decision)
                print(f"  {S.MUTED}⇢ " + ("plan approved" if decision == PLAN_APPROVED
                                          else "sent") + f"{S.R}")
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


# ---------------------------------------------------------------------------
# a turn started somewhere else
# ---------------------------------------------------------------------------

class _Watch:
    """The session's events while the prompt is open and nobody here has asked.

    A session belongs to an account, not to a terminal: the same one is open
    in Attacca's web app, and a message typed there starts a turn this machine
    serves the tools for. Unwatched, all that showed here was a tool call
    arriving from nowhere - the question it belonged to and the answer it led
    to were on another screen.
    """

    def __init__(self):
        self.session = ""
        self.link: zyris.Link | None = None
        self.stream: zyris.Stream | None = None
        self.frames: collections.deque = collections.deque()
        self.running = False          # the session's flag, as last heard
        self.active = False           # the frames held are a turn worth showing
        self.muted = False            # the person stopped following this turn
        self.retry_at = 0.0


_watch = _Watch()
_watch_closed = 0             # counts closings, so an opening can tell it was overtaken
_parked: set = set()          # sessions whose turn waits on a message from the person


def stop_watching_session() -> None:
    """Close the watch. The next open prompt starts one again, from the cursor."""
    global _watch, _watch_closed
    _watch_closed += 1
    stream, _watch = _watch.stream, _Watch()
    if stream is not None:
        try:
            stream.cancel()
        except Exception:
            pass


def _open_watch() -> None:
    """Start watching the current session, from the last event drawn here.

    Blocking - it is a call to Attacca - so `activity` runs it off the loop.
    """
    live, session = _link, session_id()
    stop_watching_session()
    opened_at = _watch_closed
    if live is None or not live.ready or not session or current_turn is not None:
        return
    try:
        stream = live.open_stream("attacca_api.turn_events", {
            "session_id": session, "after": config.ATTACCA_CURSOR or None}, timeout=10)
    except Exception:
        _watch.retry_at = time.monotonic() + WATCH_RETRY
        return
    if opened_at != _watch_closed or session != session_id() or current_turn is not None:
        # Overtaken while it was opening: a turn started here, or the
        # conversation changed. A watch left open now would hear that turn too
        # and draw it a second time, as somebody else's.
        stream.cancel()
        return
    _watch.session, _watch.link, _watch.stream = session, live, stream
    _watch.running = bool((stream.head or {}).get("running"))
    # Running already, and not on this terminal's account: somebody's turn.
    # Unless it is parked on a question shown here - then it is waiting, with
    # nothing more to draw until it is answered.
    _watch.active = _watch.running and session not in _parked


def _hear(frame: dict) -> None:
    """Sort one frame of an idle session: part of a turn to show, or nothing."""
    watch = _watch
    kind = frame.get("type")
    if kind == "status":
        watch.running = bool(frame.get("running"))
        if not watch.running:
            # The end of whatever was running. Kept only as the end of a turn
            # being shown; on its own it is the session saying it is idle.
            if watch.muted:
                watch.muted = False
                return
            if watch.active:
                watch.frames.append(frame)
            return
        if not watch.muted:
            watch.active = True
            watch.frames.append(frame)
        return
    if watch.muted:
        if kind == "event":
            config.ATTACCA_CURSOR = frame.get("cursor") or config.ATTACCA_CURSOR
        return
    if kind == "delta":
        watch.active = True
        watch.frames.append(frame)
        return
    if kind != "event":
        return
    event = frame.get("event") or {}
    key = event.get("id") or event.get("seq")
    theirs = (event.get("kind") == "chat_user" and key not in _ours
              and str((event.get("payload") or {}).get("content") or "").strip())
    if theirs:
        watch.active = True
    if watch.active:
        watch.frames.append(frame)
    else:
        # A rewrite of something already drawn - the title a reasoning block
        # is given after its turn ended. Nothing to show; only the place moved.
        config.ATTACCA_CURSOR = frame.get("cursor") or config.ATTACCA_CURSOR


def activity_now() -> bool:
    """Whether a turn started elsewhere has something to show. Never blocks."""
    watch = _watch
    if watch.stream is None or watch.session != session_id() or watch.link is not _link:
        return False
    while True:
        try:
            frame = watch.stream.get(timeout=0)
        except queue.Empty:
            break
        except zyris.ZyrisError:
            # The connection dropped, and every stream with it. Opened again
            # from the cursor once the link is back, so nothing is missed.
            stop_watching_session()
            _watch.retry_at = time.monotonic() + WATCH_RETRY
            return False
        if frame is None:
            stop_watching_session()
            _watch.retry_at = time.monotonic() + WATCH_RETRY
            return False
        if isinstance(frame, dict):
            _hear(frame)
    return watch.active and bool(watch.frames or watch.running)


def look() -> bool:
    """`activity`, for a prompt that cannot be raced: open, look once, return."""
    stale = (_watch.stream is None or _watch.session != session_id()
             or _watch.link is not _link)
    if stale and connected() and session_id() and time.monotonic() >= _watch.retry_at:
        _open_watch()
        # What was filed while this terminal was away is read back as the
        # stream opens - a moment after it opens, so the look is given one.
        deadline = time.monotonic() + 0.4
        while not activity_now() and time.monotonic() < deadline:
            time.sleep(0.02)
    return activity_now()


async def activity() -> bool:
    """Resolves once a turn started elsewhere has something to show here."""
    loop = asyncio.get_running_loop()
    while True:
        stale = (_watch.stream is None or _watch.session != session_id()
                 or _watch.link is not _link)
        if stale and connected() and session_id() and time.monotonic() >= _watch.retry_at:
            await loop.run_in_executor(None, _open_watch)
        if activity_now():
            return True
        await asyncio.sleep(IDLE_POLL)


async def follow(messages: list) -> bool:
    """Draw a turn that was started somewhere else, and keep what it said.

    The same loop as a turn started here, minus everything that is not this
    terminal's to do: nothing is sent, and stopping it with Ctrl+C stops the
    following, not the turn. Calls it makes into this machine are served as
    they come, under the same rules. Returns whether `messages` grew.
    """
    global current_turn
    from aetheris import llm_client
    watch = _watch
    if watch.stream is None or watch.link is None:
        return False
    turn = Turn(watch.link, watch.session, foreign=True)
    turn.stream, turn.backlog = watch.stream, watch.frames
    turn.cursor = config.ATTACCA_CURSOR or None
    turn.running = turn.armed = watch.running
    current_turn = turn
    stopped = False
    try:
        while True:
            turn.request = None
            await llm_client.stream_reply(messages)
            for line in turn.notices:
                print(line)
            turn.notices.clear()
            if turn.request is not None:
                serve(turn.request)
                continue
            if turn.finished or turn.parked:
                break
        if not turn.parked:
            await _late_titles(turn)
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Somebody else's turn: looking away from it is all Ctrl+C can mean.
        stopped = True
        raise
    finally:
        turn.stop = True
        turn.released.wait(1.0)       # the segment's reader, off the stream
        current_turn = None
        if turn.cursor:
            config.ATTACCA_CURSOR = turn.cursor
        if turn.parked:
            _parked.add(turn.session)
        if turn.stream is not None and turn.stream.finished:
            stop_watching_session()           # it ended under us; opened again when idle
        elif _watch.stream is turn.stream:
            # The watch carries on from where this left it. A turn that was
            # looked away from stays quiet until it ends.
            _watch.running = turn.running
            _watch.muted = stopped and turn.running
            if stopped:
                _watch.frames.clear()
            # Frames left over belong to the turn after this one, already begun.
            _watch.active = bool(_watch.frames)
        grew = _keep(turn, messages)
    if grew and not turn.parked:
        _report_usage(turn.link, turn.session)
    return grew


def _keep(turn: Turn, messages: list) -> bool:
    """Put a followed turn into the conversation, as the exchanges it was."""
    before = len(messages)
    heard = turn.heard or [{"user": "", "said": {}}]
    for index, exchange in enumerate(heard):
        if exchange["user"].strip():
            messages.append({"role": "user", "content": exchange["user"]})
        said = _joined(exchange["said"])
        if not said and index == len(heard) - 1:
            said = turn.streamed.strip()      # stopped, or parked, before it was stored
        if said:
            messages.append({"role": "assistant", "content": said})
    return len(messages) > before


# ---------------------------------------------------------------------------
# what the agent made
# ---------------------------------------------------------------------------
#
# An agent on Attacca files a long result as an artifact - `job_create_artifact`
# - rather than saying it. The artifact lives on Attacca and no call a node has
# reads one back, but the call that made it is an event of the session like any
# other, and carries what was written. So that is what is shown here. The
# tool's schema is Attacca's own and not part of what a node is told, so its
# arguments are read by the names such things go under; one that cannot be
# read that way is opened in the browser instead.

_made: dict = {}              # session id -> {event key: artifact}, as seen live

_ARTIFACT_VERBS = {"create", "write", "update", "append", "save", "publish"}
_TITLE_KEYS = ("title", "name", "filename", "file_name", "path")
_CONTENT_KEYS = ("content", "markdown", "body", "text", "html", "source", "data")
_URL = re.compile(r"https?://[^\s\"'<>)\]]+")


def _makes_artifact(name: str) -> bool:
    words = set(_tool_name(name).lower().split("_"))
    return bool(words & {"artifact", "artifacts"}) and bool(words & _ARTIFACT_VERBS)


def _strings(value, key: str = ""):
    """Every string in a JSON value, with the key it was found under."""
    if isinstance(value, str):
        yield key, value
    elif isinstance(value, dict):
        for name, inner in value.items():
            yield from _strings(inner, str(name))
    elif isinstance(value, list):
        for inner in value:
            yield from _strings(inner, key)


def _parsed(value):
    """A result that is JSON in a string, as a tool's often is."""
    if isinstance(value, str) and value.lstrip()[:1] in ("{", "["):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def artifact_of(payload: dict):
    """What a `…create_artifact` call made: {title, content, url}. None otherwise."""
    if not _makes_artifact(str(payload.get("name") or "")) or payload.get("error"):
        return None
    arguments, result = _parsed(payload.get("arguments")), _parsed(payload.get("result"))
    written = list(_strings(arguments))
    named = [text for key, text in written if key in _CONTENT_KEYS and text.strip()]
    # Under a name that says so, or - failing that - the one long thing in it.
    longest = max((text for _, text in written), key=len, default="")
    content = max(named, key=len) if named else (longest if "\n" in longest
                                                 or len(longest) > 200 else "")
    title = next((text.strip() for wanted in _TITLE_KEYS
                  for key, text in written + list(_strings(result))
                  if key == wanted and text.strip() and text is not content), "")
    found = next((_URL.search(text) for _, text in list(_strings(result)) + written
                  if text is not content and _URL.search(text)), None)
    return {"title": title or "artifact", "content": content,
            "url": found.group(0).rstrip(".,") if found else ""}


def _readable(made: dict) -> bool:
    """Whether it can be drawn here: text, and not a page meant for a browser."""
    head = made["content"].lstrip()[:200].lower()
    return bool(head) and not head.startswith(("<!doctype", "<html", "<svg", "<?xml"))


def _artifact_note(made: dict) -> str:
    """The lines under the call: what was made, and how to read it."""
    if not _readable(made):
        return (f"\n    {S.ACCENT}▤{S.R} {S.WHITE}{made['title']}{S.R} "
                f"{S.MUTED}- /artifacts open shows it in the browser{S.R}")
    lines = made["content"].strip().splitlines()
    rail = f"    {S.MUTED}│{S.R} "
    shown = [rail + S.GRAY + row + S.R for line in lines[:ARTIFACT_PREVIEW]
             for row in config.wrap_cells(line, max(20, config.tw() - 8), by_word=True)]
    more = len(lines) - ARTIFACT_PREVIEW
    return (f"\n    {S.ACCENT}▤{S.R} {S.WHITE}{made['title']}{S.R} "
            f"{S.MUTED}- {len(lines)} lines; /artifacts shows it all{S.R}\n"
            + "\n".join(shown[:ARTIFACT_PREVIEW])
            + (f"\n{rail}{S.MUTED}… {more} more{S.R}" if more > 0 else ""))


def artifacts() -> list:
    """Every artifact made in this session, oldest first.

    Read from the session's history, so one made while this terminal was
    closed - or in a turn started in the web app - is listed like any other.
    What was seen live stands in when the history cannot be read.
    """
    session = session_id()
    if not session:
        return []
    found = dict(_made.get(session, {}))
    try:
        rows = link().call("attacca_api.session_history",
                           {"session_id": session, "query": {}}, timeout=15) or []
    except Exception:
        rows = []
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict) or row.get("kind") != "tool_call":
            continue
        made = artifact_of(row.get("payload") or {})
        if made and (row.get("payload") or {}).get("result") is not None:
            found[row.get("id") or row.get("seq")] = made
    return list(found.values())


def web_address() -> str:
    """Attacca's web app, for what cannot be shown in a terminal.

    The host the socket is on, without the path the API lives under.
    """
    scheme, _, rest = http_base(providers.build(NAME).base_url).partition("://")
    return f"{scheme}://{rest.split('/', 1)[0]}"


def _open_in_browser(made: dict) -> None:
    import webbrowser
    address = made["url"] or web_address()
    where = ("" if made["url"] else " - Attacca's web app; the artifact is under "
             "this session there")
    try:
        opened = webbrowser.open(address)
    except Exception:
        opened = False
    if opened:
        print(f"  {S.OK}✓ Opened{S.R} {S.WHITE}{address}{S.R}{S.MUTED}{where}{S.R}\n")
    else:
        # No browser to hand it to - over SSH, in a container. The address is
        # still the answer; it only has to be opened by hand.
        print(f"  {S.WARN}◆ No browser could be opened here.{S.R} {S.WHITE}{address}"
              f"{S.R}{S.MUTED}{where}{S.R}\n")


def show_artifacts(argument: str = "") -> None:
    """`/artifacts`: what the agent made in this session.

    `/artifacts`            the list - or the artifact, when there is one
    `/artifacts <n>`        that one, in full
    `/artifacts open [n]`   it in the browser instead
    """
    from aetheris.renderer import _render_full
    words = argument.strip().lower().split()
    opening = bool(words) and words[0] == "open"
    number = next((word for word in words if word.isdigit()), "")
    if [word for word in words if word != "open" and not word.isdigit()]:
        print(f"  {S.ERR}✗ Usage: /artifacts [open] [number]{S.R}\n")
        return
    found = artifacts()
    if not found:
        print(f"  {S.GRAY}No artifact has been made in this session.{S.R}\n")
        return
    if number and not 1 <= int(number) <= len(found):
        print(f"  {S.ERR}✗ There is no artifact {number}; this session has "
              f"{len(found)}.{S.R}\n")
        return
    if not number and len(found) > 1 and not opening:
        print(f"\n  {S.BOLD}{S.ACCENT}Artifacts{S.R}")
        for index, made in enumerate(found, 1):
            size = (f"{len(made['content'].splitlines())} lines" if _readable(made)
                    else "opens in the browser")
            print(f"  {S.ACCENT}{index:3}.{S.R} {S.WHITE}{made['title']}{S.R} "
                  f"{S.MUTED}{size}{S.R}")
        print(f"  {S.MUTED}/artifacts <number> shows one; /artifacts open <number> "
              f"opens it in the browser.{S.R}\n")
        return
    made = found[int(number) - 1] if number else found[-1]
    if opening or not _readable(made):
        if not opening:
            print(f"  {S.MUTED}◆ {made['title']} is not something a terminal can "
                  f"draw.{S.R}")
        _open_in_browser(made)
        return
    rule = f"  {S.MUTED}{'─' * max(8, min(config.tw() - 4, 60))}{S.R}"
    print(f"\n  {S.ACCENT}▤{S.R} {S.BOLD}{S.WHITE}{made['title']}{S.R}\n{rule}\n"
          f"{_render_full(made['content'].strip())}\n{rule}")
    if made["url"]:
        print(f"  {S.MUTED}{made['url']}{S.R}")
    print()


# ---------------------------------------------------------------------------
# what it costs, and on which plan
# ---------------------------------------------------------------------------

_account_seen: tuple = (None, {})         # (the link it was asked over, the answer)


def account(live: zyris.Link, fresh: bool = False) -> dict:
    """Who this machine is paired as: `me` - the plan and the credit balance.

    Asked once per connection unless `fresh`: the plan does not change between
    two turns, and one more call under every answer is one more thing to wait
    on. `me` needs no scope, so it answers whatever was granted.
    """
    global _account_seen
    if not fresh and _account_seen[0] is live:
        return _account_seen[1]
    try:
        seen = live.call("attacca_api.me", {}, timeout=5) or {}
    except Exception:
        seen = {}              # an older deployment, or a slow one
    if not isinstance(seen, dict):
        seen = {}
    _account_seen = (live, seen)
    return seen


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
    if parts and account(live).get("plan"):
        # Which plan that is a share of. Only beside the numbers it explains:
        # a deployment that meters nothing has no line to put it on.
        parts.append(f"{account(live)['plan']} plan")
    if parts:
        print(f"\n  {S.MUTED}─ Attacca · {' · '.join(parts)}{S.R}\n")


def _show_account(live: zyris.Link) -> None:
    """The account's plan and what is left on it, as Attacca words them."""
    seen = account(live, fresh=True)
    if seen.get("plan"):
        print(f"  {S.GRAY}plan{S.R}     {S.WHITE}{seen['plan']}{S.R}")
    if seen.get("credits"):
        # A string the deployment formatted: its unit and precision, not ours.
        print(f"  {S.GRAY}balance{S.R}  {S.WHITE}{seen['credits']}{S.R} {S.MUTED}credits{S.R}")


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
    try:
        live = link()
    except Exception as error:
        print(f"  {S.ERR}{error}{S.R}\n")
        return
    if not session:
        _show_account(live)
        print(f"  {S.GRAY}No Attacca session yet - it starts with the first message.{S.R}\n")
        return
    usage = _usage(live, session)
    _show_account(live)
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
