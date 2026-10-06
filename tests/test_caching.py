"""Hosted prompt caching: the breakpoints out, and the hit counts back.

Every request re-sends about 6,000 tokens of system prompt and tool schemas
that never change. Against Ollama that is re-counted and not re-computed;
against a hosted API it is billed every time.

The three hosted providers split two ways. Anthropic caches nothing without a
`cache_control` breakpoint, so that is the only one that needed a request
change. OpenAI and Gemini cache automatically and needed only to be *read* -
which is the part that matters most here, because caching is a prefix match
and a byte that moves invalidates it silently, as a bill rather than an error.

So the checks are: the breakpoints land where they should, the counters come
back, and the prefix this harness builds is one that can actually be cached.
"""
import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aetheris import config
config.MCP_ENABLED = False
config.SAVE_CHAT_HISTORY = False

from aetheris import providers, systemprompt, toolspec       # noqa: E402

failures = []
seen = {}


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _sse(self, events):
        body = "".join(f"data: {json.dumps(p)}\n\n" for p in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        seen["path"] = self.path

        if "/v1/messages" in self.path:                          # Anthropic
            return self._sse([
                {"type": "message_start", "message": {"usage": {
                    "input_tokens": 40,
                    "cache_read_input_tokens": 5_900,
                    "cache_creation_input_tokens": 120}}},
                {"type": "content_block_delta",
                 "delta": {"type": "text_delta", "text": "네."}},
                {"type": "message_delta", "usage": {"output_tokens": 7}},
            ])
        if "chat/completions" in self.path:                      # OpenAI
            return self._sse([
                {"choices": [{"delta": {"content": "네."}}]},
                {"choices": [], "usage": {
                    "prompt_tokens": 6_040, "completion_tokens": 7,
                    "prompt_tokens_details": {"cached_tokens": 5_888}}},
            ])
        return self._sse([                                       # Gemini
            {"candidates": [{"content": {"parts": [{"text": "네."}]}}],
             "usageMetadata": {"promptTokenCount": 6_040, "candidatesTokenCount": 7,
                               "cachedContentTokenCount": 5_888}},
        ])


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{server.server_address[1]}"


async def drain(provider, messages, tools=None):
    events = []
    async for chunk in provider.stream(messages, tools=tools):
        events.append(chunk)
    return events


CONVERSATION = [
    {"role": "system", "content": "SYSTEM PROMPT " * 200},
    {"role": "user", "content": "게임 만들어줘"},
    {"role": "assistant", "content": "<tool_call>\n{}\n</tool_call>"},
    {"role": "user", "content": "[Tool Result for 'read_file']:\nx"},
]
SCHEMAS = toolspec.native_schema()

# ---------------------------------------------------------------------------
print("--- Anthropic: nothing is cached without a breakpoint ---")
anthropic = providers.AnthropicProvider({"api_key": "k", "base_url": base, "model": "m"})
events = asyncio.run(drain(anthropic, CONVERSATION, SCHEMAS))
body = seen["body"]

check("the system prompt is sent as a content block",
      isinstance(body.get("system"), list), str(type(body.get("system"))))
check("carrying a cache_control breakpoint",
      body["system"][0].get("cache_control") == {"type": "ephemeral"},
      str(body["system"][0].get("cache_control")))
check("and the text is the system prompt itself",
      body["system"][0]["text"] == CONVERSATION[0]["content"].strip())
# Render order is tools -> system -> messages, so the marker on the last
# system block covers the tool schemas too - which is two thirds of the
# fixed cost. That is why there is no separate breakpoint on `tools`.
check("the tools ride in front of it, uncluttered",
      isinstance(body.get("tools"), list) and "cache_control" not in body["tools"][0],
      str(sorted(body["tools"][0])))

last = body["messages"][-1]
check("the end of the conversation is a second read point",
      isinstance(last["content"], list)
      and last["content"][0].get("cache_control") == {"type": "ephemeral"},
      str(last["content"])[:80])
check("with the text intact",
      last["content"][0]["text"].endswith("[Tool Result for 'read_file']:\nx"),
      last["content"][0]["text"][-40:])
check("earlier messages are left as plain strings",
      all(isinstance(m["content"], str) for m in body["messages"][:-1]))
check("at most four breakpoints are ever used",
      json.dumps(body).count('"cache_control"') <= 4,
      str(json.dumps(body).count('"cache_control"')))

# The conversation this harness holds must not grow an Anthropic-shaped
# content block: history is plain text for every provider (invariant 5.3).
check("and the harness's own history is untouched",
      all(isinstance(m["content"], str) for m in CONVERSATION))

done = events[-1]
check("a cache read is reported back", done.get("cached_tokens") == 5_900,
      str(done.get("cached_tokens")))
# Anthropic's `input_tokens` counts only what was NOT cached. Reporting it
# raw would show a prompt that suddenly shrank by 6,000 tokens the moment
# caching started working.
check("and the prompt total still counts the whole prompt",
      done["prompt_tokens"] == 40 + 5_900 + 120, str(done["prompt_tokens"]))

# ---------------------------------------------------------------------------
print("\n--- OpenAI and Gemini: automatic, so only the reading was missing ---")
openai = providers.OpenAIProvider({"api_key": "k", "base_url": base, "model": "m"})
events = asyncio.run(drain(openai, CONVERSATION, SCHEMAS))
check("OpenAI is sent no cache directive",
      "cache_control" not in json.dumps(seen["body"]))
check("but its cached_tokens is read", events[-1].get("cached_tokens") == 5_888,
      str(events[-1].get("cached_tokens")))
check("and prompt_tokens is left as it came",
      events[-1]["prompt_tokens"] == 6_040, str(events[-1]["prompt_tokens"]))

gemini = providers.GeminiProvider({"api_key": "k", "base_url": base, "model": "m"})
events = asyncio.run(drain(gemini, CONVERSATION, SCHEMAS))
check("Gemini is sent no cache directive",
      "cache_control" not in json.dumps(seen["body"]))
check("its cachedContentTokenCount is read",
      events[-1].get("cached_tokens") == 5_888, str(events[-1].get("cached_tokens")))
check("and promptTokenCount already included it, so it is not added twice",
      events[-1]["prompt_tokens"] == 6_040, str(events[-1]["prompt_tokens"]))

# ---------------------------------------------------------------------------
print("\n--- the prefix this harness builds is one that can be cached ---")
# A byte that moves invalidates everything after it, and it fails silently.
first = systemprompt.systemprompt()
check("the system prompt is byte-identical between calls",
      first == systemprompt.systemprompt())
check("and again after a third",
      first == systemprompt.systemprompt(), "")
check("the tool schemas come out in the same order every time",
      json.dumps(toolspec.native_schema()) == json.dumps(toolspec.native_schema()))
# The usual silent invalidators: a clock, a uuid, a set rendered into text.
import re                                                          # noqa: E402
for pattern, what in [(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", "a timestamp"),
                      (r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}", "a uuid"),
                      (r"0x[0-9a-f]{9,}", "an object address")]:
    check(f"no {what} in the system prompt", not re.search(pattern, first),
          str(re.findall(pattern, first)[:2]))

# ---------------------------------------------------------------------------
print("\n--- the report says what happened, not what was intended ---")
from aetheris import context, tui                            # noqa: E402
import io                                                          # noqa: E402

config.token_history[:] = [
    {"prompt": 6_040, "completion": 40, "cached": 0, "turn": 1},
    {"prompt": 6_400, "completion": 40, "cached": 5_900, "turn": 1},
    {"prompt": 6_900, "completion": 40, "cached": 6_300, "turn": 1},
]
turns = context.token_turns()
check("cached tokens are folded into the turn like the rest",
      turns[0]["cached"] == 12_200, str(turns[0]))


def render(provider):
    saved, providers._active = providers._active, provider
    buffer, real = io.StringIO(), sys.stdout
    sys.stdout = buffer
    try:
        tui.display_usage_graph([{"role": "system", "content": "x" * 400}])
    finally:
        sys.stdout = real
        providers._active = saved
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", buffer.getvalue())

printed = render(anthropic)
check("a hosted provider is told how much was cached", "12,200" in printed,
      printed[printed.find("cache"):][:70])

# Ollama reuses the prefix in its own KV cache, charges nothing and reports
# nothing. Claiming a hit rate on its behalf would be inventing one.
printed = render(providers.OllamaProvider({"model": "gemma4:e4b"}))
check("and a local one is told nothing at all", "cache" not in printed.lower(),
      printed[-120:])

config.token_history[:] = [{"prompt": 900, "completion": 40, "cached": 0, "turn": 1}
                           for _ in range(4)]
printed = render(anthropic)
check("a cache that never reads is reported as a problem",
      "nothing read" in printed, printed[printed.find("cache"):][:90])

config.token_history.clear()
server.shutdown()

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("caching checks passed")
