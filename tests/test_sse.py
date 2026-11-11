"""An event stream is read whether or not it came compressed.

Anthropic gzips `/v1/messages` when the client says it can take gzip, and
requests always says so. The reader went to `response.raw`, which is the body
as it came off the wire, so a reply that arrived in full was parsed as bytes
with no `data:` line in them: no text, no counts, no error - and the turn ended
in "the model reasoned but said nothing", twice. What is checked here is that a
compressed stream is decoded, that decoding it has not made the reader wait for
the end of the stream, and that the provider built on it answers.
"""
import asyncio
import http.server
import json
import os
import socketserver
import sys
import tempfile
import threading
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("AETHERIS_HOME", tempfile.mkdtemp(prefix="sse-home-"))

from aetheris import config          # noqa: E402
config.MCP_ENABLED = False
config.SAVE_CHAT_HISTORY = False
from aetheris import providers       # noqa: E402
from aetheris.sse import iter_sse    # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


def event(kind, payload):
    return f"event: {kind}\ndata: {json.dumps(payload)}\n\n".encode("utf-8")


ANTHROPIC_REPLY = [
    event("message_start", {"type": "message_start",
                            "message": {"usage": {"input_tokens": 11}}}),
    event("content_block_start", {"type": "content_block_start", "index": 0,
                                  "content_block": {"type": "text", "text": ""}}),
    event("content_block_delta", {"type": "content_block_delta", "index": 0,
                                  "delta": {"type": "text_delta", "text": "안녕하세요!"}}),
    event("content_block_delta", {"type": "content_block_delta", "index": 0,
                                  "delta": {"type": "text_delta", "text": " 무엇을 도와드릴까요?"}}),
    event("content_block_stop", {"type": "content_block_stop", "index": 0}),
    event("message_delta", {"type": "message_delta", "usage": {"output_tokens": 23}}),
    event("message_stop", {"type": "message_stop"}),
]

# Set by the test while it holds the stream open: the server sends the first
# event, then waits for the client to say it has it before sending the rest.
first_read = threading.Event()


class Handler(http.server.BaseHTTPRequestHandler):
    gzip = True
    hold_after_first = False

    def log_message(self, *args):
        pass

    def _serve(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        if self.gzip:
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        compressor = zlib.compressobj(wbits=31) if self.gzip else None
        for index, chunk in enumerate(ANTHROPIC_REPLY):
            if compressor:
                # A sync flush per event is what a streaming server does, and
                # what lets the client decode one before the next is written.
                chunk = compressor.compress(chunk) + compressor.flush(zlib.Z_SYNC_FLUSH)
            self.wfile.write(chunk)
            self.wfile.flush()
            if index == 0 and self.hold_after_first:
                first_read.wait(5)
        if compressor:
            self.wfile.write(compressor.flush())

    do_GET = _serve
    do_POST = _serve


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def serve(**options):
    handler = type("H", (Handler,), options)
    server = Server(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def kinds_from(url):
    import requests
    response = requests.get(url, stream=True, timeout=10)
    return response, [json.loads(data)["type"] for _event, data in iter_sse(response)]


expected = [json.loads(chunk.decode().split("data: ", 1)[1])["type"] for chunk in ANTHROPIC_REPLY]

print("--- the parser ---")
for label, gzip in (("a gzipped stream", True), ("a plain one", False)):
    server, url = serve(gzip=gzip)
    try:
        response, kinds = kinds_from(url)
        check(f"{label} is read event by event", kinds == expected,
              f"{response.headers.get('Content-Encoding')}: {kinds}")
    finally:
        server.shutdown()

print("\n--- still a stream ---")
first_read.clear()
server, url = serve(gzip=True, hold_after_first=True)
try:
    import requests
    response = requests.get(url, stream=True, timeout=10)
    reader = iter_sse(response)
    first = next(reader, None)
    first_read.set()
    check("the first gzipped event arrives while the server is still holding the rest",
          first is not None and json.loads(first[1])["type"] == "message_start",
          str(first)[:80])
    rest = [json.loads(data)["type"] for _event, data in reader]
    check("and the rest follow it", rest == expected[1:], str(rest))
finally:
    first_read.set()
    server.shutdown()

print("\n--- the provider on top of it ---")
server, url = serve(gzip=True)
try:
    provider = providers.AnthropicProvider({"model": "claude-test", "api_key": "k",
                                            "base_url": url})

    async def collect():
        return [chunk async for chunk in provider.stream([{"role": "user", "content": "안녕"}])]

    chunks = asyncio.run(collect())
    text = "".join(chunk.get("text", "") for chunk in chunks)
    done = chunks[-1] if chunks else {}
    check("a gzipped Anthropic reply comes through as text",
          text == "안녕하세요! 무엇을 도와드릴까요?", repr(text))
    check("with its token counts", done.get("prompt_tokens") == 11
          and done.get("completion_tokens") == 23, str(done))
finally:
    server.shutdown()


print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("sse checks passed")
