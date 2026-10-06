"""A big MCP server is announced by name, and described when it is asked for.

A server's tools are written out in full on every single request - into the
prompt over the text protocol, into the `tools` field over a native one. One
24-tool playwright server measures 3,549 tokens of prompt or 4,637 of schema,
paid on every turn whether or not the conversation is about a browser. Attach
three and most of a 65,536 context is tool descriptions nobody asked for.

So the big ones are announced: name and tool names, 130 tokens, and
`use_mcp_server` fetches the parameters when the model decides it wants them.
That is the shape `use_skill` already has.

The two things that would be quietly wrong are checked here. First, that
loading changes what the model is *shown* and never what it may *do* - a call
to an unloaded server's tool still resolves, because a context optimisation
must not be able to break a call. Second, that `_tool_entries` and
`_raw_input_schemas` filter identically: `native_tool_schemas` zips them, so a
filter applied to one and not the other would pair one tool's name with
another tool's parameters, which is worse than either extreme.
"""
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from aetheris import paths

HOME = tempfile.mkdtemp(prefix="mcplazy-home-")
os.environ[paths.ENV_VAR] = HOME

from aetheris import config          # noqa: E402
config.SAVE_CHAT_HISTORY = False
config.MCP_ENABLED = True

from aetheris import context, mcp_client          # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


class FakeServer:
    """A connected server, without a subprocess. Only what the prompt reads."""

    def __init__(self, name, count, source="project"):
        self.name = name
        self.slug = mcp_client._slug(name)
        self.state = "connected"
        self.source = source
        self.instructions = ""
        self.info = {}
        self.spec = {}
        self.target = ""
        self.transport = None
        self.resources = []
        self.prompts = []
        self.capabilities = {}
        self.tools = [
            {"name": f"do_{i}",
             "description": f"Does thing number {i} on the {name} server.",
             "inputSchema": {"type": "object",
                             "properties": {"where": {"type": "string",
                                                      "description": "x" * 60}},
                             "required": ["where"]}}
            for i in range(count)
        ]


BIG, SMALL = FakeServer("browser", 24), FakeServer("clock", 2)
_servers = {"browser": BIG, "clock": SMALL}
mcp_client.load_servers = lambda force=False: _servers
mcp_client.all_servers = lambda: list(_servers.values())
mcp_client.connected_servers = lambda: [s for s in _servers.values()
                                        if s.state == "connected"]
mcp_client.get_server = lambda name: _servers.get(name)


def tokens(text):
    return context._estimate_tokens([{"role": "user", "content": text}])


def reset():
    config.LOADED_MCP_SERVERS.clear()
    config.MCP_LAZY_TOOLS = True
    config.MCP_LAZY_MIN_TOOLS = 6


# ---------------------------------------------------------------------------
print("--- a big server is announced, a small one is just shown ---")
reset()
check("the big one is held back", mcp_client.is_lazy(BIG))
check("the small one is not - the index would cost what its schemas cost",
      not mcp_client.is_lazy(SMALL))
shown = [s.name for s in mcp_client.shown_servers()]
check("so only the small one is described", shown == ["clock"], str(shown))

prompt = mcp_client.mcp_tools_prompt(tools_json=True)
check("the announcement names the server", "browser (24 tools)" in prompt)
check("and lists its tool names", "do_0" in prompt and "do_23" in prompt)
check("but not their parameters", '"where"' not in prompt.split("MCP SERVERS")[-1])
check("it says how to get them", "use_mcp_server" in prompt)

# ---------------------------------------------------------------------------
print("\n--- and that is the whole point: it is much smaller ---")
reset()
lazy_prompt = tokens(mcp_client.mcp_tools_prompt(tools_json=True))
lazy_native = tokens(json.dumps(mcp_client.native_tool_schemas()))
config.MCP_LAZY_TOOLS = False
full_prompt = tokens(mcp_client.mcp_tools_prompt(tools_json=True))
full_native = tokens(json.dumps(mcp_client.native_tool_schemas()))
print(f"        prompt  {lazy_prompt:>5} -> {full_prompt:>5} tokens")
print(f"        native  {lazy_native:>5} -> {full_native:>5} tokens")
check("the prompt is smaller while the server is only announced",
      lazy_prompt < full_prompt / 2, f"{lazy_prompt} vs {full_prompt}")
