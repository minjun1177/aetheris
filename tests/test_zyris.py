"""The Zyris wire, against a server that speaks it the way the spec says.

Attacca itself cannot be in CI, so the far end here is a websocket server on a
loopback port, written from `docs/zyris-protocol.md` rather than from this
client - a test that checks the client against its own idea of the protocol
would pass however wrong that idea was. Each section is one thing the spec
says must happen: the handshake is msgpack, a request is answered by id, a
stream that skips a chunk is failed rather than delivered, a dropped
connection fails every call on it and is dialled again, and a credential that
is refused - at the door or later - stops the reconnecting for good.

The server genuinely runs on a thread of its own, so a frame from it is a
frame from somewhere else.
"""
import os
import queue
import struct
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    import msgpack
    from websockets.sync.server import serve
except ImportError:
    # Optional on purpose (`aetheris[attacca]`): a checkout without the extra
    # has nothing here to test, and failing would say the harness is broken.
    print('skipped: pip install "aetheris[attacca]" to test the Zyris link')
    sys.exit(0)

from aetheris import zyris            # noqa: E402

failures = []


def check(label, ok, extra=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  ({extra})" if extra and not ok else ""))
    if not ok:
        failures.append(label)


GOOD = "zc_test_credential"


def pack(envelope: dict) -> bytes:
    return bytes([0x00]) + msgpack.packb(envelope, use_bin_type=True)


def unpack(raw) -> dict:
    return msgpack.unpackb(bytes(raw[1:]), raw=False)


def chunk(stream_id: int, seq: int, item) -> bytes:
    return (bytes([0x01]) + struct.pack(">II", stream_id, seq)
            + msgpack.packb(item, use_bin_type=True))


class FakeServer:
    """A Zyris acceptor with just enough behaviour to be argued with."""

    def __init__(self):
        self.received = []            # every envelope from the client, in order
        self.hellos = []
        self.connections = 0
        self.to_client: queue.Queue = queue.Queue()   # frames the test pushes
        self.drop_next = None         # close code to end the current connection with
        self.server = serve(self.handle, "127.0.0.1", 0,
                            process_request=self.door)
        self.port = self.server.socket.getsockname()[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}/zyris/v1/ws"

    def door(self, connection, request):
        if request.headers.get("Authorization") != f"Bearer {GOOD}":
            return connection.respond(401, "unknown credential\n")
        return None

    def handle(self, ws):
        self.connections += 1
        hello = unpack(ws.recv())
        self.hellos.append(hello)
        ws.send(pack({"t": "hello_ack", "protocol": {"major": 1, "minor": 0},
                      "serialization": "msgpack", "conn_id": "c1",
                      "resume_token": "r1", "node_id": "n1",
                      "node": {"system": "laptop", "program": "aetheris",
                               "name": hello.get("node_name")},
                      "heartbeat": {"interval_s": 20, "timeout_s": 45},
                      "resumed": False, "features": ["cancel"]}))
        # What Attacca does first, every time (§5): offer its own capability.
        ws.send(pack({"t": "req", "id": 1, "method": "zyris.announce",
                      "params": {"capabilities": [{"name": "attacca_api",
                                                   "version": 1, "tools": []}]}}))
        while True:
            while not self.to_client.empty():
                ws.send(self.to_client.get())
            if self.drop_next is not None:
                code, self.drop_next = self.drop_next, None
                ws.close(code)
                return
            try:
                raw = ws.recv(timeout=0.05)
            except TimeoutError:
                continue
            except Exception:
                return
            envelope = unpack(raw)
            self.received.append(envelope)
            self.answer(ws, envelope)

    def answer(self, ws, env):
        if env.get("t") != "req":
            return
        method, rid = env.get("method"), env.get("id")
        if method == "zyris.announce":
            names = [c["name"] for c in env["params"]["capabilities"]]
            ws.send(pack({"t": "res", "id": rid,
                          "result": {"accepted": names, "rejected": []}}))
        elif method == "echo.say":
            ws.send(pack({"t": "res", "id": rid, "result": {"said": env["params"]}}))
        elif method == "fail.now":
            ws.send(pack({"t": "err", "id": rid, "error": {
                "code": "invalid_params", "message": "no such thing",
                "retriable": False}}))
        elif method == "slow.never":
            pass
        elif method == "attacca_api.turn_events":
            sid = env["stream"]["id"]
            ws.send(pack({"t": "res", "id": rid,
                          "result": {"session_id": "s1", "running": True}}))
            ws.send(chunk(sid, 0, {"type": "delta", "kind": "assistant", "text": "Hel"}))
            ws.send(chunk(sid, 1, {"type": "delta", "kind": "assistant", "text": "lo"}))
            ws.send(chunk(sid, 2, {"type": "status", "running": False}))
            ws.send(pack({"t": "s_end", "stream": sid}))
        elif method == "gap.stream":
            sid = env["stream"]["id"]
            ws.send(pack({"t": "res", "id": rid, "result": {}}))
            ws.send(chunk(sid, 0, {"n": 0}))
            ws.send(chunk(sid, 2, {"n": 2}))         # 1 never came

    def wait_for(self, predicate, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            found = [e for e in list(self.received) if predicate(e)]
            if found:
                return found[0]
            time.sleep(0.02)
        return None

    def close(self):
        self.server.shutdown()


server = FakeServer()

print("--- a credential that is refused never becomes a connection ---")
try:
    zyris.Link(server.url, "zc_wrong", node_name="x").start()
    check("a 401 at the door raises Unauthorized", False, "it connected")
except zyris.Unauthorized:
    check("a 401 at the door raises Unauthorized", True)

print("\n--- the handshake is msgpack, and says who is calling ---")
link = zyris.Link(server.url, GOOD, node_name="myrepo").start()
hello = server.hellos[-1]
check("it connected", link.ready)
check("the first frame was a hello", hello.get("t") == "hello", str(hello))
check("it asks for protocol 1", hello.get("protocol", {}).get("major") == 1)
check("it offers msgpack and nothing else", hello.get("serialization") == ["msgpack"])
check("it is a service node with the name it asked for",
      hello.get("kind") == "service" and hello.get("node_name") == "myrepo")
check("the node's address is the one the server gave back",
      link.address == "laptop/aetheris/myrepo", link.address)

deadline = time.time() + 3
while "attacca_api" not in link.server_capabilities and time.time() < deadline:
    time.sleep(0.02)
check("the server's own announcement is accepted",
      "attacca_api" in link.server_capabilities)
accepted = server.wait_for(lambda e: e.get("t") == "res" and e.get("id") == 1)
check("and answered by id with what was accepted",
      accepted and accepted["result"]["accepted"] == ["attacca_api"], str(accepted))

print("\n--- a call is answered by its id ---")
check("a result comes back", link.call("echo.say", {"x": 1}) == {"said": {"x": 1}})
try:
    link.call("fail.now")
    check("an err envelope raises ZyrisError", False)
except zyris.ZyrisError as error:
    check("an err envelope raises ZyrisError with its code",
          error.code == "invalid_params" and not error.retriable, str(error))
try:
    link.call("slow.never", timeout=0.3)
    check("an unanswered call times out", False)
except zyris.ZyrisError as error:
    check("an unanswered call times out, retriably", error.code == "timeout"
          and error.retriable, str(error))
    check("and the far end is told to stop",
          server.wait_for(lambda e: e.get("t") == "cancel") is not None)

print("\n--- announcing is a replacement, and is answered ---")
tools = [{"name": "aetheris", "version": 1, "tools": [
    {"name": "read_file", "description": "Read a file.", "transfer": "unary",
     "request_schema": {"type": "object"}, "response_schema": {"type": "object"}}]}]
result = link.announce(tools)
check("the capability was accepted", result == {"accepted": ["aetheris"], "rejected": []},
      str(result))

print("\n--- a stream: a head, items in order, then its end ---")
stream = link.open_stream("attacca_api.turn_events", {"session_id": "s1", "after": None})
check("the head arrives with the response", stream.head == {"session_id": "s1", "running": True},
      str(stream.head))
items = []
while True:
    item = stream.get(timeout=3)
    if item is None:
        break
    items.append(item)
check("every item arrives, in order", [i.get("text") or i.get("type") for i in items]
      == ["Hel", "lo", "status"], str(items))
req = server.wait_for(lambda e: e.get("method") == "attacca_api.turn_events")
check("the stream id is odd - the dialer's half (§4)", req["stream"]["id"] % 2 == 1,
      str(req.get("stream")))
credit = server.wait_for(lambda e: e.get("t") == "s_credit")
check("credit is handed back for what was read", credit is not None
      and credit["stream"] == req["stream"]["id"] and credit["bytes"] > 0, str(credit))

print("\n--- a chunk missing from the middle fails the stream ---")
stream = link.open_stream("gap.stream")
check("the chunk before the gap is delivered", stream.get(timeout=3) == {"n": 0})
try:
    stream.get(timeout=3)
    check("the chunk after the gap is not", False)
except zyris.ZyrisError as error:
    check("the chunk after the gap is not: stream_lagged", error.code == "stream_lagged",
          str(error))
check("and the server is told to stop sending",
      server.wait_for(lambda e: e.get("t") == "s_cancel") is not None)

print("\n--- the server calls this machine's tools ---")
server.to_client.put(pack({"t": "req", "id": 900, "method": "aetheris.read_file",
                           "params": {"path": "a.txt"}}))
request = zyris.take_request(link, timeout=3)
check("the call reaches the inbox", request is not None and request.method
      == "aetheris.read_file", str(request and request.method))
check("split into capability and tool", request and request.capability == "aetheris"
      and request.tool == "read_file")
request.respond({"output": "hello"})
answer = server.wait_for(lambda e: e.get("t") == "res" and e.get("id") == 900)
check("the answer goes back under the call's id", answer and answer["result"]
      == {"output": "hello"}, str(answer))
request.respond({"output": "twice"})
time.sleep(0.2)
check("and only once", len([e for e in server.received if e.get("id") == 900]) == 1)

server.to_client.put(pack({"t": "req", "id": 901, "method": "aetheris.run_cmd",
                           "params": {"command": "sleep 100"}}))
request = zyris.take_request(link, timeout=3)
server.to_client.put(pack({"t": "cancel", "id": 901}))
deadline = time.time() + 3
while not request.cancelled.is_set() and time.time() < deadline:
    time.sleep(0.02)
check("a cancel reaches a call already taken off the inbox", request.cancelled.is_set())
request.respond({"output": "ran anyway"})
answer = server.wait_for(lambda e: e.get("id") == 901)
check("and it is still answered, once, as canceled (§2)",
      answer and answer["t"] == "err" and answer["error"]["code"] == "canceled", str(answer))

server.to_client.put(pack({"t": "req", "id": 902, "method": "zyris.mystery", "params": {}}))
answer = server.wait_for(lambda e: e.get("id") == 902)
check("a protocol method this node does not do is refused, not queued",
      answer and answer["error"]["code"] == "method_not_found"
      and zyris.take_request(link) is None, str(answer))

print("\n--- a dropped connection fails what was in flight, and comes back ---")
in_flight = {}


def wait_on_a_call_that_will_never_be_answered():
    try:
        link.call("slow.never", timeout=20)
        in_flight["error"] = None
    except zyris.ZyrisError as error:
        in_flight["error"] = error


server.received.clear()
waiter = threading.Thread(target=wait_on_a_call_that_will_never_be_answered)
waiter.start()
server.wait_for(lambda e: e.get("method") == "slow.never")
before = server.connections
server.received.clear()
server.drop_next = 1011
waiter.join(timeout=10)
error = in_flight.get("error")
check("the call waiting on the old connection fails at once, as connection_lost",
      error is not None and error.code == "connection_lost" and error.retriable,
      str(error))
deadline = time.time() + 10
while (server.connections == before or not link.ready) and time.time() < deadline:
    time.sleep(0.05)
check("it dialled again on its own", server.connections == before + 1,
      f"{before} → {server.connections}")
reannounced = server.wait_for(lambda e: e.get("method") == "zyris.announce")
check("and announced its tools again - a new connection has none (§3.4)",
      reannounced and reannounced["params"]["capabilities"][0]["name"] == "aetheris",
      str(reannounced))
check("calls work on the new connection", link.call("echo.say", {"y": 2}) == {"said": {"y": 2}})

server.to_client.put(pack({"t": "req", "id": 903, "method": "aetheris.read_file",
                           "params": {}}))
stale = zyris.take_request(link, timeout=3)
before = server.connections
server.received.clear()
server.drop_next = 1011
deadline = time.time() + 10
while (server.connections == before or not link.ready) and time.time() < deadline:
    time.sleep(0.05)
stale.respond({"output": "too late"})
time.sleep(0.2)
check("an answer for a call from a dead connection is dropped, not sent to the new one",
      not [e for e in server.received if e.get("id") == 903])

print("\n--- a credential revoked mid-connection stops it for good ---")
server.drop_next = 4401
deadline = time.time() + 5
while link.state != "unauthorized" and time.time() < deadline:
    time.sleep(0.05)
check("close 4401 reads as Unauthorized", link.state == "unauthorized", link.state)
connections = server.connections
time.sleep(1.5)
check("and it does not dial again", server.connections == connections)
try:
    link.call("echo.say")
    check("a call after that says why", False)
except zyris.Unauthorized:
    check("a call after that says why: Unauthorized", True)
link.close()

print("\n--- closing says goodbye ---")
link = zyris.Link(server.url, GOOD, node_name="bye").start()
server.received.clear()
link.close()
check("a zyris.closing note goes first (§3.3)",
      server.wait_for(lambda e: e.get("t") == "note"
                      and e.get("method") == "zyris.closing") is not None)
check("and nothing more can be asked", not link.ready)

server.close()

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("zyris link checks passed")
