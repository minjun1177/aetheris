"""Native function calling: the schemas out, and the calls back in.

Each provider is driven by a mock server speaking that vendor's documented
tool-call format - Anthropic's `input_json_delta` fragments, OpenAI's indexed
`tool_calls` deltas, Gemini's whole `functionCall` parts. The point of the last
section is that none of it reaches the rest of the harness differently: a
native call and a text `<tool_call>` end up as the same pair, run the same
handler, and leave the same history behind.
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
config.AUTO_ALLOW = True

from aetheris import providers
from aetheris import systemprompt
from aetheris import toolspec

failures = []
received = {}


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _sse(self, events):
        body = "".join(
            f"data: {p if isinstance(p, str) else json.dumps(p)}\n\n" for p in events
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        received["body"] = json.loads(self.rfile.read(
            int(self.headers.get("Content-Length", 0))))
        received["path"] = self.path

        if "/v1/messages" in self.path:                          # Anthropic
            return self._sse([
                {"type": "message_start", "message": {"usage": {"input_tokens": 9}}},
                {"type": "content_block_delta",
                 "delta": {"type": "text_delta", "text": "파일을 읽겠습니다."}},
                {"type": "content_block_start", "index": 1,
                 "content_block": {"type": "tool_use", "id": "toolu_1",
                                   "name": "read_file", "input": {}}},
                # The arguments arrive a few characters at a time.
                {"type": "content_block_delta", "index": 1,
                 "delta": {"type": "input_json_delta", "partial_json": '{"file'}},
                {"type": "content_block_delta", "index": 1,
                 "delta": {"type": "input_json_delta", "partial_json": 'path": "a'}},
                {"type": "content_block_delta", "index": 1,
                 "delta": {"type": "input_json_delta", "partial_json": '.py"}'}},
                {"type": "content_block_stop", "index": 1},
                {"type": "message_delta", "usage": {"output_tokens": 4}},
            ])
        if "/chat/completions" in self.path:                     # OpenAI
            return self._sse([
                {"choices": [{"delta": {"content": "파일을 읽겠습니다."}}]},
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "id": "call_1", "type": "function",
                     "function": {"name": "read_file", "arguments": '{"file'}}]}}]},
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": 'path": "a.py"}'}}]}}]},
                {"choices": [{"finish_reason": "tool_calls", "delta": {}}]},
                {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 4}},
                "[DONE]",
            ])
        if ":streamGenerateContent" in self.path:                # Gemini
            return self._sse([
                {"candidates": [{"content": {"parts": [{"text": "파일을 읽겠습니다."}]}}]},
                {"candidates": [{"content": {"parts": [
                    {"functionCall": {"name": "read_file",
                                      "args": {"filepath": "a.py"}}}]}}],
                 "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 4}},
            ])
        self.send_error(404)


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{server.server_address[1]}"

CONVO = [{"role": "system", "content": "sys"}, {"role": "user", "content": "a.py 읽어줘"}]
SCHEMAS = toolspec.native_schema()


async def drain(provider, tools):
    text, calls, final = "", [], {}
    async for chunk in provider.stream(CONVO, tools=tools):
        text += chunk.get("text", "") or ""
        if chunk.get("tool_call"):
            calls.append(chunk["tool_call"])
        if chunk.get("done"):
            final = chunk
    return text, calls, final


print("--- the schemas ---")
check("every tool has one", len(SCHEMAS) == len(toolspec.TOOLS))
by_name = {s["name"]: s for s in SCHEMAS}
check("a raw-block value becomes a real parameter",
      "content" in by_name["write_file"]["input_schema"]["properties"])
check("and a required one",
      "content" in by_name["write_file"]["input_schema"]["required"])
check("the block wording is gone from the native description",
      "<content>" not in by_name["write_file"]["description"])
check("optional parameters are not required",
      by_name["run_cmd"]["input_schema"]["required"] == ["command"],
      str(by_name["run_cmd"]["input_schema"]["required"]))
check("a tool with no parameters has an empty schema",
      by_name["git_diff"]["input_schema"]["properties"] == {})
check("the text prompt still hides raw blocks",
      "content" not in json.loads(toolspec.prompt_schema())[7]["parameters"])

print("\n--- Anthropic ---")
p = providers.AnthropicProvider({"api_key": "k", "base_url": base, "model": "m"})
text, calls, final = asyncio.run(drain(p, SCHEMAS))
check("prose still streams", text == "파일을 읽겠습니다.", repr(text))
check("the fragmented call is reassembled",
      calls == [{"name": "read_file", "id": "toolu_1", "arguments": {"filepath": "a.py"}}],
      str(calls))
check("tokens still come back", final.get("prompt_tokens") == 9)
sent = {t["name"] for t in received["body"]["tools"]}
check("the tools were sent", "read_file" in sent and len(sent) == len(SCHEMAS))
check("in Anthropic's shape", "input_schema" in received["body"]["tools"][0])

print("\n--- OpenAI ---")
p = providers.OpenAIProvider({"api_key": "k", "base_url": base, "model": "m"})
text, calls, final = asyncio.run(drain(p, SCHEMAS))
check("prose still streams", text == "파일을 읽겠습니다.", repr(text))
check("the indexed fragments are reassembled",
      calls == [{"name": "read_file", "id": "call_1", "arguments": {"filepath": "a.py"}}],
      str(calls))
check("tokens still come back", final.get("prompt_tokens") == 9)
check("in OpenAI's shape",
      received["body"]["tools"][0]["type"] == "function"
      and "parameters" in received["body"]["tools"][0]["function"])

print("\n--- Gemini ---")
p = providers.GeminiProvider({"api_key": "k", "base_url": base, "model": "m"})
text, calls, final = asyncio.run(drain(p, SCHEMAS))
check("prose still streams", text == "파일을 읽겠습니다.", repr(text))
check("a whole call needs no reassembly",
      calls == [{"name": "read_file", "id": "", "arguments": {"filepath": "a.py"}}],
      str(calls))
declarations = received["body"]["tools"][0]["functionDeclarations"]
check("in Gemini's shape", len(declarations) == len(SCHEMAS))
check("a tool with no parameters is declared without a schema",
      "parameters" not in [d for d in declarations if d["name"] == "git_diff"][0])
check("and one with parameters keeps them",
      "filepath" in [d for d in declarations
                     if d["name"] == "read_file"][0]["parameters"]["properties"])

print("\n--- Ollama decides per model, not per provider ---")
# Keyed by (host, model): the same model name on another machine is another
# model, so an answer cached against one daemon must not be handed to another.
here = providers.OllamaProvider({"model": "toolful:1b"}).host
# One cache, holding what `ollama show` said the model can do. Tool support and
# vision are both read out of it, so seeding it seeds both.
providers._ollama_capability_lists.update({
    (here, "toolful:1b"): ["completion", "tools"],
    (here, "toolless:1b"): ["completion"],
    (here, "seeing:1b"): ["completion", "tools", "vision"]})
check("a model whose template can call tools gets the native path",
      providers.OllamaProvider({"model": "toolful:1b"}).supports_native_tools)
check("one whose template cannot keeps the text protocol",
      not providers.OllamaProvider({"model": "toolless:1b"}).supports_native_tools)
check("the same answer says whether it can be sent an image",
      providers.OllamaProvider({"model": "seeing:1b"}).sees_images()
      and not providers.OllamaProvider({"model": "toolful:1b"}).sees_images())
check("the answer does not follow the model name to another daemon",
      not providers.OllamaProvider({"model": "toolful:1b",
                                    "base_url": "http://127.0.0.1:1"}).supports_native_tools)
check("a model nobody can ask about falls back to text",
      not providers.ollama_supports_tools("no-such-model-xyz:1b"))
check("no model at all is not native", not providers.ollama_supports_tools(""))

print("\n--- and where that daemon is can be said ---")
saved = config.OLLAMA_HOST
try:
    os.environ.pop("OLLAMA_HOST", None)
    check("it defaults to localhost",
          providers.OllamaProvider({}).host == "http://localhost:11434",
          providers.OllamaProvider({}).host)
    os.environ["OLLAMA_HOST"] = "http://from-the-environment:11434"
    check("the environment is honoured while the setting is untouched",
          providers.OllamaProvider({}).host == "http://from-the-environment:11434",
          providers.OllamaProvider({}).host)
    config.OLLAMA_HOST = "http://from-set:9999"
    check("and /set wins over the environment",
          providers.OllamaProvider({}).host == "http://from-set:9999",
          providers.OllamaProvider({}).host)
    check("a base_url saved by /connect wins over both",
          providers.OllamaProvider({"base_url": "http://saved:1/"}).host
          == "http://saved:1")
    check("it is a setting /set can reach", "OLLAMA_HOST" in config.settable())
finally:
    config.OLLAMA_HOST = saved
    os.environ.pop("OLLAMA_HOST", None)
check("it takes OpenAI's tool shape",
      providers.OllamaProvider({}).encode_tools(SCHEMAS)[0]["type"] == "function")

# Ollama hands a call over whole, with its arguments already parsed, wrapped in
# whatever object shape the client version happens to use.
class _Fn:
    name = "read_file"
    arguments = {"filepath": "a.py"}


class _Call:
    function = _Fn()


check("a pydantic-shaped call is read",
      providers._ollama_calls({"tool_calls": [_Call()]})
      == [{"tool_call": {"name": "read_file", "id": "",
                         "arguments": {"filepath": "a.py"}}}])
check("a plain dict call is read too",
      providers._ollama_calls(
          {"tool_calls": [{"function": {"name": "git_diff", "arguments": {}}}]})
      == [{"tool_call": {"name": "git_diff", "id": "", "arguments": {}}}])
check("arguments that arrive as a string are parsed",
      providers._ollama_calls(
          {"tool_calls": [{"function": {"name": "read_file",
                                        "arguments": '{"filepath": "b.py"}'}}]})[0]
      ["tool_call"]["arguments"] == {"filepath": "b.py"})
check("a nameless call is dropped",
      providers._ollama_calls({"tool_calls": [{"function": {"name": ""}}]}) == [])
check("a chunk with no calls yields none", providers._ollama_calls({}) == [])

print("\n--- broken arguments are reported, not run ---")
from aetheris import llm_client
broken = providers._decode_call({"name": "delete_file", "id": "x", "json": '{"file'})
check("unparseable JSON becomes an error, not empty arguments",
      broken["tool_call"].get("error") and broken["tool_call"]["arguments"] == {},
      str(broken))
pairs = llm_client._from_native([broken["tool_call"]])
check("and the turn loop can see it",
      llm_client.NATIVE_ERROR in pairs[0][1], str(pairs))
check("a tool that takes nothing is not an error",
      providers._decode_call({"name": "git_diff", "id": "", "json": ""})
      == {"tool_call": {"name": "git_diff", "id": "", "arguments": {}}})
check("a nameless call is dropped",
      providers._decode_call({"name": "", "id": "", "json": "{}"}) is None)

print("\n--- the prompt changes with the protocol ---")
saved = providers._active
providers._active = providers.AnthropicProvider({"api_key": "k", "model": "m"})
native_prompt = systemprompt.systemprompt()
config.NATIVE_TOOLS = False                 # same provider, text protocol
text_prompt = systemprompt.systemprompt()
config.NATIVE_TOOLS = True
providers._active = saved
check("the text prompt lists the tools", "### AVAILABLE TOOLS:" in text_prompt)
check("the native prompt does not", "### AVAILABLE TOOLS:" not in native_prompt)
check("and drops the <tool_call> format", "<tool_call>" not in native_prompt)
check("but keeps the behaviour rules",
      "Self-Correction" in native_prompt and "HASHLINE" in native_prompt)
check("it is materially shorter", len(native_prompt) < len(text_prompt) - 8000,
      f"{len(text_prompt)} -> {len(native_prompt)}")
config.NATIVE_TOOLS = False
providers._active = providers.AnthropicProvider({"api_key": "k", "model": "m"})
check("NATIVE_TOOLS=False forces the text protocol", not llm_client.native_enabled())
check("and brings the tool list back",
      "### AVAILABLE TOOLS:" in systemprompt.systemprompt())
config.NATIVE_TOOLS = True
providers._active = saved

print("\n--- end to end: a native call runs the tool and leaves ordinary history ---")
import tempfile
work = tempfile.mkdtemp(prefix="native-")
os.chdir(work)


class Writer(Handler):
    """Answers with a native write_file call, then with prose."""
    turns = 0

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        Writer.turns += 1
        if Writer.turns == 1:
            return self._sse([
                {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
                {"type": "content_block_delta",
                 "delta": {"type": "text_delta", "text": "만들겠습니다."}},
                {"type": "content_block_start", "index": 1,
                 "content_block": {"type": "tool_use", "id": "toolu_9",
                                   "name": "write_file", "input": {}}},
                {"type": "content_block_delta", "index": 1,
                 "delta": {"type": "input_json_delta",
                           "partial_json": '{"filepath": "g.py", "content": "print('}},
                {"type": "content_block_delta", "index": 1,
                 "delta": {"type": "input_json_delta",
                           "partial_json": '\\"hi\\")"}'}},
                {"type": "content_block_stop", "index": 1},
                {"type": "message_delta", "usage": {"output_tokens": 7}},
            ])
        return self._sse([
            {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
            {"type": "content_block_delta",
             "delta": {"type": "text_delta", "text": "다 만들었습니다."}},
            {"type": "message_delta", "usage": {"output_tokens": 3}},
        ])


e2e = ThreadingHTTPServer(("127.0.0.1", 0), Writer)
threading.Thread(target=e2e.serve_forever, daemon=True).start()
e2e_base = f"http://127.0.0.1:{e2e.server_address[1]}"

saved = providers._active
providers._active = providers.AnthropicProvider(
    {"api_key": "k", "base_url": e2e_base, "model": "m"})
check("the harness is in native mode", llm_client.native_enabled())

import io, re as _re
messages = [{"role": "system", "content": systemprompt.systemprompt()},
            {"role": "user", "content": "g.py 만들어줘"}]
buf, real = io.StringIO(), sys.stdout
sys.stdout = buf
try:
    answer = asyncio.run(llm_client.chat_turn(messages))
finally:
    sys.stdout = real
    providers._active = saved
printed = _re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", buf.getvalue())

check("the tool actually ran", os.path.exists(os.path.join(work, "g.py")),
      str(os.listdir(work)))
if os.path.exists(os.path.join(work, "g.py")):
    check("with the arguments the provider sent",
          open(os.path.join(work, "g.py"), encoding="utf-8").read().strip()
          == 'print("hi")',
          repr(open(os.path.join(work, "g.py"), encoding="utf-8").read()))
check("the turn finished on prose", answer.strip() == "다 만들었습니다.", repr(answer))
check("the prose before the call reached the screen", "만들겠습니다." in printed)
check("the call was shown like any other", "write_file" in printed)

assistant = [m["content"] for m in messages if m["role"] == "assistant"]
check("history keeps the call in the one text format",
      any("<tool_call>" in m and '"write_file"' in m for m in assistant),
      repr(assistant[0][:90]) if assistant else "")
check("and keeps the prose that came with it",
      any("만들겠습니다." in m for m in assistant))
check("the tool result is an ordinary user message",
      any(m["content"].startswith("[Tool Result for 'write_file']")
          for m in messages if m["role"] == "user"))
check("a saved session would re-parse it",
      llm_client.parse_tool_calls(assistant[0], quiet=True)[0][0] == "write_file"
      if assistant else False)

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("native tool checks passed")
