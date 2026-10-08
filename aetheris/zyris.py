"""Zyris - one websocket over which this machine offers tools and calls back.

The wire half of `/connect attacca`, and nothing about Attacca beyond the fact
that it is where the socket goes: the normative reference is
`docs/zyris-protocol.md` in github.com/attacca-cc/zyris-protocol, and section
numbers below are that document's.

What a `Link` is, in one paragraph. A websocket is dialled with a bearer
credential, both ends say hello, and from then on either side may call the
other: Attacca calls the tools this machine announced, and this machine calls
`attacca_api.*` back. Calls are `req`/`res` envelopes matched by id. A call
that streams (`turn_events`) answers at once with a head and then sends one
item per STREAM_DATA chunk until it ends.

Why the socket has a thread of its own. Every tool here runs on the main
thread, because that is where an approval prompt can read a keyboard - and a
prompt can sit there for as long as the person is making a cup of tea. The
far end closes a connection that stops answering pings after 45 seconds. So
the socket is read on a thread that never waits on a person, and what it
receives is handed over through queues: replies to the caller blocked on them,
tool requests to `inbox`, stream items to the stream's own queue.

Optional: `pip install "aetheris[attacca]"` brings `websockets` and `msgpack`.
Nothing imports this module unless Attacca is the provider, so a harness that
only ever talks to Ollama never needs either.
"""
import itertools
import queue
import random
import struct
import threading
import time

from aetheris import __version__

TAG_CONTROL = 0x00          # a msgpack envelope (§1)
TAG_STREAM = 0x01           # stream_id u32 BE, chunk_seq u32 BE, payload (§1)
PROTOCOL_MAJOR = 1

CALL_TIMEOUT = 30.0         # a call that is not answered by then is not coming
DIAL_TIMEOUT = 15.0
# The far end's ceiling is 8 MiB of CONTROL frame (§1 defaults); anything the
# library would refuse below that is a reply lost for no reason.
MAX_FRAME = 16 * 1024 * 1024
BACKOFF_FIRST, BACKOFF_CEILING = 1.0, 30.0

# Close codes that no reconnect can fix (§10). 4401 is a credential revoked
# from the web UI while connected, which is the same news as a 401 at dial.
CLOSE_REVOKED, CLOSE_VERSION = 4401, 4400

INSTALL_HINT = 'pip install "aetheris[attacca]"'


class MissingDependency(RuntimeError):
    """`websockets` or `msgpack` is not installed - said with the fix in it."""


def _libraries():
    """The two optional packages, or one sentence saying how to get them."""
    try:
        import msgpack
        from websockets.sync import client
        from websockets import exceptions
    except ImportError as error:
        raise MissingDependency(
            f"Attacca needs two packages the base install leaves out "
            f"({error.name}). Install them with: {INSTALL_HINT}") from None
    return msgpack, client, exceptions


class ZyrisError(Exception):
    """An `err` envelope, or a failure that has the same meaning (§2.1)."""

    def __init__(self, code: str, message: str = "", retriable: bool = False,
                 data=None):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
        self.retriable = retriable
        self.data = data


class Unauthorized(ZyrisError):
    """The credential was refused, at dial (HTTP 401) or later (close 4401).

    There is no separate "enroll again" signal in this protocol (§3.1): an
    unknown, mistyped and revoked credential all look exactly like this, and
    the answer to every one of them is a fresh code.
    """

    def __init__(self, message: str = "the credential was refused"):
        super().__init__("unauthorized", message)


def _lost() -> ZyrisError:
    return ZyrisError("connection_lost", "the connection to Attacca dropped",
                      retriable=True)


class Request:
    """A call the far end made to one of this machine's tools.

    `respond` exactly once. A `cancel` from the far end only sets `cancelled`:
    the request still has to be answered (§2), and a tool that already ran
    cannot be un-run, so what is done about it is the caller's decision.
    """

    def __init__(self, link: "Link", request_id: int, method: str, params,
                 generation: int):
        self.link = link
        self.id = request_id
        self.method = method
        self.params = params if isinstance(params, dict) else {}
        self.generation = generation
        self.cancelled = threading.Event()
        self.answered = False

    @property
    def capability(self) -> str:
        return self.method.partition(".")[0]

    @property
    def tool(self) -> str:
        return self.method.partition(".")[2]

    def respond(self, result=None, error: ZyrisError | None = None) -> None:
        self.link.respond(self, result=result, error=error)


