"""Attacca as the provider: pairing, the turn, and the calls it makes back.

Attacca runs the agent; this machine runs the tools. So what is checked here
is the seam between the two, against a fake Attacca on a loopback port that
plays scripted turns - deltas, a call back into this machine that it waits on,
the events a real turn carries, and the end:

- **Pairing** follows the device flow to the letter: a code, polls at the
  interval the server asks for, a `slow_down` that widens it, an outage that is
  waited out, an expired code replaced, a refusal taken as final - and the
  credential saved owner-only, never printed.
- **A turn** is drawn by the same `stream_reply` every reply goes through,
  and a tool the agent asks for runs through the real `dispatch_tool`: a deny
  rule refuses it, the refusal goes back to the agent as the result, and a
  tool this machine never offered is an error rather than a guess.
- **A call nobody here asked for** - an agent in the web app reaching this
  terminal while it sits at the prompt - closes the prompt, keeps what was
  half-typed, and runs under the same rules.
- **Stopping** a turn tells Attacca how much of the answer was actually seen.
- **A plan** the agent hands back is shown and decided here: Enter approves,
  anything typed is what to change, and either way it is the next message.
- **A turn started somewhere else** - a message typed into the web app, on the
  session this terminal is in - is drawn here and kept in the conversation,
  live or read back from the cursor, and is never sent, answered or stopped
  from this end.
- **The title** is the one Attacca gave the session, not one asked of a model.
"""
import asyncio
import contextlib
import io
import os
import queue
import re
import stat
import signal
import struct
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    import msgpack
    from websockets.sync.server import serve
except ImportError:
    print('skipped: pip install "aetheris[attacca]" to test Attacca')
    sys.exit(0)

from aetheris import paths              # noqa: E402

HOME = tempfile.mkdtemp(prefix="attacca-home-")
os.environ[paths.ENV_VAR] = HOME
os.environ.pop("ATTACCA_CREDENTIAL", None)
os.environ.pop("ATTACCA_SERVER_URL", None)

from aetheris import config             # noqa: E402
config.MCP_ENABLED = False
config.SAVE_CHAT_HISTORY = False
config.AUTO_ALLOW = True                 # no approval prompt may block a test
config.GIT_AUTO_COMMIT = False

from aetheris import attacca, connect, permissions, providers, zyris   # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra and not ok else ''}")


GOOD = "zc_good_credential"
WORK = tempfile.mkdtemp(prefix="attacca-work-")
os.chdir(WORK)
NOTE = os.path.join(WORK, "notes.txt")
with open(NOTE, "w", encoding="utf-8") as handle:
    handle.write("the build order is parser, lexer, emitter\n")


def pack(envelope):
    return bytes([0x00]) + msgpack.packb(envelope, use_bin_type=True)


def unpack(raw):
    return msgpack.unpackb(bytes(raw[1:]), raw=False)


