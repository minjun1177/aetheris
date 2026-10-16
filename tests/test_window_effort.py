"""A context window per model, and `/effort` per provider.

One `NUM_CTX` for every model compacted a hosted model that takes a million at
a sixteenth of it, and nothing could set how hard a hosted model reasons. What
is checked: where the window comes from and in which order, that it is asked
for once and kept per model, that Ollama is not handed a hosted model's figure,
and that each provider gets the effort in its own field - on a turn's request
only, never on a short side call, and never to an Ollama model that cannot think.
"""
import asyncio
import builtins
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AETHERIS_HOME"] = tempfile.mkdtemp(prefix="window-home-")
os.environ["AETHERIS_KEYRING"] = "off"
for variable in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
    os.environ.pop(variable, None)

from aetheris import config          # noqa: E402
config.MCP_ENABLED = False
config.CHANNEL_ENABLED = False

from aetheris import app, context, providers   # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


def said_by(function, *args):
    """What `function` printed, as one string."""
    lines, original = [], builtins.print
    builtins.print = lambda *a, **k: lines.append(" ".join(str(x) for x in a))
    try:
        function(*args)
    finally:
        builtins.print = original
    return "\n".join(lines)


def on_disk(name: str) -> dict:
    """What `providers.json` holds for a provider - read, not reloaded."""
    with open(providers.CONFIG_PATH, encoding="utf-8") as handle:
        return json.load(handle)["providers"].get(name, {})


class Captured(Exception):
    pass


def payload_of(provider, max_tokens=None):
    """The body `provider` would send, caught before it leaves."""
    sent = {}

    def post(url, headers, payload):
        sent["payload"] = payload
        raise Captured()

    provider._post_sse = post

    async def drain():
        try:
            async for _ in provider.stream([{"role": "system", "content": "s"},
                                            {"role": "user", "content": "hi"}],
                                           max_tokens=max_tokens):
                pass
        except Captured:
            pass
    asyncio.run(drain())
    return sent.get("payload", {})


print("--- where the window comes from ---")
asked = []
real_get = providers.GeminiProvider._get_json


def gemini_get(self, url, headers, timeout=30):
    asked.append(url)
    if url.endswith("/models?pageSize=200"):
        return {"models": [{"name": "models/g-listed", "inputTokenLimit": 2_000_000,
                            "supportedGenerationMethods": ["generateContent"]}]}
    return {"inputTokenLimit": 1_048_576}


providers.GeminiProvider._get_json = gemini_get
try:
    provider, problem = providers.connect("gemini", model="g-1", api_key="k")
    check("connected to a hosted model", not problem, problem)
    window, source = providers.context_window()
    check("a hosted model's own window is used", (window, source) == (1_048_576, "model"),
          str((window, source)))
    providers.context_window()
    check("and asked for once, not per request", len(asked) == 1, str(asked))
    check("the compaction budget follows it",
          context._get_ctx_budget() == int(1_048_576 * 0.85) - config.NUM_PREDICT)
    provider.list_models()
    check("a listing that carries the windows fills them in on the way",
          providers.build("gemini").window_limit() == 1_048_576
          and providers._windows.get(("gemini", provider.base_url, "g-listed")) == 2_000_000)

    providers.set_window(200_000)
    check("/context sets one by hand, and it wins",
          providers.context_window() == (200_000, "set"))
    check("kept with the provider, per model",
          on_disk("gemini")["context"] == {"g-1": 200_000})
    providers.connect("gemini", model="g-2")
    check("another model of the same provider is not given it",
          providers.context_window() == (1_048_576, "model"))
    providers.connect("gemini", model="g-1")
    providers.set_window(0)
    check("default takes it back", providers.context_window()[1] == "model"
          and "context" not in providers.settings_for("gemini"))
finally:
    providers.GeminiProvider._get_json = real_get


def refuses(self, url, headers, timeout=30):
    raise RuntimeError("offline")


providers.AnthropicProvider._get_json = refuses
try:
    providers.connect("anthropic", model="a-1", api_key="k")
    check("a model that will not say gets NUM_CTX",
          providers.context_window() == (config.NUM_CTX, "NUM_CTX"))
finally:
    del providers.AnthropicProvider._get_json

providers.connect("ollama", model="local-1")
check("Ollama gets NUM_CTX, not what the weights were trained to",
      providers.context_window() == (config.NUM_CTX, "NUM_CTX"))