class Stream:
    """The far end's half of a `uni_stream` call: a head, then items (§4).

    `get` returns the next item, `None` once the stream ended cleanly, and
    raises the `ZyrisError` it ended with otherwise - a dropped connection
    included, which kills every stream on it (§3.4). Resuming is the
    caller's business: `turn_events` resumes by cursor.
    """

    def __init__(self, link: "Link", stream_id: int):
        self.link = link
        self.id = stream_id
        self.head = None
        self._items: queue.Queue = queue.Queue()
        self._next_seq = 0
        self.finished = False

    def get(self, timeout: float | None = None):
        """The next item; `None` at a clean end. Raises `queue.Empty` on timeout."""
        kind, value = self._items.get(timeout=timeout)
        if kind == "item":
            return value
        self.finished = True
        if kind == "error":
            raise value
        return None

    def cancel(self) -> None:
        """Stop the far end sending (§4.2). Safe on a stream that already ended."""
        if not self.finished:
            self.finished = True
            self.link._send_envelope({"t": "s_cancel", "stream": self.id},
                                     quiet=True)
        self.link._forget_stream(self.id)

    # -- the reader thread's half --------------------------------------------

    def _chunk(self, seq: int, payload: bytes, unpack) -> None:
        if seq != self._next_seq:
            # Bytes past a gap are never delivered (§1): an item missing from
            # the middle of a turn is a reply with a hole in it, said
            # confidently. The caller resumes from its last cursor instead.
            self._end("error", ZyrisError(
                "stream_lagged", f"chunk {seq} arrived where {self._next_seq} "
                "was expected", retriable=True))
            self.link._send_envelope({"t": "s_cancel", "stream": self.id},
                                     quiet=True)
            return
        self._next_seq += 1
        try:
            item = unpack(payload)
        except Exception as error:
            self._end("error", ZyrisError("parse_error", str(error)))
            return
        self._items.put(("item", item))
        # Credit goes back as soon as the bytes are off the socket. Holding it
        # until the main thread has drawn them would bound memory a little
        # tighter, but these are lines of a reply, and a turn that stalled
        # because the screen was busy redrawing is the worse failure.
        self.link._send_envelope(
            {"t": "s_credit", "stream": self.id, "bytes": len(payload)},
            quiet=True)

    def _end(self, kind: str, value=None) -> None:
        if not self.finished:
            self._items.put((kind, value))