check("and the native schemas carry only the small server",
      lazy_native < full_native / 2, f"{lazy_native} vs {full_native}")

# ---------------------------------------------------------------------------
print("\n--- asking for it hands over the parameters ---")
reset()
result = mcp_client.use_server("browser")
check("the call succeeds", not result.startswith("[Error]"), result[:70])
check("and it is marked loaded", "browser" in config.LOADED_MCP_SERVERS)
check("the server is described from now on",
      sorted(s.name for s in mcp_client.shown_servers()) == ["browser", "clock"],
      str([s.name for s in mcp_client.shown_servers()]))
check("its tools reach the native schemas",
      any(s["name"] == "mcp__browser__do_7"
          for s in mcp_client.native_tool_schemas()))
check("the announcement stops announcing it",
      "browser (24 tools)" not in mcp_client.mcp_tools_prompt(tools_json=True))
check("a name that is not there is an error naming the ones that are",
      "clock" in mcp_client.use_server("nosuch")
      and mcp_client.use_server("nosuch").startswith("[Error]"))

# ---------------------------------------------------------------------------
print("\n--- names and parameters cannot come apart ---")
# `native_tool_schemas` zips `_tool_entries` with `_raw_input_schemas`. If one
# filtered and the other did not, every tool after the first hidden server
# would be handed somebody else's parameters - a silent, untraceable wrong call.
for loaded in ([], ["browser"]):
    reset()
    config.LOADED_MCP_SERVERS[:] = loaded
    schemas = mcp_client.native_tool_schemas()
    mismatched = [s for s in schemas
                  if s["name"].startswith("mcp__")
                  and s["name"].split("__")[1] not in
                  json.dumps(s["input_schema"]) + s["description"]]
    check(f"every schema belongs to its own tool (loaded={loaded or 'none'})",
          all(s["name"].split("__")[1] in s["description"] for s in schemas
              if s["name"].startswith("mcp__")),
          str([s["name"] for s in schemas][:3]))

# ---------------------------------------------------------------------------
print("\n--- loading is about what is shown, never about what is allowed ---")
reset()
check("an unloaded server's tool still resolves",
      mcp_client.resolve_tool("mcp__browser__do_3") is not None)
check("and calling one loads it, so the next call is not a guess",
      (mcp_client.note_call("mcp__browser__do_3") is None)
      and "browser" in config.LOADED_MCP_SERVERS)

# ---------------------------------------------------------------------------
print("\n--- a load that the compressor dropped is a load that ended ---")
reset()
loaded_msg = {"role": "user", "content": "[Tool Result for 'use_mcp_server']:\n"
                                         + mcp_client.use_server("browser")}
check("the marker is found while the message is there",
      mcp_client.loaded_in([loaded_msg]) == ["browser"])
check("and gone once it is not", mcp_client.loaded_in([]) == [])
config.LOADED_MCP_SERVERS[:] = ["browser"]
context._sync_loaded_mcp_servers([{"role": "user", "content": "unrelated"}])
check("the sync forgets it rather than claiming tools nobody is sending",
      config.LOADED_MCP_SERVERS == [], str(config.LOADED_MCP_SERVERS))
# The marker sits at the front of the result, so a *trimmed* result - which
# keeps both ends - still counts as loaded. Only dropping it entirely unloads.
trimmed = dict(loaded_msg)
trimmed["content"] = trimmed["content"][:200] + "\n…\n" + trimmed["content"][-200:]
check("a trimmed result still counts", mcp_client.loaded_in([trimmed]) == ["browser"])

# ---------------------------------------------------------------------------
print("\n--- switching it off restores the old behaviour exactly ---")
reset()
config.MCP_LAZY_TOOLS = False
off_prompt = mcp_client.mcp_tools_prompt(tools_json=True)
config.MCP_LAZY_TOOLS = True
config.LOADED_MCP_SERVERS[:] = ["browser", "clock"]
all_loaded = mcp_client.mcp_tools_prompt(tools_json=True)
check("everything loaded looks the same as lazy switched off",
      off_prompt == all_loaded)
check("and no announcement is left over", "MCP SERVERS" not in off_prompt)

shutil.rmtree(HOME, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("mcp lazy-loading checks passed")
