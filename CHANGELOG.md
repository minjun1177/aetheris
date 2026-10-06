# Changelog

What changed, and why it was worth changing. Versions follow [semantic
versioning](https://semver.org), and from 1.0.0 the surface named in
[Compatibility](#compatibility) is a promise rather than a record: it moves on a
major version and nowhere else. It was written down and pinned by a test from
0.6.0 onwards for exactly that reason - the promise is one this project had
already been keeping for several releases before it was made.

Releases before 1.0.0 were published as **simple-harness**, and the entries
below say so where they said so at the time. Nothing in the history has been
rewritten to use the new name.

## Compatibility

Five things are the contract. `tests/test_compat.py` fails when one of them
disappears, so removing or renaming any of it takes a deliberate edit to that
file - which is the point.

| Surface | What is promised |
|:---|:---|
| Slash commands | A command that exists keeps its name and keeps meaning what it meant |
| `/set` settings | A setting name in `config.py` is public the moment it exists, because `/set` derives its list from there |
| Tool names | Model-facing, and written into saved sessions - a rename breaks replay, not just a prompt |
| State layout | `~/.aetheris/`: `sessions/`, `memory.json`, `settings.json`, `permissions.json`, `mcp.json`, `history`, `skills/` - and `AETHERIS_HOME` to move all of them. `~/.localchat` and `LOCALCHAT_HOME` are still read, and the old directory still wins where it exists |
| Project files | `.permissions.json`, `.mcp.json`, `skills/<name>/SKILL.md`, read from the working directory first |

Not promised: anything inside `aetheris.*`. The modules are an
implementation, not an API, and the reusable pieces are meant to leave for
packages of their own rather than be imported from here.

---

## 1.0.0 - 2026-10-06

The release where the project took its own name. Nothing about what it does
changed; everything about what it is called did.

### Aetheris

`simple-harness` described the shape of the thing rather than the thing, and it
described it as a stopgap - which it stopped being several releases ago. So:

| Was | Is |
|:---|:---|
| `pip install simple-harness` | `pip install aetheris` |
| `simple-harness` | `aetheris` |
| `python -m simple_harness` | `python -m aetheris` |
| `from simple_harness import ...` | `from aetheris import ...` |
| `~/.localchat/` | `~/.aetheris/` |
| `LOCALCHAT_HOME` | `AETHERIS_HOME` |
| `SIMPLE_HARNESS_ACCEPT_TERMS` | `AETHERIS_ACCEPT_TERMS` |
| `github.com/minjun1177/simple_harness` | `github.com/minjun1177/aetheris` |

### Nobody loses a session to it

A rename is a bad reason to lose a year of conversations, so the state
directory is the one thing that does not move:

- **`~/.localchat` is still the home where it already exists** and
  `~/.aetheris` does not. It is read in place - not copied, not migrated, not
  moved. Upgrading changes nothing on disk, and a person who prefers the new
  name moves the directory themselves, once.
- **`LOCALCHAT_HOME` is still read** when `AETHERIS_HOME` is unset, so a shell
  profile or a CI job that exports the old one keeps pointing where it pointed.
- **`SIMPLE_HARNESS_ACCEPT_TERMS` still counts as agreement**, so an unattended
  job does not start asking a question nobody is there to answer.

Both halves of that fallback are pinned in `tests/test_compat.py`, next to the
surface they are the exception to. The old names are read and never written: a
fresh install only ever sees the new ones.

### The promise stops being provisional

The major version is 1, so the [Compatibility](#compatibility) table is now a
guarantee rather than a record, and the 0.x escape clause that said otherwise is
gone. The state-layout row is the only one the rename touched, and it is the row
that carries the fallback above.

## 0.6.0 - 2026-09-08

The release where the harness stopped trusting the model's account of its own
work, in four different places.

### Auto-verify: the project's own check runs itself

A model that edits a file says "done" without running anything - not from
laziness, but because at 4B the thought does not occur, and a system prompt
telling it to check its work is forgotten by the third tool call. So the
harness runs the check instead. After a turn that wrote a file - once per turn,
not once per file - the project's own suite runs and a failure is put back in
front of the model to fix.

The check is found, never invented: a marker file (`pyproject.toml`,
`package.json`, `Cargo.toml`, `go.mod`), an installed runner, and for npm a
`test` script that is not the placeholder `npm init` writes. **What runs is
decided by the extension of what changed**, so a repository holding both
`pyproject.toml` and `package.json` still sends `.py` to pytest and `.ts` to
npm. It gives up rather than nagging: a suite that exceeds `VERIFY_TIMEOUT`
switches itself off, and three failures in a row stop the loop and ask for an
explanation instead of a fourth guess. Python failures come back with
`--showlocals`, so the model is handed the state that caused the failure rather
than only the line it happened on. The same failure arriving twice is counted
as one attempt going nowhere.

`/autoverify on|off`, `VERIFY_TIMEOUT`, `VERIFY_OUTPUT_CHARS`.

### `/tdd`: "make the test pass" cannot mean "edit the test"

`/tdd <request>` locks this project's test files for one request. The lock
lifts itself when the turn ends. Without it, the shortest path from a red test
to a green one runs straight through the assertion.

### Deepthink can start itself over

Stage 6 was the only stage asked to find the work wanting, and it had nowhere
to put what it found: a verify that saw half the plan undone ended the chain
and handed that report back as the answer. Now it says so - `MORE_WORK_NEEDED`,
or its own report read back in one short call - and the six stages run again
**from stage 1**, because what is left after a failed pass is a different piece
of work and planning it is the step that would otherwise be skipped.

Bounded three ways: `DEEPTHINK_MAX_PASSES` (3) is the ceiling, the next pass is
told to finish what the report named and not to widen it, and a pass with
nothing left in it ends after one turn on the existing `NO_PLAN_NEEDED` path.
The two gates lean opposite ways on purpose - unclear means "there is work" when
deciding whether to build, and "it is finished" when deciding whether to go
round again, because a chain that restarts itself on a maybe does not terminate.

### `edit_file` reads what the model meant, and refuses the rest with evidence

An anchor whose spelling can only mean one thing is repaired rather than
rejected. Everything else is refused **with the real lines attached**, so the
next attempt is made against the file instead of against memory. Hashes gained a
third character, the quoted line beats the hash when the two disagree, and a
successful edit hands back the lines around it - so the following edit needs no
re-read.

### A big MCP server is announced, not described

One `@playwright/mcp` server cost 3,549 prompt tokens (4,637 as a native
`tools` field) on every request, in conversations that had nothing to do with a
browser. A server with `MCP_LAZY_MIN_TOOLS` (6) tools or more now sends its name
and its tool names only - 186 tokens - and `use_mcp_server` sends the schemas
when the model asks for them. This is not a permission: calling a tool on a
server that was never described still works, and the call is what loads it.

### An agent that was asked a question is made to answer it

Two 4B instances in one project negotiated over a claimed file correctly right
up to the last step, where the reply was written into the answer - addressed to
the other agent, delivered to nobody - while the other agent sat waiting. The
channel now records who addressed this agent directly, `send_agent_message`
clears it, and a turn that ends with the question outstanding is told so once.
Once, not until it complies: a model that ignores the second reminder ignores
the fourth.

### Also

- README gained a comparison against other harnesses, with the claims that are
  actually enforced marked as such.
- A version already on the index no longer fails the publish workflow, so the
  hand-run `testpypi` → `pypi` → GitHub Release order stops tripping over
  itself.

---

## 0.5.0 - 2026-09-05

- **A stateful Python VM** (`run_python`): a scratch process that keeps its
  variables between calls, with ceilings on memory, output, file size and time,
  and a restart when it dies.
- **Runtime settings** (`/set`): what is settable is *derived* from `config.py`
  rather than listed, so a setting is settable the moment it exists and there is
  no second table to drift. Only the deviations are written to
  `~/.localchat/settings.json`, so an improved default still reaches anyone who
  never overrode it.
- **Prompt caching** for the hosted providers, with a test that fails when
  nothing is being read from the cache - a cache that silently stops working is
  worse than none, because the bill is the only place it shows.
- `/usage` stopped calling two different counts "turns" and now shows the fixed
  per-request cost separately.

## 0.4.2 - 2026-09-03

- **`@path` mentions**: a file, or a directory listing, attached to the message
  by typing `@` - completed from what is actually on disk, and behaving the same
  on Windows.
- **`!command`** at the prompt, and **CLI session resume**.
- CI now catches a tag that disagrees with `simple_harness.__version__`, before
  the upload that cannot be taken back.

## 0.3.0 - 2026-09-02

- **Deepthink separated finding faults from fixing them.** A stage allowed to
  fix stops looking as soon as it has something to fix, so the second half of
  its own list went unread. Review is read-only and writes a numbered list;
  revise turns the tools back on and works through it.
- **What this does to the machine is said once, before it does it** (`terms.py`).
- A failed tool result names the call that failed, and long errors are trimmed
  from the middle rather than the end.
- Tool results are trimmed only when the context budget actually needs it.

## 0.2.0 - 2026-09-01

- **`~/.localchat`**: sessions, memory, history and saved keys moved out of
  whatever directory the harness happened to start in. What stays per-project is
  what is genuinely about the project - `.permissions.json`, `.mcp.json`,
  `skills/`.
- **Installable**, Apache-2.0, named Simple Harness, published through Trusted
  Publishing with a dry-run target.
- `--help` and `--version`; the version derives from the package, so there is
  one copy of it.

## Before that

The first tagged release is `v0.2.0`; everything earlier is in the git history.
That history is where the harness got its shape: the tool registry that renders
the prompt and binds dispatch from one table, the JSON repair engine and raw
`<content>` blocks that took local-model session failures from 7/7 to 0/3,
native function calling decided per model rather than per provider, a git commit
per AI edit with `/undo`, sub-agents, MCP without an SDK, and deepthink itself.
