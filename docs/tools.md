# Tools

[← README](../README.md)

## Tool Capabilities

The client equips the model with 44 tools. They are listed in one table in
`toolspec.py`, from which both the system prompt and the dispatcher are
generated - so this list cannot quietly drift from what actually runs.

### Tool Call Format

A model with a real function-calling interface just calls the tool, and none of
this section applies to it - skip to *How tools are asked for* under Providers.

Everything below is the **text protocol**, used for models that have no such
interface. The model emits a `<tool_call>` block. Anything a parameter can hold
in one line goes in the JSON; a file body does not:

```
<tool_call>
{"name": "write_file", "arguments": {"filepath": "game.py"}}
<content>
import random

print("Guess the number!")
</content>
</tool_call>
```

Escaping a whole source file into a JSON string is the single thing small local
models get wrong most often - a bare quote inside `print("hi")`, a lost
backslash before a line continuation, one uncounted brace - and any of them used
to throw the entire generation away. A raw block removes the requirement: the
text is written exactly as it belongs on disk, with no escaping at all. `<content>`
feeds `write_file` and `run_python`; `<old_content>` and `<new_content>` feed
`edit_file`; `<stdin>` feeds `run_cmd`, `send_input` and `run_python`.

### Reading part of a file

`read_file` takes `start_line` and `end_line` as well as the path. Left out,
the whole file comes back as it always did; given, only those lines do, under
their real numbers and anchors, with `[lines 120-180 of 900]` after them. A file
too large to hand over whole was cut at a character count, part way through a
line and with no way past the cut - the half line even carried the anchor of
the half. With `RETURN_ALL_FILE_CONTENT` off it is now cut between lines, and
the cut says which `start_line` reads on.

### Editing by hashline anchor

`read_file` returns every line with a prefix:

```
50:1fa|    print(answer)
```

`50` is the line number and `1fa` is a three-character fingerprint of that line's
exact content. Both used to be decoration - `edit_file` stripped the prefix off
and matched what was left as literal text, so to change one line the model still
had to reproduce it perfectly: every space of indentation, every quote, every
backslash. That is what a small model gets wrong most often. And a line that
appears twice anywhere in the file could not be edited at all, because the
snippet was ambiguous and the edit was refused.

The prefix is enough on its own. Hand the row back with different text after
the `|`, and that is the entire edit - there is no `old_content` block:

````
<tool_call>
{"name": "edit_file", "arguments": {"filepath": "game.py"}}
<new_content>
50:1fa|    print("the answer was", answer)
</new_content>
</tool_call>
````

`50:1fa` says *which* line and proves it is the line that was read; everything
after the `|` is what it becomes. One row per line changed, and the lines need
not be next to each other:

````
<new_content>
12:a41|import sys
50:1fa|    print("the answer was", answer)
</new_content>
````

Each row replaces one line with one line, so nothing below moves and every other
anchor the model is holding stays valid - which is what makes several edits in
one call safe.

**When the number of lines changes**, or they are being deleted, that form
cannot say it: then `old_content` names the lines and `new_content` is ordinary
text.

| `old_content` | Means |
| :--- | :--- |
| `50:1fa` | Replace line 50 |
| `50:1fa\|    print(answer)` | The whole row copied out of the listing - the text beside it is the *old* line, and is only used to confirm it |
| `50:1fa` / `51:9c0` / `52:aa4`, one per line | Replace that run of lines |
| `50:1fa-53:9c0` | Replace the span, both ends checked |

An empty `new_content` there deletes the lines outright.

**The hash is what makes it safe rather than merely convenient.** A line number
on its own would happily point at whatever has since moved into that position. So
every anchor is checked against the file before anything is written, and a
mismatch is refused with what is actually there:

```
[Error] Line 50 of game.py is not what 50:1fa says it is. It now reads
50:9c0|    print(result)
The file has changed since you read it, or the anchor was mistyped. read_file it
again and use the anchors from the new listing.
```

That is the case worth having: another agent edited the file, or the model's own
earlier edit moved everything below it, and the edit lands in the wrong place
with nothing to say so. It is refused instead. When the number of lines changes,
the result says so too, so the next edit starts from a fresh `read_file`.

