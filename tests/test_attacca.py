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
                result = {"id": f"s{len(self.sessions_created)}", "running": False}
            elif method == "attacca_api.turn_events":
                stream_id, seq = env["stream"]["id"], 0
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
                continue
            elif method == "attacca_api.send_message":
                self.marks["sent"] = time.time()
                steps = steps + [("item", {"type": "status", "running": True}),
                                 event(1, "chat_user", {"content": params["message"]})] \
                    + list(self.scripts.get(params["message"], []))
                result = None
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
check("as the program 'aetheris', for exactly the four scopes it uses",
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

print("\n--- the session follows the conversation ---")
from aetheris import session            # noqa: E402
config.SAVE_CHAT_HISTORY = True
config.SESSION_DIR = tempfile.mkdtemp(prefix="attacca-sessions-")
sid = session.save_session(messages, None)
saved = session.load_session(sid)
check("saved with the session file", saved.get("attacca_session") == "s1")
config.ATTACCA_SESSION = ""
plain = session.save_session([{"role": "system", "content": "x"}], None)
check("and absent from one that never used Attacca",
      "attacca_session" not in session.load_session(plain))
check("live state, not a setting: /set does not offer it",
      "ATTACCA_SESSION" not in config.settable())
config.SAVE_CHAT_HISTORY = False

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