class FakeAttacca:
    """`attacca_api` over Zyris, with turns played from a script."""

    def __init__(self):
        self.received = []
        self.to_client: queue.Queue = queue.Queue()
        self.scripts = {}                # message text -> steps
        self.sessions_created = []
        self.cancelled = []
        self.answers = {}                # call id -> the node's res/err
        self.marks = {}                  # name -> when the script reached it
        self.busy_until = 0.0            # a turn already running on the session
        self.titles = {}                 # session id -> the title Attacca gave it
        self.injected: queue.Queue = queue.Queue()   # frames nobody here asked for
        self.replay = []                 # what a stream opened with a cursor reads back
        self.opened = []                 # the params of every turn_events call
        self.jobs = []                   # what every create_job was given
        self.history = []                # what session_history reads back
        self.server = serve(self.handle, "127.0.0.1", 0, process_request=self.door)
        self.port = self.server.socket.getsockname()[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}/zyris/v1/ws"

    def door(self, connection, request):
        if request.headers.get("Authorization") != f"Bearer {GOOD}":
            return connection.respond(401, "unknown credential\n")
        return None

    def handle(self, ws):
        hello = unpack(ws.recv())
        ws.send(pack({"t": "hello_ack", "protocol": {"major": 1, "minor": 0},
                      "serialization": "msgpack", "conn_id": "c", "resume_token": "r",
                      "node_id": "n", "node": {"system": "laptop", "program": "aetheris",
                                               "name": hello.get("node_name")},
                      "resumed": False, "features": ["cancel"]}))
        stream_id, seq = None, 0
        steps, waiting_for = [], None
        while True:
            while not self.to_client.empty():
                ws.send(self.to_client.get())
            while not self.injected.empty():
                steps = steps + list(self.injected.get())
            while steps and waiting_for is None:
                step = steps[0]
                if step[0] == "pause":            # until cancel_turn arrives
                    break
                if step[0] == "until":            # a clock, without blocking the reader
                    if time.time() < step[1]:
                        break
                    steps.pop(0)
                    continue
                if step[0] == "mark":
                    self.marks[step[1]] = time.time()
                    steps.pop(0)
                    continue
                steps.pop(0)
                if step[0] == "item":
                    ws.send(bytes([0x01]) + struct.pack(">II", stream_id, seq)
                            + msgpack.packb(step[1], use_bin_type=True))
                    seq += 1
                elif step[0] == "call":
                    ws.send(pack({"t": "req", "id": step[1], "method": step[2],
                                  "params": step[3]}))
                    waiting_for = step[1]
                elif step[0] == "end":
                    ws.send(pack({"t": "s_end", "stream": stream_id}))
            try:
                raw = ws.recv(timeout=0.02)
            except TimeoutError:
                continue
            except Exception:
                return
            env = unpack(raw)
            self.received.append(env)
            kind, rid, method = env.get("t"), env.get("id"), env.get("method")
            if kind in ("res", "err") and rid == waiting_for:
                self.answers[rid] = env
                waiting_for = None
                continue
            if kind in ("res", "err"):
                self.answers[rid] = env
                continue
            if kind != "req":
                continue
            params = env.get("params") or {}
            if method == "zyris.announce":
                result = {"accepted": [c["name"] for c in params["capabilities"]],
                          "rejected": []}
            elif method == "attacca_api.list_agents":
                result = [{"id": "ag1", "name": "Main Agent", "model": "qwen3-32b"},
                          {"id": "ag2", "name": "Reader"}]
            elif method == "attacca_api.create_session_with":
                self.sessions_created.append(params["session"])
                # Untitled reads back under a placeholder, as on attacca.cc.
                result = {"id": f"s{len(self.sessions_created)}", "running": False,
                          "title": PLACEHOLDER}
            elif method == "attacca_api.list_sessions":
                result = [{"id": f"s{n}", "running": False,
                           "title": self.titles.get(f"s{n}", PLACEHOLDER)}
                          for n in range(1, len(self.sessions_created) + 1)]
            elif method == "attacca_api.me":
                result = {"user_id": "u1", "email": "someone@example.com",
                          "display_name": "Someone", "scopes": list(attacca.SCOPES),
                          "plan": "Pro", "credits": "8,420"}
            elif method == "attacca_api.turn_events":
                stream_id, seq = env["stream"]["id"], 0
                self.opened.append(params)
                busy = self.busy_until > time.time()
                ws.send(pack({"t": "res", "id": rid, "result": {
                    "session_id": params["session_id"], "running": busy}}))
                if busy:
                    # Somebody else's turn, ending a moment from now.
                    steps = [("until", self.busy_until), ("mark", "busy_ended"),
                             ("item", {"type": "status", "running": False})]
                else:
                    # What attacca.cc does as a stream opens: the session's
                    # state *before* anything is sent. Read as the end of the
                    # turn, it ended live check #2 before its first word.
                    steps = [("item", {"type": "status", "running": False})]
                    if params.get("after") is not None:
                        # Resumed from a cursor: what was filed after it,
                        # with no `running: false` of its own to end on.
                        steps = steps + list(self.replay)
                        self.replay = []
                continue
            elif method == "attacca_api.send_message":
                self.marks["sent"] = time.time()
                steps = steps + [("item", {"type": "status", "running": True}),
                                 event(1, "chat_user", {"content": params["message"]})] \
                    + list(self.scripts.get(params["message"], []))
                result = None
            elif method == "attacca_api.create_job":
                self.jobs.append(params["job"])
                # Started as it is created, on a session of its own.
                result = {"id": f"j{len(self.jobs)}", "title": PLACEHOLDER,
                          "state": "processing", "session_id": f"js{len(self.jobs)}"}
            elif method == "attacca_api.session_history":
                result = list(self.history)
            elif method == "attacca_api.session_usage":
                result = {"model": "qwen3-32b", "context_tokens": 1234,
                          "input_tokens": 852281, "output_tokens": 11796,
                          "total_tokens": 864077, "credits_used": "0.05%"}
            elif method == "attacca_api.cancel_turn":
                self.cancelled.append(params)
                steps = [("item", {"type": "cancelled"}),
                         ("item", {"type": "status", "running": False}), ("end",)]
                result = None
            else:
                ws.send(pack({"t": "err", "id": rid, "error": {
                    "code": "method_not_found", "message": method, "retriable": False}}))
                continue
            ws.send(pack({"t": "res", "id": rid, "result": result}))

    def play(self, steps):
        """Frames on the open stream that nothing here asked for - a turn
        started in the web app."""
        self.injected.put(list(steps))

    def wait_answer(self, call_id, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            if call_id in self.answers:
                return self.answers[call_id]
            time.sleep(0.02)
        return None


def delta(text, kind="assistant"):
    return ("item", {"type": "delta", "kind": kind, "text": text})


def event(cursor, kind, payload):
    return ("item", {"type": "event", "cursor": cursor,
                     "event": {"seq": cursor, "cursor": cursor, "kind": kind,
                               "payload": dict(payload, kind=kind)}})


DONE = [("item", {"type": "status", "running": False}), ("end",)]
PLACEHOLDER = "New session"

server = FakeAttacca()


class Reply:
    """A fake `requests` response."""

    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeRequests:
    def __init__(self, script):
        self.script = list(script)
        self.posted = []

    def post(self, url, json=None, timeout=None):
        self.posted.append((url, json))
        return self.script.pop(0)


def with_requests(fake):
    providers._requests = lambda: fake


real_requests = providers._requests
slept = []
attacca.time.sleep = slept.append       # the poll waits are recorded, not waited


def run(coroutine, timeout=20):
    return asyncio.run(asyncio.wait_for(coroutine, timeout=timeout))


ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def quietly(function, *args):
    """Run it, and return what it printed as plain text - colour codes sit
    between the words of every line, so a check against coloured output can
    pass for the wrong reason."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = function(*args)
    return result, ANSI.sub("", out.getvalue())


print("--- the device endpoints live where the socket does ---")
check("wss becomes https, cut at /zyris/",
      attacca.http_base("wss://attacca.cc/api/zyris/v1/ws") == "https://attacca.cc/api")
check("ws becomes http", attacca.http_base("ws://127.0.0.1:9/zyris/v1/ws")
      == "http://127.0.0.1:9")

print("\n--- pairing: a code, polls at the server's pace, then a credential ---")
CODE = {"device_code": "zdc_secret", "user_code": "WXQR-7KBD",
        "verification_uri": "https://attacca.example/settings/zyris/device",
        "expires_in": 600, "interval": 1}
GRANT = {"credential": GOOD, "system": {"name": "laptop"}, "program": {"name": "aetheris"},
         "scopes": list(attacca.SCOPES), "owner_email": "someone@example.com"}
fake = FakeRequests([
    Reply(200, CODE),
    Reply(400, {"error": "authorization_pending"}),
    Reply(400, {"error": "slow_down", "interval": 7}),
    Reply(503, ValueError("an HTML error page")),
    Reply(200, GRANT),
])
with_requests(fake)
providers.settings_for("attacca")["base_url"] = server.url
provider = providers.build("attacca")
check("not paired yet says how to pair", "not paired" in provider.ready(), provider.ready())
slept.clear()
ok, shown = quietly(attacca.pair, provider)
check("pairing succeeds", ok is True, shown[-400:])
check("the code is shown, with where to type it",
      "WXQR-7KBD" in shown and "attacca.example/settings/zyris/device" in shown)
authorize_url, body = fake.posted[0]
check("it asks /zyris/v1/device/authorize on the socket's own host",
      authorize_url == attacca.http_base(server.url) + "/zyris/v1/device/authorize",
      authorize_url)
check("as the program 'aetheris', for exactly the scopes it uses",
      body["program"] == "aetheris" and body["scopes"] == list(attacca.SCOPES), str(body))
token_url, token_body = fake.posted[1]
check("it polls /device/token with the device grant type (RFC 8628)",
      token_url.endswith("/zyris/v1/device/token")
      and token_body == {"device_code": "zdc_secret", "grant_type": attacca.GRANT_TYPE},
      str(token_body))
check("at the server's interval, widened by slow_down, held through an outage",
      slept == [1, 1, 7, 7], str(slept))
check("the credential is saved", providers.settings_for("attacca").get("api_key") == GOOD)
check("and never printed", GOOD not in shown)
if os.name == "posix":
    mode = stat.S_IMODE(os.stat(providers.CONFIG_PATH).st_mode)
    check("in a file only its owner can read", mode & 0o077 == 0, oct(mode))
check("the account it landed in is named once", "someone@example.com" in shown)

fake = FakeRequests([Reply(200, CODE), Reply(400, {"error": "expired_token"}),
                     Reply(200, dict(CODE, user_code="HTPL-2FMR")),
                     Reply(200, dict(GRANT, scopes=["agents:read"]))])
with_requests(fake)
ok, shown = quietly(attacca.pair, provider)
check("an expired code is replaced by a fresh one, not given up on",
      ok and "HTPL-2FMR" in shown, shown[-300:])
check("a grant missing scopes says which, and how to fix it",
      "sessions:write" in shown and "pair again" in shown)

fake = FakeRequests([Reply(200, CODE), Reply(400, {"error": "access_denied",
                                                   "error_description": "the person said no"})])
with_requests(fake)
ok, shown = quietly(attacca.pair, provider)
check("a refusal is final, and says why", ok is False and "the person said no" in shown)
providers._requests = real_requests
providers.settings_for("attacca")["api_key"] = GOOD

print("\n--- without the extra, /connect attacca says what to install first ---")
real_libraries = zyris._libraries


def missing():
    raise zyris.MissingDependency(f"needs websockets. Install them with: {zyris.INSTALL_HINT}")


zyris._libraries = missing
ok, shown = quietly(connect._ensure_key, "attacca")
zyris._libraries = real_libraries
check("before any code is shown", ok is False and "aetheris[attacca]" in shown
      and "WXQR" not in shown, shown)

print("\n--- what this machine offers the agent ---")
offered = attacca.capability()
names = [tool["name"] for tool in offered["tools"]]
check("the capability is 'aetheris' v1", offered["name"] == "aetheris" and offered["version"] == 1)
check("the tool table is the one a local model gets", "read_file" in names
      and "run_cmd" in names and "edit_file" in names)
check("minus the tools that only mean something to the local loop",
      "spawn_agent" not in names and "view_image" not in names)
check("each is a unary call answering {output: string}",
      all(t["transfer"] == "unary" and t["response_schema"] == attacca.RESPONSE_SCHEMA
          for t in offered["tools"]))

print("\n--- connecting: the agent list is the model list ---")
providers.connect("attacca", model="Main Agent")
check("Attacca is the provider, and drives its own turns",
      providers.current().name == "attacca" and providers.current().drives_turns)
agents = providers.current().list_models()
check("agents are listed with their model", agents[0] == {"name": "Main Agent",
                                                          "detail": "qwen3-32b"}, str(agents))
check("the link announced this machine's tools", any(
    e.get("method") == "zyris.announce" and e["params"]["capabilities"][0]["name"] == "aetheris"
    for e in server.received))
check("and is listening for calls", attacca.connected())

print("\n--- a turn: streamed, a tool run here, the answer kept ---")
server.scripts["what does notes.txt say?"] = [
    delta("Let me look.\n"),
    ("call", 1001, "aetheris.read_file", {"filepath": NOTE}),
    event(10, "tool_call", {"name": "zyris__aetheris_v1__read_file",
                            "arguments": {"filepath": NOTE},
                            "result": {"output": "..."}}),
    event(11, "tool_call", {"name": "web_search", "arguments": {"q": "x"},
                            "result": {"hits": 3}}),
    delta("weighing it", kind="reasoning"),
    delta("It lists the build order.\n"),
    event(12, "chat_agent", {"content": "Let me look.\nIt lists the build order."}),
] + DONE
permissions.hold("allow", ["read_file"], "attacca-test")
messages = [{"role": "system", "content": "local system prompt"},
            {"role": "user", "content": "what does notes.txt say?"}]
config.token_history.clear()
answer, shown = quietly(lambda: run(attacca.run_turn(messages)))
check("the answer is Attacca's stored one", answer == "Let me look.\nIt lists the build order.",
      repr(answer))
check("appended to the conversation like any answer",
      messages[-1] == {"role": "assistant", "content": answer})
check("the streamed text was drawn", "Let me look." in shown and "It lists the build order." in shown)
result = server.wait_answer(1001)
check("the file was read here, through the real tool, and sent back",
      result and "parser, lexer, emitter" in result["result"]["output"], str(result))
check("a call it served is not drawn a second time from the event",
      "zyris__aetheris_v1__read_file" not in shown)
check("a tool that ran on Attacca gets one line", "◇ web_search (on Attacca)" in shown)
created = server.sessions_created[0]
check("the session went to the chosen agent", created["agent_id"] == "ag1", str(created))
check("with a preamble naming this node and directory, and no title",
      "laptop/aetheris/" in created["preamble"] and WORK in created["preamble"]
      and "title" not in created, created.get("preamble", "")[:200])
check("the local small-model system prompt is not sent",
      "local system prompt" not in str(server.received))
check("no line of zero tokens is printed for a turn nothing counted", "tokens: 0 in" not in shown)
check("and none is recorded", config.token_history == [], str(config.token_history))
check("what the session cost is reported once, from Attacca",
      "Attacca · qwen3-32b · 1,234 tokens in context · credits used 0.05%" in shown)
check("the session is remembered for the next turn", config.ATTACCA_SESSION == "s1")

print("\n--- /usage shows what Attacca counted, not a local estimate ---")
_, shown = quietly(attacca.show_usage)
check("the session's totals, as Attacca sent them",
      "input 852,281" in shown and "output 11,796" in shown and "total 864,077" in shown,
      shown)
check("with the context the agent reads now, and the credits",
      "context  1,234" in shown and "credits  0.05%" in shown, shown)
check("and not the local graph's 'no data'", "No token usage data" not in shown)
kept, config.ATTACCA_SESSION = config.ATTACCA_SESSION, ""
_, shown = quietly(attacca.show_usage)
check("before the first message, it says there is no session yet",
      "No Attacca session yet" in shown, shown)
config.ATTACCA_SESSION = kept

print("\n--- the same rules: refused, and never offered ---")
server.scripts["run something"] = [
    ("call", 1002, "aetheris.run_cmd", {"command": "echo hi"}),
    ("call", 1003, "aetheris.spawn_agent", {"task": "x"}),
    delta("Done.\n"),
] + DONE
permissions.hold("deny", ["run_cmd"], "attacca-test")
messages.append({"role": "user", "content": "run something"})
answer, shown = quietly(lambda: run(attacca.run_turn(messages)))
refused = server.wait_answer(1002)
check("a deny rule refuses the agent's call, as it would a local model's",
      refused and "blocked by your permission rules" in refused["result"]["output"], str(refused))
never = server.wait_answer(1003)
check("a tool this machine does not offer is an error, not a guess",
      never and never["t"] == "err" and never["error"]["code"] == "method_not_found", str(never))
check("the second turn reused the session", len(server.sessions_created) == 1)
check("with no final event, the streamed text is the answer", answer == "Done.", repr(answer))
permissions.release("attacca-test")

print("\n--- a session already busy is let finish before this turn is sent ---")
server.scripts["after the other one"] = [delta("Now me.\n")] + DONE
server.busy_until = time.time() + 0.6
messages.append({"role": "user", "content": "after the other one"})
answer, shown = quietly(lambda: run(attacca.run_turn(messages)))
check("it says why it is waiting", "already running on this Attacca session" in shown, shown[:300])
check("and sends only once that turn has ended", server.marks.get("sent", 0)
      >= server.marks.get("busy_ended", float("inf")), str(server.marks))
check("whose end is not mistaken for the end of this one", answer == "Now me.", repr(answer))

print("\n--- stopping a turn says how much was seen ---")
# Opening with "\n\n" the way agents on attacca.cc do: trimmed from the
# screen, but still counted, since Attacca's copy of the answer has them.
server.scripts["slow"] = [delta("\n\n"), delta("partial "), ("pause",)]
messages.append({"role": "user", "content": "slow"})


async def stop_it():
    task = asyncio.ensure_future(attacca.run_turn(messages))
    for _ in range(200):
        await asyncio.sleep(0.02)
        if attacca.current_turn is not None and attacca.current_turn.printed:
            break
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return "cancelled"
    return "finished"


outcome, _ = quietly(lambda: run(stop_it()))
check("the turn ends on the cancel", outcome == "cancelled", outcome)
deadline = time.time() + 3
while not server.cancelled and time.time() < deadline:
    time.sleep(0.02)
check("Attacca is told to stop, keeping exactly what was delivered",
      server.cancelled and server.cancelled[0] == {
          "session_id": "s1", "delivered": {"cursor": None, "chars": len("\n\npartial ")}},
      str(server.cancelled))
check("and nothing is left half-running here", attacca.current_turn is None)
check("what had been said by then is the answer the conversation keeps",
      messages[-1] == {"role": "assistant", "content": "partial"}, str(messages[-1]))

print("\n--- what a turn is doing, as it does it ---")
TITLE = "사용자 요청에 맞춰 도구 목록을 정리하는 중"
server.scripts["show me everything"] = [
    event(2, "work_summary", {"content": ""}),
    event(2, "work_summary", {"content": "Looking for the scroll math"}),
    delta("weighing the options", "reasoning"),
    # A thinking block opens untitled; the title lands by rewriting it, and
    # a rewrite that changes nothing must not draw a second line.
    event(3, "thinking", {"content": "weighing the options", "title": None}),
    event(3, "thinking", {"content": "weighing the options", "title": TITLE}),
    event(3, "thinking", {"content": "weighing the options", "title": TITLE}),
    event(4, "subagent_update", {"summary": "reading the docs", "status": "running"}),
    event(4, "subagent_update", {"summary": "reading the docs", "status": "completed"}),
    event(5, "error", {"message": "node laptop/other timed out"}),
    event(6, "tool_call", {"name": "report_result", "result": "ok", "error": None,
                           "arguments": {"summary": "Scroll math lives in rows.rs",
                                         "status": "success"}}),
    delta("Answer."),
    event(7, "chat_agent", {"content": "Answer."}),
] + DONE
# Exactly what attacca.cc did in live check #4: the last block's title, and
# the work's heading, rewritten in *after* the turn has said it is over.
server.scripts["a short one"] = [
    event(2, "work_summary", {"content": ""}),
    event(3, "thinking", {"content": "1024 against 1000", "title": None}),
    delta("By 24."), event(4, "chat_agent", {"content": "By 24."}),
    ("item", {"type": "status", "running": False}),
    event(3, "thinking", {"content": "1024 against 1000", "title": "차이를 계산하는 중"}),
    event(2, "work_summary", {"content": "차이를 계산하는 중"}),
    ("end",)]
messages.append({"role": "user", "content": "a short one"})
answer, shown = quietly(lambda: run(attacca.run_turn(messages)))
check("a title that lands after the turn ended is still shown", "✻ 차이를 계산하는 중" in shown,
      shown[-400:])
check("and the heading that repeats it word for word is not shown twice",
      shown.count("차이를 계산하는 중") == 1, shown[-400:])
messages.append({"role": "user", "content": "show me everything"})
answer, shown = quietly(lambda: run(attacca.run_turn(messages)))
check("the answer still arrives", answer == "Answer.", repr(answer))
check("a thinking block's title, once it lands", f"✻ {TITLE}" in shown, shown[-600:])
check("drawn once, however often the event is rewritten", shown.count(TITLE) == 1)
check("the heading of the work", "▾ Looking for the scroll math" in shown)
check("a sub-agent while it runs, and when it is done",
      "sub-agent running: reading the docs" in shown
      and "sub-agent done: reading the docs" in shown)
check("an error Attacca recorded is said, not dropped",
      "✗ Attacca: node laptop/other timed out" in shown)
check("the run's report reads as its result, not as one more tool",
      "◆ done Scroll math lives in rows.rs" in shown and "report_result" not in shown)

print("\n--- a question is answered inside the turn that asked it ---")
ASK = {"questions": [
    {"header": "DB", "question": "Which database?", "multiSelect": False,
     "options": [{"label": "Postgres", "description": "the one in prod"},
                 {"label": "SQLite"}]},
    {"question": "Anything else?", "multiSelect": True,
     "options": [{"label": "tests"}, {"label": "docs"}]}]}
EXPECTED = ("[DB] Which database?\n  Postgres (the one in prod)\n\n"
            "Anything else?\n  - tests\n  - docs")
# The agent waits on Attacca for the answer *inside* the turn: nothing more
# comes until it is sent, and without an answer here the turn never ends.
server.scripts["ask me"] = [event(2, "tool_call", {"name": "question", "arguments": ASK,
                                                   "result": None, "error": None})]
server.scripts[EXPECTED] = [
    event(3, "tool_call", {"name": "question", "arguments": ASK, "error": None,
                           "result": {"status": "answered", "answer": EXPECTED}}),
    delta("Postgres it is."), event(4, "chat_agent", {"content": "Postgres it is."}),
] + DONE
import builtins                          # noqa: E402
typed = iter(["1", "1, 2"])
real_input, builtins.input = builtins.input, lambda prompt="": next(typed)
messages.append({"role": "user", "content": "ask me"})
try:
    answer, shown = quietly(lambda: run(attacca.run_turn(messages)))
finally:
    builtins.input = real_input
sent = [e["params"]["message"] for e in server.received
        if e.get("method") == "attacca_api.send_message"]
check("the options are put to the person", "1 Postgres - the one in prod" in shown, shown[-500:])
check("the answer goes back with the question it answers", sent[-1] == EXPECTED, repr(sent[-1]))
check("and the turn carries on to its end", answer == "Postgres it is.", repr(answer))
check("an answered question is not asked again", shown.count("Which database?") == 1)
step = {"options": [{"label": "a"}, {"label": "b"}], "multi": False}
check("a label is a pick, as a remote's button sends it", attacca._picks("b", step) == [1])
check("two numbers for a one-answer question are not a pick",
      attacca._picks("1 2", step) == [])
check("anything else is a typed answer, marked as typed",
      attacca._picks("neither", step) == [])

print("\n--- closing the window stops the turn on Attacca ---")
if hasattr(signal, "SIGTERM") and os.name != "nt":
    server.scripts["slow again"] = [delta("partial "), ("pause",)]
    messages.append({"role": "user", "content": "slow again"})
    before = len(server.cancelled)

    async def close_it():
        task = asyncio.ensure_future(attacca.run_turn(messages))
        for _ in range(200):
            await asyncio.sleep(0.02)
            if attacca.current_turn is not None and attacca.current_turn.printed:
                break
        os.kill(os.getpid(), signal.SIGTERM)
        await task

    try:
        quietly(lambda: run(close_it()))
        code = None
    except SystemExit as stop:
        code = stop.code
    check("the program ends, as a closed window should", code == 128 + signal.SIGTERM, repr(code))
    check("having told Attacca to stop the turn first", len(server.cancelled) == before + 1,
          str(server.cancelled[before:]))
    check("and SIGTERM is left as it was found once the turn is over",
          signal.getsignal(signal.SIGTERM) == signal.SIG_DFL)

print("\n--- the agent is told what a local model would have been ---")
with open("AGENTS.md", "w", encoding="utf-8") as handle:
    handle.write("Run the tests with `make check` - never pytest directly.\n")
with open(".env", "w", encoding="utf-8") as handle:
    handle.write("STRIPE_KEY=sk_live_notarealkey123456\n")
try:
    told = attacca._preamble(attacca.link())
finally:
    os.remove("AGENTS.md")
    os.remove(".env")
check("the project's own instructions", "make check" in told, told[:300])
check("the .env names, and how to use them", "STRIPE_KEY" in told and "{{env:NAME}}" in told)
check("never the .env value", "sk_live_notarealkey123456" not in told)

print("\n--- a call nobody here made: the prompt makes way, keeps the line ---")
from aetheris import app                 # noqa: E402


class FakeBuffer:
    text = "half a sentence"


class FakeSession:
    def __init__(self):
        self.default_buffer = FakeBuffer()
        self.defaults = []

    async def prompt_async(self, message, default=None):
        self.defaults.append(default)
        await asyncio.Event().wait()       # nobody types


fake_prompt = FakeSession()
check("nothing is waiting yet", not attacca.call_waiting())
server.to_client.put(pack({"t": "req", "id": 1004, "method": "aetheris.read_file",
                           "params": {"filepath": NOTE}}))
outcome = run(app._typed_or_remote(fake_prompt, "> "), timeout=5)
check("the prompt closes for the call", outcome is app.IDLE_CALL)
check("the first prompt opened the ordinary way", fake_prompt.defaults[0] is None,
      str(fake_prompt.defaults))
check("what was half-typed is set aside", app._kept_typing == "half a sentence")
check("the call is still queued - looked at, not taken", attacca.call_waiting())
outcome = run(app._typed_or_remote(fake_prompt, "> "), timeout=5)
check("and the next prompt opens with it put back",
      fake_prompt.defaults[-1] == "half a sentence", str(fake_prompt.defaults))
permissions.hold("allow", ["read_file"], "attacca-test")
served, shown = quietly(attacca.serve_waiting)
check("the call is served once", served == 1)
check("with a line saying where it came from", "not from this terminal" in shown, shown)
answer = server.wait_answer(1004)
check("through the same tool, under the same rules",
      answer and "parser, lexer, emitter" in answer["result"]["output"], str(answer))
permissions.release("attacca-test")

print("\n--- an answer said in parts is kept whole ---")
attacca.stop_watching_session()
server.scripts["two parts"] = [
    delta("First I will look.\n"),
    event(20, "chat_agent", {"content": "First I will look."}),
    event(21, "tool_call", {"name": "web_search", "arguments": {}, "result": {"hits": 1}}),
    delta("Found it.\n"),
    event(22, "chat_agent", {"content": "Found it."}),
] + DONE
messages.append({"role": "user", "content": "two parts"})
answer, shown = quietly(lambda: run(attacca.run_turn(messages)))
check("both things the agent said are in the saved answer",
      answer == "First I will look.\n\nFound it.", repr(answer))
check("and neither is drawn twice", shown.count("First I will look.") == 1
      and shown.count("Found it.") == 1, shown)
check("the account's plan is named beside what the session cost",
      "credits used 0.05% · Pro plan" in shown, shown[-300:])
check("the place it got to is remembered", config.ATTACCA_CURSOR == 22,
      str(config.ATTACCA_CURSOR))

print("\n--- /usage names the plan and what is left on it ---")
_, shown = quietly(attacca.show_usage)
check("the plan, as Attacca calls it", "plan     Pro" in shown, shown)
check("and the balance, as Attacca words it", "balance  8,420" in shown, shown)

print("\n--- a plan is decided here, by the message that follows it ---")
PLAN = {"markdown": "# Plan\n\n1. parse the notes\n2. emit the order"}
server.scripts["plan it"] = [event(23, "tool_call", {
    "name": "submit_plan", "arguments": PLAN, "result": None, "error": None})]
server.scripts[attacca.PLAN_APPROVED] = [
    event(24, "tool_call", {"name": "submit_plan", "arguments": PLAN, "error": None,
                            "result": {"submitted": True, "status": "answered",
                                       "decision": attacca.PLAN_APPROVED}}),
    delta("Starting."), event(25, "chat_agent", {"content": "Starting."}),
] + DONE
real_input, builtins.input = builtins.input, lambda prompt="": ""
messages.append({"role": "user", "content": "plan it"})
try:
    answer, shown = quietly(lambda: run(attacca.run_turn(messages)))
finally:
    builtins.input = real_input
sent = [e["params"]["message"] for e in server.received
        if e.get("method") == "attacca_api.send_message"]
check("the plan is shown, rendered", "Plan from Attacca" in shown
      and "parse the notes" in shown and "emit the order" in shown, shown[-600:])
check("Enter approves it, as a message", sent[-1] == attacca.PLAN_APPROVED, repr(sent[-1]))
check("and the turn carries on to its end", answer == "Starting.", repr(answer))
check("a decided plan is not offered again", shown.count("Plan from Attacca") == 1)
builtins.input = lambda prompt="": "  use sqlite instead  "
try:
    decision, _ = quietly(attacca.decide, PLAN["markdown"])
finally:
    builtins.input = real_input
check("anything typed is the decision, as typed", decision == "use sqlite instead", decision)
check("a wait that ran out leaves the plan open", not attacca._answered(
    {"result": {"submitted": True, "status": "timeout"}}, "decision"))
check("an empty plan is no plan", attacca.plan_of({"markdown": "  \n"}) == ""
      and attacca.plan_of({}) == "")

print("\n--- a message typed in the web app is drawn here ---")
attacca.FOLLOW_QUIET, attacca.WATCH_RETRY = 0.3, 0
before = len(messages)
sends_before, cancels_before = len(sent), len(server.cancelled)


async def typed_elsewhere(frames):
    waiting = asyncio.ensure_future(app._typed_or_remote(fake_prompt, "> "))
    for _ in range(250):                       # until the idle watch is open
        await asyncio.sleep(0.02)
        if attacca._watch.stream is not None:
            break
    server.play(frames)
    return await waiting

permissions.hold("allow", ["read_file"], "attacca-test")
app._kept_typing = ""
outcome = run(typed_elsewhere([
    ("item", {"type": "status", "running": True}),
    event(30, "chat_user", {"content": "what is in notes.txt, from the web"}),
    ("call", 1010, "aetheris.read_file", {"filepath": NOTE}),
    delta("It is the build order.\n"),
    event(31, "chat_agent", {"content": "It is the build order."}),
    ("item", {"type": "status", "running": False}),
]))
check("the prompt makes way for it", outcome is app.WEB_TURN)
check("and keeps what was half-typed", app._kept_typing == "half a sentence")
check("watched from where this terminal had got to",
      server.opened[-1].get("after") == 25, str(server.opened[-1]))
grew, shown = quietly(lambda: run(attacca.follow(messages)))
check("their message is drawn as a line of the conversation",
      "❯ what is in notes.txt, from the web" in shown
      and "sent from Attacca's web app" in shown, shown)
check("and the answer under it", "It is the build order." in shown)
check("the call it made into this machine is served",
      "parser, lexer, emitter" in str(server.wait_answer(1010)))
check("both are kept in the conversation", grew and messages[before:] == [
    {"role": "user", "content": "what is in notes.txt, from the web"},
    {"role": "assistant", "content": "It is the build order."}], str(messages[before:]))
sent = [e["params"]["message"] for e in server.received
        if e.get("method") == "attacca_api.send_message"]
check("nothing was sent from here - it was not this terminal's turn",
      len(sent) == sends_before and len(server.cancelled) == cancels_before,
      str(sent[sends_before:]))
check("the place it got to moves with it", config.ATTACCA_CURSOR == 31)
permissions.release("attacca-test")

server.play([event(32, "thinking", {"title": "Reading the notes"})])
shown_as_turn = False
deadline = time.time() + 3             # `time.sleep` is recorded here, not waited
while config.ATTACCA_CURSOR != 32 and time.time() < deadline:
    shown_as_turn = shown_as_turn or attacca.activity_now()
check("a title that lands afterwards is not a turn", not shown_as_turn)
check("only the place moves", config.ATTACCA_CURSOR == 32, str(config.ATTACCA_CURSOR))

print("\n--- and one typed while this terminal was away is read back ---")
attacca.stop_watching_session()
server.replay = [event(40, "chat_user", {"content": "while you were away"}),
                 event(41, "chat_agent", {"content": "Answered while you were away."})]
before = len(messages)
check("it is found when the prompt next opens", attacca.look())
check("asked for from the cursor, not from the start",
      server.opened[-1].get("after") == 32, str(server.opened[-1]))
grew, shown = quietly(lambda: run(attacca.follow(messages)))
check("both sides are drawn, though neither streamed",
      "❯ while you were away" in shown and "Answered while you were away." in shown, shown)
check("and kept", messages[before:] == [
    {"role": "user", "content": "while you were away"},
    {"role": "assistant", "content": "Answered while you were away."}], str(messages[before:]))
check("once: there is nothing more to show", not attacca.look())

print("\n--- a question asked in their turn is answered by the next message ---")
before = len(messages)
server.play([("item", {"type": "status", "running": True}),
             event(50, "chat_user", {"content": "pick a database for me"}),
             event(51, "tool_call", {"name": "question", "arguments": ASK,
                                     "result": None, "error": None})])
deadline = time.time() + 3
while not attacca.activity_now() and time.time() < deadline:
    time.sleep(0.02)
grew, shown = quietly(lambda: run(attacca.follow(messages)))
check("the question is shown, and how to answer it",
      "Which database?" in shown and "your next message is the answer" in shown, shown)
check("without being asked here a second time - the prompt comes back",
      "s1" in attacca._parked and attacca.current_turn is None)
server.scripts["Postgres"] = [delta("Postgres, then."),
                              event(52, "chat_agent", {"content": "Postgres, then."})] + DONE
server.busy_until = time.time() + 0.3     # still running: it is waiting on this
messages.append({"role": "user", "content": "Postgres"})
answer, shown = quietly(lambda: run(attacca.run_turn(messages)))
check("the answer typed here is sent at once, not held for the turn to end",
      "already running" not in shown, shown[:300])
check("and the turn carries on", answer == "Postgres, then.", repr(answer))

print("\n--- looking away from their turn does not stop it ---")
before, cancels_before = len(messages), len(server.cancelled)
check("back at the prompt, nothing of the turn just had is taken for theirs",
      not attacca.look() and attacca._watch.stream is not None)
server.play([("item", {"type": "status", "running": True}),
             event(60, "chat_user", {"content": "a long one, from the web"}),
             delta("Working on it")])
deadline = time.time() + 3
while not attacca.activity_now() and time.time() < deadline:
    pass


async def look_away():
    task = asyncio.ensure_future(attacca.follow(messages))
    for _ in range(200):
        await asyncio.sleep(0.02)
        if attacca.current_turn is not None and attacca.current_turn.printed:
            break
    task.cancel()                          # what Ctrl+C does to it
    try:
        await task
    except asyncio.CancelledError:
        return "stopped following"
    return "finished"

outcome, _ = quietly(lambda: run(look_away()))
check("Ctrl+C ends the following", outcome == "stopped following", outcome)
check("and tells Attacca nothing: it was not this terminal's turn to stop",
      len(server.cancelled) == cancels_before, str(server.cancelled[cancels_before:]))
check("what had been said is kept", messages[before:] == [
    {"role": "user", "content": "a long one, from the web"},
    {"role": "assistant", "content": "Working on it"}], str(messages[before:]))
server.play([delta(" still"), event(61, "chat_agent", {"content": "Working on it still."}),
             ("item", {"type": "status", "running": False})])
came_back = False
deadline = time.time() + 3
while attacca._watch.muted and time.time() < deadline:
    came_back = came_back or attacca.activity_now()
check("the rest of that turn does not open the prompt again", not came_back)
check("and it is listened for again once the turn has ended",
      not attacca._watch.muted and config.ATTACCA_CURSOR == 61, str(config.ATTACCA_CURSOR))

print("\n--- the title is the one Attacca gave the session ---")
attacca.TITLE_PAUSE = 0.01
attacca._title_waited.clear()
check("a session nobody has named reads back under a placeholder",
      attacca._placeholders.get("s1") == PLACEHOLDER, str(attacca._placeholders))
check("which is not a title", run(attacca.title_when_named()) == "")
asked = [e for e in server.received if e.get("method") == "attacca_api.list_sessions"]
check("it was waited for, a few times, the first time",
      len(asked) == attacca.TITLE_TRIES, str(len(asked)))
run(attacca.title_when_named())
asked = [e for e in server.received if e.get("method") == "attacca_api.list_sessions"]
check("and asked for once on every turn after", len(asked) == attacca.TITLE_TRIES + 1)
server.titles["s1"] = "빌드 순서 정리"
check("once Attacca has named it, that is the title",
      run(attacca.title_when_named()) == "빌드 순서 정리")

print("\n--- the session follows the conversation ---")
from aetheris import session            # noqa: E402
config.SAVE_CHAT_HISTORY = True
config.SESSION_DIR = tempfile.mkdtemp(prefix="attacca-sessions-")
sid = session.save_session(messages, None)
saved = session.load_session(sid)
check("saved with the session file", saved.get("attacca_session") == "s1")
check("with the place it had got to", saved.get("attacca_cursor") == config.ATTACCA_CURSOR
      and config.ATTACCA_CURSOR >= 52, str(saved.get("attacca_cursor")))
config.SESSION_TITLE = ""
renamed, shown = quietly(lambda: run(app._name_from_attacca(sid)))
check("and filed under Attacca's title for it", config.SESSION_TITLE == "빌드 순서 정리"
      and session.load_session(renamed).get("title") == "빌드 순서 정리", shown)
check("which nothing here asked a model for", renamed != sid and "session titled" in shown)
config.SESSION_TITLE = ""
kept_cursor = config.ATTACCA_CURSOR
attacca.forget_session()
check("a new conversation forgets the place with the session",
      config.ATTACCA_SESSION == "" and config.ATTACCA_CURSOR == 0)
config.ATTACCA_SESSION = ""
plain = session.save_session([{"role": "system", "content": "x"}], None)
check("and absent from one that never used Attacca",
      "attacca_session" not in session.load_session(plain))
check("live state, not a setting: /set does not offer it",
      "ATTACCA_SESSION" not in config.settable())
config.SAVE_CHAT_HISTORY = False

print("\n--- what !commands printed goes in front of the next message ---")
config.ATTACCA_SESSION, config.ATTACCA_CURSOR = "s1", 0
attacca._parked.discard("s1")
server.scripts.clear()
messages.append({"role": "user", "content": "[Shell] $ git status\nnothing to commit"})
messages.append({"role": "user", "content": "[Shell] $ ls\nnotes.txt"})
messages.append({"role": "user", "content": "what does that mean"})
wanted = (attacca.SHELL_CONTEXT + "\n$ git status\nnothing to commit\n\n$ ls\nnotes.txt"
          "\n\nwhat does that mean")
server.scripts[wanted] = [delta("Clean."), event(70, "chat_agent", {"content": "Clean."})] + DONE
answer, shown = quietly(lambda: run(attacca.run_turn(messages)))
sent = [e["params"]["message"] for e in server.received
        if e.get("method") == "attacca_api.send_message"]
check("the commands and their output ride with the line", sent[-1] == wanted, repr(sent[-1]))
check("and the turn is answered as any other", answer == "Clean.", repr(answer))
messages.append({"role": "user", "content": "and now"})
check("only the ones since the last answer", attacca._with_shell(messages) == "and now")
messages.pop()
config.RETURN_SHELL_OUTPUT = False
check("none at all with RETURN_SHELL_OUTPUT off",
      attacca._with_shell(messages[:-1]) == "what does that mean")
config.RETURN_SHELL_OUTPUT = True
check("a setting /set offers", "RETURN_SHELL_OUTPUT" in config.settable())

print("\n--- an artifact the agent made is shown here ---")
REPORT = "# Build order\n\n" + "\n".join(f"- step {n}" for n in range(1, 13))
MADE = {"name": "job_create_artifact", "error": None,
        "arguments": {"title": "Build order", "content": REPORT}}
PAGE = {"name": "job_create_artifact", "error": None,
        "arguments": {"name": "chart.html", "html": "<!doctype html><html><b>x</b></html>"},
        "result": "{\"id\": \"art_2\"}"}
server.scripts["write it up"] = [
    event(80, "tool_call", dict(MADE, result=None)),
    event(80, "tool_call", dict(MADE, result={"id": "art_1",
                                              "url": "https://attacca.example/a/art_1."})),
    event(81, "tool_call", PAGE),
    delta("Filed."), event(82, "chat_agent", {"content": "Filed."})] + DONE
messages.append({"role": "user", "content": "write it up"})
answer, shown = quietly(lambda: run(attacca.run_turn(messages)))
check("the call is named, with what it made", "job_create_artifact (on Attacca)" in shown
      and "Build order" in shown, shown[-700:])
check("its first lines are shown under it", "- step 4" in shown and "- step 5" not in shown
      and "… 8 more" in shown, shown[-700:])
check("once, though the event was written twice", shown.count("▤ Build order") == 1, shown)
check("with how to read the rest", "/artifacts shows it all" in shown)
check("a page is not drawn; it is offered to the browser",
      "chart.html" in shown and "/artifacts open" in shown and "<b>" not in shown, shown[-400:])
made = attacca.artifact_of(dict(MADE, result={"url": "https://attacca.example/a/art_1."}))
check("read off the call: title, content, address", made == {
    "title": "Build order", "content": REPORT, "url": "https://attacca.example/a/art_1"}, str(made))
check("content under a name of its own is still found", attacca.artifact_of(
    {"name": "zyris__x__create_artifact", "arguments": {"label": "n", "payload": REPORT},
     "result": {}})["content"] == REPORT)
check("a tool that makes none is not one", attacca.artifact_of(
    {"name": "job_list_artifacts", "arguments": {}, "result": []}) is None
    and attacca.artifact_of({"name": "read_file", "arguments": {"path": "a"}}) is None)
check("nor is one that failed", attacca.artifact_of(dict(MADE, error="quota")) is None)

opened = []
import webbrowser                       # noqa: E402
real_open, webbrowser.open = webbrowser.open, lambda address: opened.append(address) or True
try:
    _, shown = quietly(attacca.show_artifacts, "")
    check("/artifacts lists them when there are several", "1. Build order" in shown
          and "2. chart.html" in shown and "opens in the browser" in shown, shown)
    _, shown = quietly(attacca.show_artifacts, "1")
    check("/artifacts <n> shows one in full", "step 12" in shown and "▤ Build order" in shown
          and not opened, shown[-300:])
    quietly(attacca.show_artifacts, "open 1")
    check("/artifacts open takes its address to the browser",
          opened == ["https://attacca.example/a/art_1"], str(opened))
    _, shown = quietly(attacca.show_artifacts, "2")
    check("one a terminal cannot draw goes to the browser by itself",
          opened[-1] == attacca.web_address() and "Opened" in shown, f"{opened} {shown}")
    check("to the web app, where no address came with it",
          attacca.web_address() == f"http://127.0.0.1:{server.port}", attacca.web_address())
    _, shown = quietly(attacca.show_artifacts, "9")
    check("a number past the end says how many there are", "There is no artifact 9" in shown)
    # After a restart nothing was seen live: the session's history is asked.
    attacca._made.clear()
    server.history = [{"seq": 80, "cursor": 80, "kind": "tool_call",
                       "payload": dict(MADE, result={"id": "art_1"})},
                      {"seq": 82, "cursor": 82, "kind": "chat_agent",
                       "payload": {"content": "Filed."}}]
    _, shown = quietly(attacca.show_artifacts, "")
    check("read back from the session's history, and shown as the only one",
          "step 12" in shown and "Artifacts" not in shown, shown[-300:])
    webbrowser.open = lambda address: False
    _, shown = quietly(attacca.show_artifacts, "open")
    check("with no browser here, the address is said instead",
          "No browser" in shown and attacca.web_address() in shown, shown)
finally:
    webbrowser.open = real_open
server.history = []

print("\n--- /planmode is Attacca's own plan mode: a job that plans first ---")
check("the pairing asks for the scope a job needs", attacca.PLAN_SCOPE in attacca.SCOPES)
check("and with it granted, the plan is Attacca's to ask for", quietly(attacca.plans_here)[0])
config.PLANMODE = True
server.scripts[attacca.PLAN_APPROVED] = [
    event(24, "tool_call", {"name": "submit_plan", "arguments": PLAN, "error": None,
                            "result": {"submitted": True, "status": "answered",
                                       "decision": attacca.PLAN_APPROVED}}),
    delta("Starting."), event(25, "chat_agent", {"content": "Starting."}),
] + DONE
before = len([e for e in server.received if e.get("method") == "attacca_api.send_message"])
server.replay = [("item", {"type": "status", "running": True}),
                 event(1, "chat_user", {"content": "as Attacca filed it"}),
                 event(2, "tool_call", {"name": "submit_plan", "arguments": PLAN,
                                        "result": None, "error": None})]
messages.append({"role": "user", "content": "[Shell] $ ls\nnotes.txt"})
messages.append({"role": "user", "content": "refactor the parser"})
builtins.input = lambda prompt="": ""
try:
    answer, shown = quietly(lambda: run(attacca.run_turn(messages)))
finally:
    builtins.input = real_input
job = server.jobs[-1] if server.jobs else {}
check("the message starts a job, in plan mode, on the chosen agent",
      len(server.jobs) == 1 and job.get("plan_mode") is True and job.get("agent_id") == "ag1",
      str(server.jobs))
check("carrying the line, what was run before it, and where this machine is",
      job.get("message", "").startswith(attacca.SHELL_CONTEXT + "\n$ ls\nnotes.txt\n\n"
                                        "refactor the parser\n\n" + attacca.PLAN_CONTEXT)
      and os.getcwd() in job.get("message", ""), job.get("message", "")[:300])
sent = [e["params"] for e in server.received
        if e.get("method") == "attacca_api.send_message"][before:]
check("nothing is sent but the decision, to the job's session",
      [(p["session_id"], p["message"]) for p in sent] == [("js1", attacca.PLAN_APPROVED)],
      str(sent))
check("the job's session is this terminal's now", config.ATTACCA_SESSION == "js1"
      and config.ATTACCA_JOB == "j1", f"{config.ATTACCA_SESSION} {config.ATTACCA_JOB}")
check("read from its first event, not from now",
      server.opened[-1] == {"session_id": "js1", "after": 0}, str(server.opened[-1]))
check("the plan is shown and Enter approves it", "Plan from Attacca" in shown
      and "plan approved" in shown and answer == "Starting.", shown[-500:])
check("and it is said that the earlier session stays behind",
      "a session of its own" in shown, shown[:300])
server.scripts["and the lexer"] = [delta("Ok."), event(90, "chat_agent", {"content": "Ok."})] + DONE
messages.append({"role": "user", "content": "and the lexer"})
answer, _ = quietly(lambda: run(attacca.run_turn(messages)))
sent = [e["params"] for e in server.received if e.get("method") == "attacca_api.send_message"]
check("what is typed next goes on in that session, not to a second job",
      len(server.jobs) == 1 and sent[-1]["session_id"] == "js1" and answer == "Ok.", str(sent[-1]))
config.SAVE_CHAT_HISTORY = True
sid = session.save_session(messages, None)
check("the job is saved with the session", session.load_session(sid).get("attacca_job") == "j1")
config.ATTACCA_JOB = ""
app._adopt_session(session.load_session(sid))
check("and comes back with it", config.ATTACCA_JOB == "j1")
config.SAVE_CHAT_HISTORY = False
attacca.plan_anew()
check("/planmode on, said again, plans the next thing too", config.ATTACCA_JOB == "")
attacca._account_seen = (attacca.link(), {"scopes": ["sessions:read", "sessions:write"]})
ok, shown = quietly(attacca.plans_here)
check("a pairing without the scope is told how to get it", ok is False
      and attacca.PLAN_SCOPE in shown and "/connect forget attacca" in shown, shown)
ok, shown = quietly(attacca.plans_here)
check("once", ok is False and shown == "")
attacca._account_seen = (attacca.link(), {})
check("a deployment that does not say plans the old way", quietly(attacca.plans_here)[0] is False)
attacca._account_seen = (None, {})
config.PLANMODE = False
attacca.forget_session()
check("a new conversation forgets the job", config.ATTACCA_JOB == "")

print("\n--- one-off requests are refused plainly, not sent to the agent ---")
try:
    run(providers.complete([{"role": "user", "content": "title this"}], max_tokens=20))
    check("providers.complete refuses", False)
except RuntimeError as error:
    check("providers.complete refuses, saying why", "own agent loop" in str(error), str(error))

print("\n--- a credential Attacca no longer accepts is dropped, with the fix ---")
attacca.disconnect()
providers.settings_for("attacca")["api_key"] = "zc_revoked"
try:
    attacca.link()
    check("a refused credential raises", False)
except attacca.NotPaired as error:
    check("a refused credential says to pair again", "/connect attacca" in str(error), str(error))
check("and is not kept", "api_key" not in providers.settings_for("attacca"))

server.server.shutdown()
print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("attacca checks passed")