In the one-row form the hash is the *only* check there is - the text beside it
is what the line is to become, not what it is now - so it is never waived. In
the `old_content` form the row may also quote the line, and then **the quote
decides, in both directions.** A hash that disagrees with an exactly-correct
line beside it (`50:abd|    print(answer)`) is a slip of three hand-copied
characters and the line is believed. A hash that *agrees* while the quoted line
does not is the more interesting case: an anchor points at a position, so a
line that moved away and a different line that moved in collide once in 4096,
and the quote is what catches it. That is refused.

**A spelling that can only mean one thing is read, not refused.** `read_file`
prints a `|` after every anchor, so a model writes one after a span too, and
`6:cae-9:964|` used to fall through to text matching and come back as
"old_content was not found" - which says nothing about the anchor being one
character off. A local 4B model spent eight tool calls resending it. Now it
resolves. So does a row that names a line and quotes it with no hash at all
(`50    print(answer)`), but only when the quoted text really is that line -
which is the same evidence that forgives a mistyped hash. When it is not, the
content goes back to being matched as text.

**What is never repaired is a row with nothing behind it.** `50|    pass` in
`new_content` names a line and says what it becomes, and nothing there shows
the model has read what it is about to overwrite. That is refused - but the
refusal hands back the lines it meant, in the shape `read_file` prints them, so
the next call can be right without going to look:

```
[Error] Nothing was written. Those rows name lines but carry no hash, and the
text after the `|` is what the line is to become - so there is nothing here
that shows you have read what is already on it. game.py currently has:
  50:1fa|    print(answer)
Send it again with each anchor exactly as it appears above - 50:1fa|<the new
line> - or read_file for the rest.
```

The model is not offered a "confirm and proceed" instead. Being asked is not
being stopped, and a 4B model says yes.

**An edit hands back the lines around it, already anchored.** Editing a line
changes its hash, and changing the number of lines moves every anchor below it
- so straight after an edit the model is holding anchors that are wrong, and
its only recourse was to read the whole file again. That is a round trip, and
the whole file back into a context that is usually small, to recover a few
lines it already knows. The neighbourhood comes back with the result instead:

```
[Success] File edited: greet.py (line 3 replaced). One line became one line, so
nothing below moved and the rest of your anchors are still good.
The file now reads, around what you changed:
  1:eb1|def greet(name):
  2:cd7|    answer = "hi " + name
  3:3dd|    pass
  4:964|    return answer
  5:d41|
```

Five lines either side, merged when two edits are close and elided when they
are far apart. Nothing is inferred - this is the file as it now stands. When
the line count changed, the listing says so, because anchors *outside* it have
moved and those still need a `read_file`.

An `old_content` that is not made *entirely* of anchors is matched as text
exactly as before, and so is a `new_content` whose rows are not all anchored.
Anchors have to be certain before they take over, because falling back is always
safe and taking over wrongly is not. For the same reason, an `old_content` that
names different lines from the ones `new_content` anchors is refused rather than
half-obeyed.

Plain JSON still works. When it arrives damaged, the parser repairs what is
unambiguously safe - unclosed brackets, parameters the model put beside `name`
instead of inside `arguments`, a payload whose quotes broke the JSON around it -
and says so. What it refuses to repair is a reply that stopped early: closing
the brackets there would invent arguments that were never sent, and `write_file`
would happily write the empty result over a real file. Those are reported, and
the model is asked to send the call again.

**The envelope is repaired too.** A model with no tool-calling template of its
own does not reach for `<tool_call>`; it reaches for the nearest thing it knows.
Gemma writes a markdown fence, and it writes the name under `tool_name`, and it
nests the whole call under a `tool_call` key:

````
```tool_call
{"tool_name": "write_file", "arguments": {"filepath": "hello.py"}}
```
<content>
print('hi')
</content>
````

The JSON inside is usually byte-perfect and the raw block is byte-perfect - only
the wrapper is wrong. Reading only the literal tag found nothing there, so the
turn ended with no tool run, no error and nothing said, which is the one failure
this protocol exists to prevent. All three shapes are now read.

The refusals are what keep that honest. A fence is only read as a call when
there is no `<tool_call>` anywhere in the reply, so a correctly formatted call is
never second-guessed; and unless the fence says `tool_call` or `tool_code`
outright, what it holds has to decode to a tool that actually exists. An
ordinary ```json block in an answer stays an answer.

### When a tool fails

A failure used to arrive as the failure and nothing else - `[Error] Command
failed (exit code 1).` and a traceback. Which command? Which file? The model had
to recall what it asked for, and a small one often recalls wrong and fixes the
file it was thinking about instead of the one that broke. So the call comes with
the error:

```
[Error] run_cmd(command='python3 boom.py'): Command failed (exit code 1).
Traceback (most recent call last):
  File "boom.py", line 2, in f
    raise RuntimeError("kaboom")