class Link:
    """One live connection to a Zyris server, kept up until `close`.

    `start` makes the first connection on the calling thread, so a refused
    credential or a server that is not there is that call's exception. After
    that the reader thread owns the socket: it reconnects on its own with
    backoff, and re-announces this machine's tools on every new connection,
    because a connection that did not resume has no announcement (§3.4).
    """

    def __init__(self, url: str, credential: str, *, node_name: str,
                 kind: str = "service", agent: str = ""):
        self.url = url
        self._credential = credential
        self.node_name = node_name
        self.kind = kind
        self.agent = agent or f"aetheris/{__version__}"

        self.inbox: queue.Queue = queue.Queue()      # Request, for the main thread
        self.hello_ack: dict = {}
        self.server_capabilities: dict = {}           # what the far end announced
        self.capabilities: list = []                  # what this end announces
        self.state = "closed"                         # connecting | ready | closed | unauthorized
        self.last_error: Exception | None = None
        self.generation = 0                           # bumped on every new connection

        self._ws = None
        self._send_lock = threading.Lock()
        self._lock = threading.Lock()
        self._pending: dict = {}                      # request id -> [Event, result, error]
        self._streams: dict = {}                      # stream id -> Stream
        self._open: dict = {}                         # inbound id -> Request, until answered
        self._request_ids = itertools.count(1)
        self._stream_ids = itertools.count(1, 2)     # the dialer's ids are odd (§4)
        self._closing = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> "Link":
        """Connect, or raise why not. Then keep connected in the background."""
        self._dial()
        self._thread = threading.Thread(target=self._run, name="aetheris-zyris",
                                        daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        """Say goodbye properly (§3.3) and stop reconnecting."""
        self._closing.set()
        ws = self._ws
        if ws is not None:
            self._send_envelope({"t": "note", "method": "zyris.closing",
                                 "params": {"reason": "aetheris is closing"}},
                                quiet=True)
            try:
                ws.close(1000)
            except Exception:
                pass
        self._fail_everything(ZyrisError("connection_lost", "closed"))
        self.state = "closed"

    @property
    def ready(self) -> bool:
        return self.state == "ready"

    @property
    def address(self) -> str:
        """`system/program/node` as the server placed this connection (§3.2)."""
        node = self.hello_ack.get("node") or {}
        parts = [node.get("system"), node.get("program"), node.get("name")]
        return "/".join(p for p in parts if p) or self.node_name

    def wait_ready(self, timeout: float) -> bool:
        return self._ready.wait(timeout)

    # -- calling the far end -------------------------------------------------

    def call(self, method: str, params: dict | None = None,
             timeout: float = CALL_TIMEOUT):
        """Call a tool the far end announced and wait for its result."""
        waiter = self._request(method, params or {})
        return self._await(waiter, timeout, method)

    def open_stream(self, method: str, params: dict | None = None,
                    timeout: float = CALL_TIMEOUT) -> Stream:
        """Call a `uni_stream` tool. The head is on `.head`; items follow."""
        stream_id = next(self._stream_ids)
        stream = Stream(self, stream_id)
        with self._lock:
            self._streams[stream_id] = stream
        try:
            waiter = self._request(method, params or {},
                                   stream={"id": stream_id})
            stream.head = self._await(waiter, timeout, method)
        except BaseException:
            self._forget_stream(stream_id)
            raise
        return stream

    def announce(self, capabilities: list, timeout: float = CALL_TIMEOUT):
        """Replace everything this machine offers (§5) - full, not a diff."""
        self.capabilities = list(capabilities)
        return self.call("zyris.announce", {"capabilities": self.capabilities},
                         timeout=timeout)

    def respond(self, request: Request, result=None,
                error: ZyrisError | None = None) -> None:
        """Answer an inbound call, once. A reply for a dead connection is dropped:
        the far end already failed that call with `connection_lost` (§3.4)."""
        if request.answered:
            return
        request.answered = True
        with self._lock:
            self._open.pop(request.id, None)
        if request.generation != self.generation:
            return
        if error is None and request.cancelled.is_set():
            error = ZyrisError("canceled", "the caller cancelled this call")
        if error is not None:
            self._send_envelope({"t": "err", "id": request.id, "error": {
                "code": error.code, "message": error.message,
                "retriable": bool(error.retriable), "data": error.data}},
                quiet=True)
        else:
            self._send_envelope({"t": "res", "id": request.id,
                                 "result": result}, quiet=True)

    # -- internals: requests ---------------------------------------------------

    def _request(self, method: str, params: dict, stream: dict | None = None):
        if self._closing.is_set():
            raise ZyrisError("connection_lost", "the link is closed")
        if self.state == "unauthorized":
            raise Unauthorized()
        request_id = next(self._request_ids)
        waiter = [threading.Event(), None, None]
        with self._lock:
            self._pending[request_id] = waiter
        envelope = {"t": "req", "id": request_id, "method": method,
                    "params": params}
        if stream is not None:
            envelope["stream"] = stream
        try:
            self._send_envelope(envelope)
        except BaseException:
            with self._lock:
                self._pending.pop(request_id, None)
            raise
        waiter.append(request_id)
        return waiter

    def _await(self, waiter, timeout: float, method: str):
        event, _, _, request_id = waiter
        if not event.wait(timeout):
            with self._lock:
                self._pending.pop(request_id, None)
            # Best-effort (§2): the far end still answers, and that answer is
            # dropped because nobody is waiting for it any more.
            self._send_envelope({"t": "cancel", "id": request_id}, quiet=True)
            raise ZyrisError("timeout", f"{method} did not answer within "
                             f"{timeout:.0f}s", retriable=True)
        if waiter[2] is not None:
            raise waiter[2]
        return waiter[1]

    def _settle(self, request_id, result=None, error=None) -> None:
        with self._lock:
            waiter = self._pending.pop(request_id, None)
        if waiter is None:
            return                     # timed out already; nobody to tell
        waiter[1], waiter[2] = result, error
        waiter[0].set()

    def _forget_stream(self, stream_id: int) -> None:
        with self._lock:
            self._streams.pop(stream_id, None)

    def _fail_everything(self, error: ZyrisError) -> None:
        """Every call and stream on a connection dies with it (§3.4)."""
        with self._lock:
            pending, self._pending = self._pending, {}
            streams, self._streams = self._streams, {}
            # Calls made on a dead connection can no longer be answered; the far
            # end has already failed them with `connection_lost` on its side.
            self._open = {}
        for waiter in pending.values():
            waiter[1], waiter[2] = None, error
            waiter[0].set()
        for stream in streams.values():
            stream._end("error", error)

    # -- internals: the wire ---------------------------------------------------

    def _send_envelope(self, envelope: dict, quiet: bool = False) -> None:
        msgpack, _, _ = _libraries()
        frame = bytes([TAG_CONTROL]) + msgpack.packb(envelope, use_bin_type=True)
        ws = self._ws
        try:
            if ws is None:
                raise _lost()
            with self._send_lock:
                ws.send(frame)
        except ZyrisError:
            if not quiet:
                raise
        except Exception as error:
            if not quiet:
                raise ZyrisError("connection_lost", str(error),
                                 retriable=True) from None

    def _dial(self) -> None:
        """Open the socket and shake hands (§3.1, §3.2). Raises on failure."""
        msgpack, client, exceptions = _libraries()
        self.state = "connecting"
        self._ready.clear()
        try:
            ws = client.connect(
                self.url,
                additional_headers={"Authorization": f"Bearer {self._credential}"},
                user_agent_header=self.agent,
                open_timeout=DIAL_TIMEOUT,
                max_size=MAX_FRAME,
                compression=None)
        except exceptions.InvalidStatus as error:
            status = getattr(error.response, "status_code", 0)
            if status == 401:
                self.state = "unauthorized"
                raise Unauthorized() from None
            self.state = "closed"
            raise ZyrisError("connection_lost", f"the server answered HTTP "
                             f"{status} instead of opening a connection",
                             retriable=True) from None
        except Exception as error:
            self.state = "closed"
            raise ZyrisError("connection_lost", f"could not reach {self.url}: "
                             f"{error}", retriable=True) from None

        hello = {
            "t": "hello",
            "protocol": {"major": PROTOCOL_MAJOR, "minors_supported": [0]},
            # msgpack only. The handshake is msgpack whatever is negotiated
            # (§3.2), so offering JSON as well would buy a second codec and
            # nothing else.
            "serialization": ["msgpack"],
            "agent": self.agent,
            "kind": self.kind,
            "node_name": self.node_name,
            "features": ["cancel"],
        }
        try:
            ws.send(bytes([TAG_CONTROL]) + msgpack.packb(hello, use_bin_type=True))
            raw = ws.recv(timeout=DIAL_TIMEOUT)
        except Exception as error:
            self._abandon(ws)
            self.state = "closed"
            raise ZyrisError("connection_lost", f"the handshake did not finish: "
                             f"{error}", retriable=True) from None
        ack = self._decode_control(raw, msgpack)
        if not isinstance(ack, dict) or ack.get("t") != "hello_ack":
            self._abandon(ws)
            self.state = "closed"
            detail = (ack or {}).get("error", {}) if isinstance(ack, dict) else {}
            raise ZyrisError(detail.get("code") or "unsupported_version",
                             detail.get("message") or "the server did not "
                             "accept this handshake")

        with self._lock:
            self._ws = ws
            self.generation += 1
        self.hello_ack = ack
        self.state = "ready"
        self._ready.set()

    @staticmethod
    def _decode_control(raw, msgpack):
        if isinstance(raw, (bytes, bytearray)) and raw and raw[0] == TAG_CONTROL:
            return msgpack.unpackb(bytes(raw[1:]), raw=False)
        return None

    @staticmethod
    def _abandon(ws) -> None:
        try:
            ws.close()
        except Exception:
            pass

    def _run(self) -> None:
        """The reader thread: read until the socket dies, then dial again."""
        msgpack, _, exceptions = _libraries()
        delay = BACKOFF_FIRST
        first = True
        while not self._closing.is_set():
            if not first:
                try:
                    self._dial()
                except Unauthorized as error:
                    self.last_error = error
                    self._fail_everything(error)
                    return
                except ZyrisError as error:
                    self.last_error = error
                    # Jittered, so a server back from a restart is not met by
                    # every node it had at the same instant.
                    time.sleep(delay * random.uniform(0.5, 1.0))
                    delay = min(delay * 2, BACKOFF_CEILING)
                    continue
                delay = BACKOFF_FIRST
                if self.capabilities:
                    # Not `announce()`: that waits for an answer, and the
                    # answer is read by this very thread.
                    self._send_envelope({
                        "t": "req", "id": next(self._request_ids),
                        "method": "zyris.announce",
                        "params": {"capabilities": self.capabilities}},
                        quiet=True)
            first = False
            ws = self._ws
            try:
                while not self._closing.is_set():
                    raw = ws.recv()
                    self._handle(raw, msgpack)
            except exceptions.ConnectionClosed as closed:
                code = getattr(getattr(closed, "rcvd", None), "code", None)
                if code == CLOSE_REVOKED:
                    self.state = "unauthorized"
                    self.last_error = Unauthorized("the credential was revoked")
                    self._fail_everything(self.last_error)
                    return
                if code == CLOSE_VERSION:
                    self.state = "closed"
                    self.last_error = ZyrisError("unsupported_version")
                    self._fail_everything(self.last_error)
                    return
            except Exception as error:      # never let the reader die silently
                self.last_error = error
            with self._lock:
                self._ws = None
            self.state = "closed"
            self._ready.clear()
            self._fail_everything(_lost())

    def _handle(self, raw, msgpack) -> None:
        if isinstance(raw, str):
            return                          # JSON mode was never offered
        if not raw:
            return
        tag = raw[0]
        if tag == TAG_STREAM:
            if len(raw) < 9:
                return
            stream_id, seq = struct.unpack(">II", bytes(raw[1:9]))
            with self._lock:
                stream = self._streams.get(stream_id)
            if stream is not None:
                stream._chunk(seq, bytes(raw[9:]),
                              lambda b: msgpack.unpackb(b, raw=False))
            return
        if tag != TAG_CONTROL:
            return
        try:
            envelope = msgpack.unpackb(bytes(raw[1:]), raw=False)
        except Exception:
            return
        if isinstance(envelope, dict):
            self._envelope(envelope)

    def _envelope(self, envelope: dict) -> None:
        kind = envelope.get("t")
        if kind == "res":
            self._settle(envelope.get("id"), result=envelope.get("result"))
        elif kind == "err":
            detail = envelope.get("error") or {}
            self._settle(envelope.get("id"), error=ZyrisError(
                str(detail.get("code") or "internal"),
                str(detail.get("message") or ""),
                bool(detail.get("retriable")), detail.get("data")))
        elif kind == "req":
            self._inbound(envelope)
        elif kind == "cancel":
            # Waiting in the inbox or already running on the main thread - the
            # same flag either way, and the same single answer still owed.
            with self._lock:
                request = self._open.get(envelope.get("id"))
            if request is not None:
                request.cancelled.set()
        elif kind == "s_end":
            with self._lock:
                stream = self._streams.pop(envelope.get("stream"), None)
            if stream is not None:
                stream._end("end", envelope.get("trailer"))
        elif kind == "s_err":
            with self._lock:
                stream = self._streams.pop(envelope.get("stream"), None)
            if stream is not None:
                detail = envelope.get("error") or {}
                stream._end("error", ZyrisError(
                    str(detail.get("code") or "internal"),
                    str(detail.get("message") or ""),
                    bool(detail.get("retriable"))))
        # `prog`, `note` (`zyris.closing` included - the socket closing is what
        # actually ends things) and `s_credit` - this end never sends stream
        # data, so it holds no credit to be topped up - need nothing.

    def _inbound(self, envelope: dict) -> None:
        method = str(envelope.get("method") or "")
        request = Request(self, envelope.get("id"), method,
                          envelope.get("params"), self.generation)
        if method == "zyris.announce":
            # Attacca announces `attacca_api` to every node straight after the
            # handshake (§5). Accept all of it: what can actually be called is
            # decided by the scopes this credential was granted, server-side.
            offered = (request.params or {}).get("capabilities") or []
            names = []
            for capability in offered:
                if isinstance(capability, dict) and capability.get("name"):
                    self.server_capabilities[capability["name"]] = capability
                    names.append(capability["name"])
            self.respond(request, {"accepted": names, "rejected": []})
            return
        if method.startswith(("zyris.", "webrtc.")):
            self.respond(request, error=ZyrisError(
                "method_not_found", f"{method} is not something this node does"))
            return
        with self._lock:
            self._open[request.id] = request
        self.inbox.put(request)


def take_request(link: Link | None, timeout: float = 0.0) -> Request | None:
    """The next tool call waiting on `link`, or None. Never blocks past `timeout`."""
    if link is None:
        return None
    try:
        if timeout <= 0:
            return link.inbox.get_nowait()
        return link.inbox.get(timeout=timeout)
    except queue.Empty:
        return None


if __name__ == "__main__":
    print("This file can not run directly.")
