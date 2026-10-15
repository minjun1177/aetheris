# Using the Parts

[← README](../README.md)

`pip install aetheris` is also a small typed library (`py.typed`). Two pieces of
the harness work on their own, and those - the names in `aetheris.__all__` -
are promised the way the commands and settings are: kept, and called the same
way, until the next major version. Everything else under `aetheris.*` is the
harness's implementation and changes whenever the harness needs it to.

| Name | What it is |
| :--- | :--- |
| `MCPServer`, `MCPError` | An MCP client with no SDK: JSON-RPC 2.0 over stdio, streamable HTTP or the older HTTP+SSE |
| `start_shell`, `ShellSession` | A command kept running, so a program that asks a question can be answered |

`import aetheris` loads nothing; each name is imported the first time it is
used. Their timeouts are the harness's settings, so the first call also loads
its configuration - a fraction of a second, and nothing is written.

---

## An MCP server

```python
import aetheris

server = aetheris.MCPServer("files", {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
})
if not server.connect():                  # initialize, then the tool list
    raise SystemExit(server.error)
print([tool["name"] for tool in server.tools])
result = server.call_tool("list_directory", {"path": "."})   # the JSON-RPC result
server.close()
```

The spec is one entry of an `.mcp.json` - `command`/`args`/`env`, or `url` and
`headers` - taken as given: `${VAR}` is expanded when the harness reads
`.mcp.json`, not here. `connect()` returns `False`
and leaves the reason in `server.error` rather than raising; `call_tool`,
`read_resource` and `get_prompt` raise `MCPError` for a server that answered
with an error or not at all.

---

## A command that asks questions

```python
import time
import aetheris

shell = aetheris.start_shell("python -c \"print('hi', input('name? '))\"")
text, ended, timed_out = shell.read_until_idle(0.5, time.time() + 30)
print(text)                               # "name? " - it is waiting
shell.send("Mirae")
text, ended, timed_out = shell.read_until_idle(0.5, time.time() + 30)
print(text, ended)                        # "hi Mirae", True
shell.close()                             # the exit status
```

`read_until_idle` returns once the program has gone quiet in the way a program
waiting for input does - on Linux that is read off `/proc`, elsewhere it is
judged from the output and the CPU time, which is why `test_platform.py` is
worth running on a new machine. How long it waits on a quiet program that may
just be busy comes from the same settings the harness uses (`CMD_WAIT_TIMEOUT`,
`CMD_IDLE_GRACE`), read from `~/.aetheris/settings.json` when there is one.

---

## Not in the library

The `<tool_call>` reader and its JSON repair are not exported. They exist to
keep a small model's malformed call from ending a turn, report to the harness's
screen, and load the whole harness when imported - a library that promised them
would promise all of that. They may become a package of their own if someone
needs them on their own.