RuntimeError: kaboom
```

Arguments are shortened so a file body cannot push the error off the top, and
the error's own text is kept whole - it is usually the only thing that says what
to do next. `FileNotFoundError: 'confg.py'` is a typo you can see; "a file error
occurred" is a turn spent running the command again to find out.

A long result is only shortened when the context actually needs the room, and
then **from the middle**. It used to be cut to 3000 characters on every turn no
matter how much room was left - so reading a 12,000-character file left a
quarter of it - and the cut took the end, which for a traceback is the line
that says what went wrong.

### Web & Network Tools
- `search_web`: Multi-source search with local relevance ranking (see Web Search below).
- `get_url`: Fetch web page contents and strip raw HTML down to readable text.
- `call_api`: Execute HTTP requests (GET, POST, PUT, PATCH, DELETE) with custom headers and JSON/text payloads.

### File System & Workspace Tools
- `read_file`: Read contents of a local file formatted with line numbers and line MD5 hashes.
- `write_file`: Create new files or overwrite existing file content. The body comes in a `<content>` raw block.
- `edit_file`: Replace part of a file, via `<old_content>` / `<new_content>` raw blocks. `old_content` either names the lines by hashline anchor (`50:1fa`) or quotes them as text - see *Editing by hashline anchor*.
- `multi_edit`: Several edits in one call - in one file or across several - applied all together or not at all, approved once, committed once, taken back with one `/undo`. One `<edits>` raw block of SEARCH/REPLACE blocks:

  ```text
  /abs/path/app.py
  <<<<<<< SEARCH
  50:1fa-53:9c0
  =======
  def total(items, tax):
      return sum(items) * (1 + tax)
  >>>>>>> REPLACE
  /abs/path/cart.py
  <<<<<<< SEARCH
  12:ab3
  =======
  print(total(cart, 0.1))
  >>>>>>> REPLACE
  ```

  SEARCH takes whatever `edit_file`'s `old_content` would (anchors or exact text) and may be empty when every REPLACE row carries its own anchor. Edits are applied bottom-up within each file, so every anchor is the one `read_file` gave even after an earlier block changes the line count. Permission rules and `/tdd` locks for `edit_file` apply to each file in it.
- `delete_file`: Remove a file from disk.
- `copy_file`: Copy a file to a new location.
- `move_file`: Move or rename a file in one step. A file git tracks moves with `git mv`, so the rename is one commit, `/undo` takes it back whole, and the history follows the file. Never overwrites an existing file.
- `create_dir`: Create a new directory path.
- `list_dir`: Display directory contents.
- `find_files`: Find files by name or path pattern anywhere in the project (`*.py`, `test_*`, `src/**/*.ts`) - one call instead of a `list_dir` per directory. Honours `.gitignore`; up to 200 paths.
- `search_in_file`: Search the project's files for text or a regex, grep-style. Only what git would show is searched - `.gitignore` is honoured and binaries are skipped - with an optional `glob` (`*.py`, `src/*.ts`) and `context` lines around each match. Uses ripgrep when it is installed, and gives the same answer without it.

### System & Git Management
- `run_cmd`: Execute system shell commands (requires approval). Stays connected to the command and reports when it is waiting for input.
- `send_input`: Answer a running command's prompt and read what it prints next.
- `end_process`: Stop a command left running by `run_cmd`.
- `run_python`: Run Python in a scratch process that keeps what it defines between calls, and get back what it printed plus the value of the last line. See *The Python VM* below.
- `get_system_info`: Retrieve system CPU, memory usage, disk statistics, and top memory-consuming processes.
- `view_image`: Look at an image file - a screenshot, a diagram, a chart. The image is put in front of the model together with the tool's result. png, jpeg, gif and webp; `read_file` refuses an image and sends the model here.
- `git_status`: Check current git repository status.
- `git_diff`: View current git working directory modifications.

### Memory & Interaction Tools
- `write_memory`: Save key information to persistent JSON storage. Pass `important: true` for something the model must know from the first message of every later session - who you are, how you want it to work, a standing rule about the project - and it is put into the system prompt at the start of each session instead of waiting for a `read_memory` that may never come. Saving over a memory without mentioning `important` keeps the mark it already has; `important: false` takes it away.
- `read_memory`: Retrieve content of a specific stored memory item.
- `get_memory_list`: List stored memory IDs with timestamp and preview, marking the ones saved as `important`.
- `edit_memory`: Update content of an existing memory record.
- `delete_memory`: Remove a memory entry from disk.
- `update_todos`: The model's checklist for a request of several steps, one `[ ]` / `[x]` line per item. The harness keeps it with the session and shows the open items to the model again before every request - on what is sent, never in the stored conversation - so it does not lose its place.

### Project Note Tools
Markdown notes about *this* project, kept separately from memory - memory is about you and follows you everywhere, a note is about one repository and is wrong anywhere else. One note is one `.md` file under `~/.aetheris/notes/<project>/`, so the directory can be opened in any editor. Only the titles go into the system prompt; a body is read when it is asked for.
- `write_note`: Save a markdown note about this project - why something is built the way it is, the order a job has to be done in, what is still open. The body arrives in a `<content>` raw block, never JSON-escaped. Writing under an id that already exists replaces it whole.
- `read_note`: Read one note in full by its id.
- `list_notes`: List this project's notes - id, size and when each was last written.
- `edit_note`: Replace the body of a note that already exists. `write_note` is what starts a new one.
- `delete_note`: Remove a note from disk.
- `get_user_input`: Ask the user one or more questions, each with its own list of options plus a free-text choice.

### MCP Tools
Present only when an MCP server is attached (see MCP Servers below).
- `mcp__<server>__<tool>`: Every tool each connected server exposes, with its own parameters. A server with more than a handful of tools is announced by name instead and its parameters arrive on request - see *Big servers are announced, not described*.
- `use_mcp_server`: Hand over one announced server's tools, with their parameters. Called once per server, per conversation.
- `list_mcp_resources`: List the resources the connected servers expose, with the URI needed to read each one.
- `read_mcp_resource`: Read one resource by URI.

### Agent Channel Tools
Present whenever more than one harness is running in the same project (see The Agent Channel below).
- `list_agents`: Who else is working here right now, on what, and which files each is holding.
- `send_agent_message`: Say something to one of them, or to all of them.
- `claim_files`: Announce files you are about to change, so nobody else changes them meanwhile.
- `release_files`: Hand them back.

### Workflow Tools
- `use_skill`: Load the full instructions of a skill listed in the system prompt.
- `submit_plan_for_approval`: Present a task plan and diff blueprint for approval before executing (plan mode).
- `get_code_skeleton`: Return a JSON outline of a source file's structure via Tree-sitter.
- `query_ast_node`: Search a source file for Tree-sitter S-expression patterns.
- `spawn_agent`: Hire a second model for one self-contained job. See below.

### Sub-agents

`spawn_agent` starts a fresh conversation - its own system prompt, its own
history, its own tool loop - gives it a written brief, and returns its final
report as the tool result. The user never sees the sub-agent's working; the
assistant never sees it either, only the report.

It is for work whose *output* matters and whose *process* does not: finding
where something is handled across a codebase, reading six files to answer one
question, checking a list of URLs. Twenty tool results that will never be needed
again fill the sub-agent's context instead of the conversation's.

```
spawn_agent(task="Find every place a session file is written, and report the
                  file and line of each.",
            context="I already know session.py:save_session is one of them.",
            model="qwen3:8b")        # optional - a cheaper model for a long search
```

What it may do:

- every tool the assistant has, except `get_user_input`, `submit_plan_for_approval`
  and `spawn_agent` itself. It has nobody to ask, no plan to submit, and hiring
  chains have unbounded cost;
- nothing the assistant could not have done. Its tool calls go through the same
  permission rules and raise the same approval prompts. A sub-agent is not a way
  around a `deny` rule;
- at most `SUBAGENT_MAX_TURNS` turns (12 by default), after which it is asked for
  a report from what it has rather than being cut off mid-search.

Starting one asks for approval, like running a command does - it costs a stretch
of time, and on a hosted model real money, before it reaches its first tool.
Allow it permanently with an `allow` rule for `spawn_agent`.
