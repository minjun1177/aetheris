# Skills and MCP Servers

[← README](../README.md)

## Skills

A skill is an instruction pack stored on disk that the model pulls in only when
it is relevant. This keeps the system prompt small no matter how many skills
exist: only `name` and `description` are always loaded, and the body arrives
when the model calls `use_skill`.

```
skills/
  git-commit/
    SKILL.md          <- required
    types.md          <- optional bundled files, listed with absolute paths
  quick-skill.md      <- one-file skill
```

Skills are searched in `./skills/` first, then `~/.aetheris/skills/`; the first
match on a name wins, so a project skill overrides a personal one.

`SKILL.md` opens with YAML frontmatter:

```markdown
---
name: git-commit
description: Use when the user asks for a commit message. Triggers on "commit", "커밋".
allowed-tools: git_status, git_diff, run_cmd
---

Instructions in plain markdown.
```

- `name` — optional; the folder or file name is used when it is missing.
- `description` — the only thing the model sees before loading, so write it as
  *when to use this* and include the words a user would actually type.
- `allowed-tools` — optional; shown to the model as the tool set the skill
  expects. Advisory, not enforced by the harness.

Two skills ship with the repo: `git-commit` and `code-review`. See
`skills/README.md` for the full format reference.

They are *in the repository*, not in the installed package - skills are looked
for in the working directory and in `~/.aetheris/skills/`, never next to the
code, so that a project's own skills win and an install cannot quietly add
instructions you did not write. From a `pip install`, copy the two you want:

```bash
git clone https://github.com/minjun1177/aetheris
cp -r aetheris/skills/* ~/.aetheris/skills/
```

---

## MCP Servers

[MCP](https://modelcontextprotocol.io) is the standard way to hand an assistant
tools it did not ship with - a filesystem browser, a database, an issue tracker.
Declare a server once and its tools appear alongside the built-in ones.

### Declaring a server

Servers are read from `./.mcp.json` first, then `~/.aetheris/mcp.json`; a
project entry wins over a personal one with the same name. Copy
`.mcp.json.example` to get started.

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "D:/work"]
    },
    "github": {
      "type": "http",
      "url": "https://api.githubcopilot.com/mcp/",
      "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"}
    }
  }
}
```

| Key | Meaning |
| :--- | :--- |
| `command`, `args`, `env`, `cwd` | Start a local server over **stdio**. |
| `url`, `headers` | Talk to a remote server over **streamable HTTP**. |
| `type` | `stdio`, `http`, or `sse` for the deprecated HTTP+SSE transport. Inferred from `command`/`url` when omitted. |
| `disabled` | `true` keeps the entry but does not start it. |
| `timeout` | Seconds to wait for a tool call from this server. |
| `autoApprove` | Tool names on this server that may run without an approval prompt. |
| `trust` | `true` auto-approves every tool on this server. |

`${VAR}` and `${env:VAR}` anywhere in a server entry are replaced with the
matching environment variable, so tokens stay out of the file.

### How it behaves

Every enabled server is started when the app launches, in parallel. Its tools
are fetched and written into the system prompt as `mcp__<server>__<tool>`, with
each tool's JSON Schema flattened into the same parameter list the built-in
tools use. A server that fails to start is reported and skipped - the app runs
without it. Server-declared `instructions` are passed through to the model, and
`readOnlyHint` / `destructiveHint` annotations are surfaced in the tool
description.

Calls go through the same approval prompt as `run_cmd` and file edits, so
nothing runs on an attached server without a `y` - unless `/automode on`,
`autoApprove`, or `trust` says otherwise.

### Big servers are announced, not described

A server's tools are written out in full on **every request** - into the system
prompt over the text protocol, into the request's own `tools` field over a
native one. Measured against a real `@playwright/mcp` server with 24 tools:

| | tokens, every request |
| :--- | ---: |
| in the system prompt (text protocol) | 3,549 |
| in the `tools` field (native) | 4,637 |
| **its name and its tools' names** | **130** |

That is paid whether or not the conversation has anything to do with a browser,
and attaching three such servers spends most of a 65,536 context on tool
descriptions nobody asked for.

So a server with `MCP_LAZY_MIN_TOOLS` tools or more (6 by default) is
*announced* rather than described:

```
### MCP SERVERS (attached, tools not yet loaded):
- playwright (24 tools): browser_close, browser_resize, browser_navigate,
  browser_click, browser_type, browser_snapshot, browser_evaluate, …
```

Names only - a name is what tells the model whether a server does the thing it
wants. When it decides it does, `use_mcp_server("playwright")` hands over the
parameters, and from then on those tools are in the list like any other. It is
the shape `use_skill` already has, for the same reason: the prompt should carry
what is needed to *choose*, not everything that might be used.

Three things keep it from being a trap:

**Loading is about what the model is shown, never about what it may do.** A
call to an unloaded server's tool still resolves and still runs - a context
optimisation must not be able to break a call. And calling one loads that
server, so the *next* call is not a guess. (Over a native interface a provider
cannot emit a call whose schema it was never given; that is a protocol limit
rather than a rule here, and it is what the announcement exists to work
around.)

**A load that the compressor dropped is a load that ended.** The loaded set is
read back out of the conversation, exactly as `LOADED_SKILLS` is, so the model
is never told a server is "already loaded" after the message carrying its tools
was pruned away.

**A small server is not worth a round trip.** Below `MCP_LAZY_MIN_TOOLS` the
announcement costs about what the schemas cost, so those servers are shown
outright. `MCP_LAZY_TOOLS = False` restores the old behaviour exactly.

Tuning knobs live in `config.py`: `MCP_ENABLED`, `MCP_LAZY_TOOLS`,
`MCP_LAZY_MIN_TOOLS`, `MCP_STARTUP_TIMEOUT`,
`MCP_CALL_TIMEOUT`, `MCP_HTTP_TIMEOUT`, `MCP_RESULT_CHARS`,
`MCP_MAX_TOOLS_PER_SERVER`, `MCP_TRUSTED_SERVERS`, and
`MCP_AUTO_APPROVE_READONLY`.

### Inspecting and controlling

`/mcp` shows every configured server, its transport, what it exposes, and the
error behind any failure. `/mcp tools` expands the tool list, `/mcp resources`
lists readable resources, `/mcp reload` re-reads the config files and
reconnects, and `/mcp prompt <server> <name> key=value` runs a prompt template
the server offers as your next message.

The protocol client is a self-contained JSON-RPC implementation in
`mcp_client.py` - no SDK dependency, and no new packages to install.