check("and asks Ollama for it",
      providers.ollama_request("local-1", [])["options"]["num_ctx"] == config.NUM_CTX)
providers.set_window(16_384)
check("a local model given its own window asks Ollama for that",
      providers.ollama_request("local-1", [])["options"]["num_ctx"] == 16_384
      and providers.ollama_request("local-2", [])["options"]["num_ctx"] == config.NUM_CTX)
providers.set_window(0)

print("\n--- /context ---")
said = said_by(app._context, "")
check("alone it says the window, where it came from, and how to set it",
      f"{config.NUM_CTX:,}" in said and "NUM_CTX" in said and "/context <tokens>" in said,
      said)
said = said_by(app._context, "32k")
check("32k is understood", providers.context_window() == (32_000, "set"), said)
said = said_by(app._context, "lots")
check("a word is a usage error, and changes nothing",
      "Usage: /context" in said and providers.context_window() == (32_000, "set"))
said_by(app._context, "100")
check("a window smaller than two replies is refused",
      providers.context_window() == (32_000, "set"))
said_by(app._context, "default")
check("default puts it back", providers.context_window()[1] == "NUM_CTX")
check("1m and 1,000,000 are the same", app._tokens("1m") == app._tokens("1,000,000") == 1_000_000)

print("\n--- effort, in each provider's own words ---")
anthropic = providers.build("anthropic")
anthropic.settings.update(model="a-1", api_key="k")
openai = providers.build("openai")
openai.settings.update(model="o-1", api_key="k")
gemini = providers.build("gemini")
gemini.settings.update(model="g-1", api_key="k")
check("nothing is sent until one is chosen",
      "output_config" not in payload_of(anthropic)
      and "reasoning_effort" not in payload_of(openai)
      and "thinkingConfig" not in payload_of(gemini)["generationConfig"])
for provider in (anthropic, openai, gemini):
    provider.settings["effort"] = "high"
check("Anthropic: output_config.effort",
      payload_of(anthropic).get("output_config") == {"effort": "high"})
check("OpenAI: reasoning_effort", payload_of(openai).get("reasoning_effort") == "high")
check("Gemini: thinkingConfig.thinkingLevel",
      payload_of(gemini)["generationConfig"].get("thinkingConfig") == {"thinkingLevel": "HIGH"})
check("a short side call carries none of them",
      "output_config" not in payload_of(anthropic, max_tokens=6)
      and "reasoning_effort" not in payload_of(openai, max_tokens=6)
      and "thinkingConfig" not in payload_of(gemini, max_tokens=6)["generationConfig"])


class Refused:
    status_code = 400
    headers = {}
    text = '{"error": {"message": "thinking_level is not supported"}}'

    def json(self):
        return {"error": {"message": "thinking_level is not supported"}}


check("a 400 while an effort is set says how to stop sending it",
      "/effort default" in gemini._failed(Refused()), gemini._failed(Refused()))
gemini.settings.pop("effort")
check("and says nothing of it otherwise", "/effort" not in gemini._failed(Refused()))

ollama = providers.build("ollama")
ollama.settings.update(model="thinker", effort="off")
providers._ollama_capability_lists[(ollama.host, "thinker")] = ["completion", "thinking"]
providers._ollama_capability_lists[(ollama.host, "plain")] = ["completion"]
check("Ollama: off is think false", ollama.think_value() is False)
ollama.settings["effort"] = "high"
check("a level goes as the level", ollama.think_value() == "high")
check("not on a side call", ollama.think_value(6) is None)
ollama.settings["model"] = "plain"
check("and never to a model that does not think - Ollama refuses the request",
      ollama.think_value() is None)

print("\n--- /effort ---")
providers.connect("openai", model="o-1", api_key="k")
said = said_by(app._effort, "medium")
check("sets it for the provider in use",
      providers.current().effort == "medium" and "medium" in said, said)
check("kept with the provider",
      on_disk("openai")["effort"] == "medium")
said = said_by(app._effort, "turbo")
check("a level the provider does not take is refused, naming the ones it does",
      "none, minimal, low" in said and providers.current().effort == "medium", said)
said_by(app._effort, "default")
check("default leaves it to the model", providers.current().effort == ""
      and "effort" not in on_disk("openai"))
providers.settings_for("ollama").pop("effort", None)
providers.connect("ollama", model="plain")
said = said_by(app._effort, "high")
check("a local model that does not think is told so, and nothing is kept",
      "does not think" in said and not providers.current().effort, said)


print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("window and effort checks passed")
