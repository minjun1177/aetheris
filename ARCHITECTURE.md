# ARCHITECTURE

For anyone - person or model - about to change this codebase. It says where
things are, what may not be broken, and what to touch for the change you have in
mind. `README.md` says what the harness does and how to use it; this says how it
is put together.

Read §2 and §5 before editing anything. They are short and they are where the
mistakes are.

---

## 1. What this is

A terminal AI assistant, ~16,600 lines of Python, no framework. It talks to
Ollama, Anthropic, OpenAI and Gemini over plain HTTP (no vendor SDKs), gives the
model 40 tools, and runs them with the user's approval.

The design constraint that explains most of the odd decisions: **it has to work
with a 4-billion-parameter local model.** Such a model cannot reliably escape a
source file into a JSON string, cannot be trusted to follow "do not edit yet",
and will report success it did not verify. Wherever you see machinery that looks
like distrust of the model, that is what it is, and it is load-bearing.

---

## 2. One turn, end to end

The single most useful thing to know. Follow it once and most of the codebase
falls into place.

```
app.main()                                      app.py
│
├─ read a line from the user
├─ if it starts with "/" → slash command, continue the loop
│
├─ channel.turn_note()  → what the other agents here said   channel.py → §8
│  appended as its own user message, ahead of the next line
├─ messages.append({"role": "user", "content": <what they typed>})
├─ save_session(messages, id)                   session.py
│
├─ config.DEEPTHINK ? deepthink.run(messages)   deepthink.py  → §7
│                   : chat_turn(messages)       llm_client.py
│                     └─ context.manage_context first
│
└─ chat_turn(messages)                          llm_client.py
   │
   │  loop until the model stops calling tools:
   │
   ├─ channel.turn_note(mid_turn=True)  → anything said since the last
   │  request, appended before this one. Not on the first pass: what
   │  the person typed has to stay last there.            channel.py → §8
   ├─ tools = native_tools() if the provider supports it else None
   ├─ text = await stream_reply(messages, tools=..., calls_out=[])
   │  │
   │  └─ providers.current().stream(messages, tools=...)     providers.py
   │     yields {"text":…} {"thinking":…} {"tool_call":…} {"done":…}
   │     · text is printed through StreamFilter (hides <tool_call>, <think>)
   │     · thinking is printed only if SHOW_THINKING, never stored
   │     · tool_call events land in calls_out
   │     · done carries the token counts → context.observe_usage
   │
   ├─ calls = _from_native(calls_out) if calls_out
   │          else parse_tool_calls(text)          ← the JSON repair engine
   │
   ├─ if no calls → return the text. The turn is over.
   │
   └─ for (name, arguments) in calls:
      ├─ result = tools.dispatch_tool(name, arguments)        tools.py  → §4
      └─ messages.append({"role": "user",
                          "content": f"[Tool Result for '{name}']:\n{result}"})
      loop
```

Two things to notice, because a lot depends on them:

**A tool result is a `user` message, not a `tool` message.** The history is a
flat list of `{"role", "content"}` and nothing else, whatever the provider.

**A native tool call is written back into the history as `<tool_call>` text**
(`llm_client._render_call`). So a session recorded against Anthropic replays
against Ollama, the summariser and token estimator only ever see one format, and
`session.py` needs no notion of tool calls at all.

---

## 3. The data shapes

Four shapes carry everything. Keep them exactly.

**Message** — the only history format. There is no other.

```python
{"role": "system" | "user" | "assistant", "content": str}
```

`content` is always a plain string. A tool result is a user message beginning
`[Tool Result for '<name>']:\n`. `messages[0]` is always the system message.

**Stream event** — what every provider yields, whatever it speaks.

```python
{"text": str}                    # answer text, as it arrives
{"thinking": str}                # scratch work; never enters history
{"tool_call": {"name": str, "id": str, "arguments": dict,
               "error": str}}    # "error" only when the arguments would not parse
{"done": True, "prompt_tokens": int, "completion_tokens": int,
 "cached_tokens": int,          # prompt tokens the provider served from its
                                # own cache; 0 on Ollama, which reuses its KV
                                # cache locally and reports nothing
 "total_seconds": float, "eval_seconds": float}
```

A provider may yield any of these in any order. `done` comes last.

**Tool call** — what dispatch takes, from either protocol.

```python
(name: str, arguments: dict)
```

**Tool spec** — `toolspec.Tool` and `toolspec.Param`. One table, two renderings:
`prompt_schema()` for the text protocol, `native_schema()` for the real one.

---

## 4. `dispatch_tool` - every tool call goes through here

`tools.dispatch_tool(name, arguments)` in this order. Anything that must apply
to *all* tools belongs here and nowhere else.

1. **Normalise** — a string `arguments` is JSON-parsed; anything else becomes `{}`.
2. **Display** — `_fmt_tool_call`.
3. **Deepthink read-only gate** — refuses a world-changing tool during a
   planning stage (§7).
4. **Another agent's claim** — `_claimed_by_another` refuses a write to a file
   a different harness in this project is in the middle of changing (§8).
5. **Permission rules** — `deny` returns without reaching the handler; `allow`
   sets `config.POLICY_AUTO_ALLOW` so the handler's approval prompt passes.
6. **Run** — `_run_tool` looks the name up in `toolspec`, binds the arguments
   through it, calls the handler. MCP tools and the two MCP resource tools are
   handled after that lookup fails.
7. **Auto-commit** — `_commit_if_changed` commits a file the tool changed, if
   the tool reported `[Success`.
8. **Claim what changed** — `channel.note_write` holds those same files for a
   few minutes, so the next agent to walk into one is stopped and told who to
   ask. Both this and the commit read `_paths_written`, so they cannot disagree
   about what a call actually wrote.
9. **Note it for auto-verify** — `verify.note_written`, reading the same
   `_paths_written`. Noted, not run: the project's check belongs after the
   whole reply's tool calls, not between two of them, so `chat_turn` runs it
   (§7a).
10. **Name the failure** — `_name_the_failure` puts the call in front of an
   `[Error]` result: `[Error] run_cmd(command='python3 boom.py'): Command
   failed (exit code 1).` Arguments are truncated so a file body cannot push
   the error off the top. `[System]` is left alone - see below.

A refusal from step 3, 4 or 5 returns a string starting with `[System]`. That
prefix is a contract: `chat_turn` counts consecutive `[System]` results and ends
the turn after three, so a model cannot spend its whole budget on a closed door.
Step 10 rewrites `[Error]` and never `[System]`, because rewriting the prefix
would break that counter. It is also why an auto-verify failure is appended as
a *message* rather than returned as a tool result: it starts with `[System]`
too, and three of them in a row would end the turn at exactly the point the
model was being asked to keep working.

---

## 5. Invariants

Each of these was a bug once. Breaking one is silent.

**5.1 The tool table is the only tool list.** `toolspec.TOOLS` generates the
system prompt *and* binds dispatch arguments. `tools._check_registry` raises at
first dispatch if a tool has no handler, a handler is undescribed, or a
handler's arity does not match its spec. Never hardcode a tool list anywhere
else.

**5.2 `config.py` builds the system prompt at import time** (`SYSTEM_PROMPT = syp()`).
So everything `systemprompt.py` reaches at module level - `toolspec`,
`mcp_client`, `skills` - **must not import `config` at module level.** They read
it lazily inside functions instead. Add a module-level `import config` to any of
them and the whole program stops importing.

**5.3 History is plain text, always.** No structured tool blocks, no
provider-specific shapes. Native tool calls are rendered back to `<tool_call>`
text before being stored. Break this and session files, `/load`, context
compression and the token estimate all need a second code path.

**5.4 Thinking never enters history.** `strip_thinking` runs before the
assistant message is appended, unless `STORE_THINKING`. On a local model the
reasoning is often longer than the answer, and the context budget is small.

**5.5 Ollama keeps the text protocol when its model cannot do better.**
`OllamaProvider.supports_native_tools` is a per-model property, not a class
attribute. A model whose template cannot format a tool call will never make one;
the text protocol plus the JSON repair engine is the only thing that works for
it. Anything that fails - daemon down, model unknown - means text.

**5.6 Tool-call repair never invents an argument.** `llm_client` will close
brackets a model left open, but a reply that *stopped mid-value* is reported as
unparseable instead. Guessing there would let `write_file` write an empty string
over a real file.

The envelope is repaired on the same terms. A model with no tool-calling
template writes the call in a markdown fence, or names it `tool_name`, or nests
it under a `tool_call` key - all of which used to parse to nothing, ending the
turn with no tool run and no error. `_fenced_tool_calls`, `_rename_to_name` and
`_unwrap_envelope` read those, and each is deliberately narrow: a fence is only
considered when the reply holds no `<tool_call>` at all, an unlabelled fence has
to decode to a tool that exists, and a lone key is only an envelope when its
only value is a dict carrying a name. Widening any of them turns an answer's
code block into a tool call.

**5.6a An allow rule stops at the command it names.** `run_cmd` goes through a
shell, and rules match text, so `run_cmd(git status)` also matches
`git status && rm -rf ~`. `permissions.chains_a_second_command` downgrades that
to the prompt. It never touches `deny`, and it never turns an `ask` into
anything stricter. An empty pattern - `write_file()` - is rejected at load
rather than matching everything, which is what it used to do.

**5.7 Files that must survive a crash are written through `atomic.py`.**
Sessions, memory, permission rules, saved API keys. `open(path, "w")` truncates
before it writes; being killed in between empties the file. Use
`atomic.write_json(path, data, private=True)` for anything holding a secret -
`private` keeps it owner-only for the whole write, which `open()` then `chmod`
does not. POSIX only: Windows has no mode bits, so `private` buys nothing there
and `test_durability.py` says so instead of asserting a "600" that cannot
exist.

**5.6b A tool result is trimmed only as much as the budget forces, and from the
middle.** Two separate bugs lived here.

Trimming ran at a flat 3000 characters on every `manage_context`, whatever the
budget: with a 65536 context and 49,000 tokens spare, a 12,000-character file
read came back as a quarter of itself and the rest was gone for good. It is
budget-driven now - `_TRIM_STEPS` is tried loosest first and stops at the first
ceiling that fits, with 24000 as a plain backstop because `read_file` has no
ceiling of its own.

And it kept only the front, which for a traceback means keeping "Traceback
(most recent call last):" and deleting "RuntimeError: kaboom" - the one line
that says what went wrong. Both ends are kept now; the front still matters for
the results that are not errors.

`manage_context` runs once per turn, not inside the tool loop, so neither bug
touched a result the model had just asked for - they shredded what it had read
*earlier*. Deepthink is where that hurts, because each of its six stages is
another `manage_context` over the same conversation.

**5.7a State that is about the person goes in `paths.home()`; state that is
about a project stays with the project.** Sessions, memory, input history and
the saved keys are the person's, and resolve under `~/.aetheris` - never to a
relative path. `.permissions.json`, `.mcp.json` and `skills/` are the
project's, and are read from the working directory first so a repository can
carry its own and win.

Writing the first group relative to the working directory is what this used to
do, and it only looked harmless while the harness was run as `python app.py`
from its own checkout: as an installed command it scattered files wherever you
happened to start, and split one memory into one per directory. `paths.py` is
stdlib-only and imports nothing local, so the modules below `config` can use it
without breaking 5.2.

Nothing is ever moved on the user's behalf. `sessions` and `memory.json` are
ordinary enough names that acting on sight would eventually destroy real work,
so `strays_in_cwd()` reports and stops.

One session file still records a directory: the one it was last saved from.
That is the only thing `-c` has to go on. Sessions are the person's and stay in
one place, so "the session I was in here" cannot be answered by where the file
lives - it has to be written down when the session is. `session.latest_in_dir`
compares them through `realpath` and `normcase`, so a symlink or a differently
cased spelling of the same directory is the same directory; a file written
before the format recorded one is never picked, because a wrong guess at which
conversation to reopen is worse than saying there is none.

**5.8 Auto-commit takes only the paths the tool named.** `git commit` is given
those paths explicitly. Never let it sweep up the index - the user's staged work
is not ours to commit.

**5.9 `[System]` prefixes a refusal.** See §4. `[Error]` prefixes a failure, and
both are `config.TOOL_REFUSAL_PREFIX` and `config.TOOL_ERROR_PREFIX` - one copy
each, because `tools` writes them, `llm_client` counts them and `tui` colours
them, and none of the three can import the others.

They are **anchors, tested with `startswith`, never searched for**. Everything a
tool returns is content somebody else wrote: a page `get_url` fetched, a file
`read_file` read, whatever `run_cmd` printed. Any of it may contain either
marker as ordinary text - this repository's own source contains dozens - and a
substring test turns that into a failure report under output that was perfectly
good. `tui._fmt_tool_result` searched the whole body until it was made to match
`tools._name_the_failure` and `_commit_if_changed`, which had always anchored.

The same rule covers the terminal generally: **nothing writes to stderr while a
tool is running unless the tool failed.** A dependency's warning printed
mid-search is indistinguishable, to the person watching, from the tool breaking.
`websearch` is where this bites - see the top of that module and `strip_html`.

**5.10 Never report success you did not verify.** This applies to the code as
much as to the model: `deepthink._report_checks` counts the commands the final
stage actually ran, from the tool results, and prints that *after* the model's
prose - because the model will claim success it did not earn, and the reader
believes the last line.

**5.12 An anchor is verified or it is not used.** `read_file` returns
`50:1fa|    print(answer)`, and `edit_file` reads that row back two ways:
`tools._parse_patch` takes `38:ff7|print()` in `new_content` as a whole edit -
which line, and what it becomes - and `_parse_anchors` takes `50:1fa` in
`old_content` as the target for a replacement that changes the number of lines.

The line number alone would be a loaded gun: it points at whatever has since
moved into that position, and the model's own previous edit is enough to move
it. So every anchor's hash is checked against the file first and a mismatch is
an `[Error]` naming what is actually on that line, never a write. The one-row
form is the strict one - the text beside the anchor is the *new* line, so the
hash is the only evidence there is and it is never waived.

The parse fails *closed into the old behaviour*: one ordinary line anywhere in
`old_content` and the whole snippet is matched as text, exactly as before.
Falling back is always safe; taking over wrongly is not.

When the row also quotes the line, the quote decides and the hash does not -
in both directions. A wrong hash beside an exactly-correct line is accepted,
because three hand-copied hex characters are easier to get wrong than the line
itself. A hash that *agrees* while the quoted line does not is refused, which
is the case that matters: `HASHLINE_DIGITS` is 3, so an anchor pointing at a
position collides once in 4096 with whatever has moved into it, and the quote
is the only thing that notices. The digits went from 2 to 3 for that reason -
465 tokens on this repository's largest file, against a 1-in-256 silent
overwrite. The patterns still *read* a two-character digest, which can never
match, so a stale anchor gets "line 50 is not what 50:1f says it is; it now
reads 50:1fa|..." rather than falling through to a text match that says
nothing.

Around that sits a repair layer, in the spirit of tool-call repair (5.6):
a spelling that can only mean one thing is read rather than refused. `read_file`
prints a `|` after every anchor, so a model writes one after a span too -
`_ANCHOR_SPAN_PIPED` reads `6:cae-9:964|`, and `_parse_unhashed_anchors` reads a
row that names a line and quotes it with no hash at all - `3     print(answer)`.

Which reading was meant is not decidable from the row: `6:cae|def f():-9:964|`
is a span, and equally one anchor whose quoted text contains `-9:964|`. So
`_anchor_target` does not guess. It collects every reading that parses, asks
`_anchor_problem` whether each one *describes the file*, and takes the first
that does; when none does, it complains about the first, which is the one the
model most likely meant. A reading made only of hash-less rows that does not
describe the file is not an error at all - that is ordinary text starting with
a number, and it goes back to being matched as text.

What is never repaired is a row with no evidence at all. `3|    pass` in
`new_content` names a line and says what it becomes, and nothing there shows the
model has read what is being overwritten - so it is refused, and the refusal
carries the lines it meant, rendered exactly as `read_file` prints them
(`tools._show_lines`) so the next call can be right without a round trip to go
and look. A model is never offered a "confirm and proceed" path instead: being
asked is not being stopped, and a 4B model says yes. The same reasoning as the
read-only planning stages (§7).

A successful edit ends with `_echo_region`: the changed lines and five either
side, rendered as `read_file` prints them. Editing a line changes its hash and
changing the line count moves every anchor below, so without this the model is
holding wrong anchors the moment it succeeds, and its only recourse is to read
the whole file back into a small context. Windows merge when edits are close
and elide when they are far apart, and the listing says so when anchors outside
it have moved. It states the file, so it can be trusted the way `git diff` in
deepthink's review stage can (5.10) - nothing here is remembered or inferred.

**5.13 A tool the model cannot see is still a tool it may call.** A server's
tools cost 3,549 tokens of prompt or 4,637 of `tools` field on *every* request
(measured, one 24-tool playwright server), so `mcp_client` announces a big one
by name - 130 tokens - and `use_mcp_server` hands over the parameters when the
model asks. What that must never become is a permission system. `resolve_tool`
and `dispatch_tool` are untouched: a call to an unannounced server's tool runs,
and `note_call` loads that server so the next one is not a guess. A saving that
can refuse a call is not a saving.

Two things fall out of it. `_tool_entries` and `_raw_input_schemas` are zipped
by `native_tool_schemas`, so they filter through the same `shown_servers()` -
filtering one and not the other would pair a tool's name with another tool's
parameters. And the loaded set is read back out of the history
(`loaded_in`, `context._sync_loaded_mcp_servers`) for the reason
`LOADED_SKILLS` is: the compressor can drop the message that carried a
server's tools, and a set kept only in memory would go on claiming they were
sent.

**5.11 Two harnesses in one project must not silently overwrite each other.**
Every instance is its own process, so nothing about the conversation can tell
you another one exists. `channel.py` is the only place that knows, and the only
thing that makes it real is the refusal in `dispatch_tool` step 4 - a board
that agents can read and ignore prevents nothing.

The claim is taken automatically by whoever writes a file, not only by
`claim_files`, for the reason that runs through this whole codebase: the
protection cannot depend on the model having thought to ask for it.

It fails open, everywhere. A board that will not parse, a lock that cannot be
taken, a home directory that cannot be written - each of those lets the write
through. Coordination is worth a refusal; it is not worth a harness that cannot
edit a file because a JSON file in `~/.aetheris` is malformed.

Answering is chased the same way the rest is enforced. `turn_note` records the
agents that addressed this one directly (`awaiting_reply`), a successful
`send_agent_message` clears it, and `chat_turn` nudges once when a turn ends
with the question outstanding. It is there because two 4B models were run at
each other and the holder replied *in its own answer* - "you have my agreement
to proceed" - addressed to the other agent and delivered to nobody. A broadcast
never sets it: news needs no answer.

**5.14 The public surface only grows.** Five things belong to the people using
this and not to whoever is editing it: slash command names, `/set` setting
names, tool names, the `~/.aetheris` layout, and the project files
(`.permissions.json`, `.mcp.json`, `skills/<name>/SKILL.md`) that are read from
the working directory first. `tests/test_compat.py` lists all five and fails
when one disappears; adding is free and reported, never failed.

Two of them are less obviously public than the rest. A **setting name** is
public the moment it exists, because `/set` derives its list from `config.py`
rather than a table - so a rename there renames a key in somebody's saved
`settings.json`. A **tool name** is written into the history as `<tool_call>`
text whatever protocol produced it (§2), so renaming one does not just change a
prompt: it stops an old session from replaying. Nothing inside
`aetheris.*` is covered - the modules are an implementation, and the
reusable pieces are meant to leave for packages of their own.

While the major version is 0 this is a record rather than a guarantee, and
`CHANGELOG.md` says so in as many words. It exists from 0.6.0 so that 1.0.0 can
be a promise already kept rather than one made on the day.

**5.15 A memory the model must not miss is in the prompt, not waiting to be
looked up.** Long-term memory was a store with a door on it: `write_memory` put
something in, and `read_memory` took it back out - if the model thought to ask.
A fresh session has no reason to think of it. Nothing in an empty conversation
says there is anything to find, so the fact that the user writes in Korean, or
that this project is never pushed to `main` directly, sat in `memory.json`
being correct and unread.

`write_memory` takes a third argument for that. `important=true` marks the
entry, `session.memory_prompt_section()` writes every marked entry out, and
`app._compose_system_prompt` puts that block into the system message - so it is
there before the user's first line, on a session started fresh and on one
resumed, without a tool call. Everything unmarked keeps working exactly as it
did.

Three things make it safe to put there:

* **The mark belongs to the memory, not to the call.** Saving over an entry
  without mentioning `important` keeps the mark it has; only an explicit
  `false` takes it away. A model rewriting "User name" a month later is not
  making a decision about whether the user still wants to be remembered.
* **It is bounded, and says when it cut.** `MEMORY_IMPORTANT_MAX` entries and
  `MEMORY_IMPORTANT_CHARS` each. This is prompt, which is paid for on every
  turn of every session; a model that marks forty things important should not
  quietly take a tenth of the context window. What is cut is named as cut, so
  the model knows to call `read_memory` rather than trusting half a sentence.
* **It is byte-identical between builds**, which 5.10 requires of everything in
  the prefix: the file's own order, no clock, no set iterated into text. Marking
  a memory important mid-session costs one cache miss at the next prompt
  rebuild, the same as loading a skill - and the prompt is deliberately not
  rebuilt every turn, so it costs nothing until then.

It is read in `_compose_system_prompt` rather than inside `systemprompt()` for
an import reason and not a design one: `config` builds the system prompt at
import time, and `session` imports `config`, so `systemprompt` cannot reach
`session` without a cycle. Composing is where every other per-conversation
piece already goes.

**5.16 What is true about a project is kept per project, as markdown, and the
file is the record.** 5.15 put what the model knows about the *person* in front
of it. The other half is what it knows about the *repository* - why the anchor
hash has three characters, the order the deploy runs in, the three questions
nobody has answered - and none of that belongs in a store shared by every
project, because it is not merely unhelpful elsewhere, it is wrong elsewhere.
It is also prose with headings and a code fence in it, which is not what JSON
is for.

So `notes.py` is a second store with different rules. `channel.workspace()`
decides what a project is, which is the git working tree or the current
directory when there is none, so a terminal in `src/` sees what one at the root
sees; `paths.workspace_slug` names the directory, and it is the same name the
channel board is filed under, so the two cannot come to different conclusions
about where a project ends.

One note is one file - `~/.aetheris/notes/<project>/<id>.md` - and the file is
the whole record: its name is the id, its mtime is when it was written, its
bytes are the content. There is deliberately no index beside it. An index is a
second place for "which notes exist" to be true in, and this one could disagree
with the directory the moment a person deleted a file by hand, which they can,
because the point of markdown on disk is that it opens in an editor.

Three things follow the same reasoning as 5.15:

* **The body travels as a raw block.** `<content>`, the way file bodies do. A
  note is the one kind of text most likely to contain a fenced code block, and
  asking a 4B model to JSON-escape one is asking for the failure this harness
  exists to avoid.
* **The prompt gets titles, never bodies.** A note can be a page; forty of them
  would be unbounded context charged on every turn. What a fresh session lacks
  is not the content but the knowledge that the content exists - one line per
  note buys exactly that, and `read_note` buys the rest.
* **The title list is sorted by id.** Not by mtime. This is a cache prefix
  (5.10), and ordering it by time would reshuffle the prompt every time a note
  was written, for nothing.

A note id is chosen by the model and becomes a filename, so it goes through
`paths.safe_name` - the same slug a session title gets, for the same reason.
`../../.ssh/config` loses every separator on the way through, and `note_path`
then refuses anything whose parent is not the notes directory, so loosening the
slug later cannot quietly turn an id into a way to write anywhere on the disk.

**5.17 An image rides beside the text, as a path, and the model is asked
whether it can see before one is sent.** Three of the four providers take
images, and so do a fair number of local models - Ollama reports `vision` in
the same `capabilities` list that says whether a model can call a tool, and the
4B-class model this harness is benchmarked against has it. Two things had to be
decided to make that work here, and both of them were decided by things already
in this file.

*Where an image lives on a message.* `content` is a string everywhere:
`merge_runs` concatenates it, `context` trims it, `session` replays it, and 5.3
requires the stored history to be plain text so that one format reaches all of
them. Turning content into a block list to hold a picture would have rewritten
every one of those. So an image is a sibling key, `message["images"]`, and
`providers.py` builds the four wire shapes on the way out - Ollama already uses
that exact key, Anthropic gets image blocks before the text block, OpenAI gets
a `data:` URL part, Gemini gets `inline_data`. `merge_runs_with_images` exists
because merging keeps only role and content, which would have thrown an
attachment away the moment a tool result landed on top of it.

*What the key holds: paths, not bytes.* A session file is written after every
turn. A 400KB screenshot is 550KB of base64 in every saved copy of every
conversation it appears in; the path is forty bytes. So the bytes are read at
send time and never stored, and an image deleted between one session and its
resumption is skipped rather than fatal - the rest of the conversation still
answers.

*Asking first.* `Provider.sees_images()` is True for the hosted three and
overridden for Ollama, and the reason it is worth a probe at all is that this
is the one capability whose absence is **silent**. A hosted API rejects an
image it cannot take and says so. Ollama drops it on the way in, and the model
then answers about the sentence alone - fluently, at length, about a picture it
was never shown. A wrong answer with no error in it is the worst outcome
available, so the check happens before the request and the refusal names
installed models that would have worked.

The three ways in follow the same rule, that a person should not have to know
any of the above. `@shot.png` attaches rather than pasting bytes into the
prompt; `read_file` on an image refuses and names `view_image`, because a small
model reaches for `read_file` for everything; and `view_image` cannot hand a
picture back through a string, so it leaves the path with `images` and
`llm_client` hangs it on the tool-result message it is already appending - the
same arrangement `verify` uses for a check it wants run after the turn.

---

## 6. Module map

Every module lives in `aetheris/`, and imports it by the package name:
`from aetheris import config`, `from aetheris.tui import _hr`. The
tree below leaves that prefix off for readability; on disk `app.py` is
`aetheris/app.py`. Tests and the two documents sit outside the package.

Layered by what may import what. A module may import anything above it.

```
app.py            the loop, slash commands, session lifecycle
  └ llm_client.py chat_turn / stream_reply / the JSON repair engine
      └ tools.py  dispatch_tool + every tool handler
      └ context.py token budget, trimming, compression
      └ verify.py the project's own check, run after a turn that wrote a file
  └ deepthink.py  the six-stage chain
  └ subagent.py   spawn_agent's own conversation loop
  └ channel.py    the board the harnesses in one project share
      └ vm.py     the Python scratch process behind run_python
      └ providers.py  four wire formats → one event shape
          └ sse.py    server-sent events, read as they arrive
          └ atomic.py crash-safe writes
  └ config.py     every setting and its default, the saved overrides read
                  over them, plus SYSTEM_PROMPT built at import
      └ systemprompt.py  the prompt text, both protocol variants
          └ toolspec.py  the tool table (stdlib only, imports nothing local)
```

| File | Owns | Do **not** put here |
| :--- | :--- | :--- |
| `app.py` | The REPL, slash commands, session save/rename, prompt refresh | Anything a tool does |
| `llm_client.py` | The turn loop, stream filtering, tool-call parsing and repair | Provider wire formats |
| `providers.py` | HTTP, streaming, per-vendor tool encoding, saved connection | Anything about *which* tools exist |
| `toolspec.py` | Names, descriptions, parameters, both schema renderings | Handlers, imports of other modules |
| `tools.py` | Handlers, dispatch, approval prompts, the hashline anchor (5.12) | Tool descriptions - those are in `toolspec` |
| `systemprompt.py` | Prompt text; `tool_rules(native)` shared with `subagent` | A second copy of anything |
| `deepthink.py` | Stage list, stage instructions, stage gating, when the chain repeats | Tool logic |
| `subagent.py` | The sub-agent's own loop and prompt | A second protocol |
| `git_ops.py` | Commit, undo, diff. Never raises | Anything not about git |
| `verify.py` | Which check a project declares, running it, and the wording of a failure | When to run it or how many times - that is `chat_turn` |
| `channel.py` | Who else is running here, what they said, what they hold | Anything about one conversation |
| `context.py` | Token estimate, trimming, compression, and folding the token history into turns | |
| `session.py` | Session files, the directory each was worked in, long-term memory, and the block the important ones make (5.15) | Where that block is put - `app` composes |
| `notes.py` | A project's markdown notes: where they live, the five tools over them, and the block of titles (5.16) | What counts as a project - `channel.workspace` answers that |
| `images.py` | Recognising an image, resizing one too big for an API, and the base64 a provider sends (5.17) | The four wire shapes - `providers` builds those |
| `mentions.py` | `@path` in a typed message: what it names, and what it attaches | Printing - the caller does that |
| `permissions.py` | Rule loading and the allow/deny/ask decision | |
| `shell_session.py` | Live commands, waiting-vs-busy detection | |
| `vm.py` | The scratch Python process: its wire protocol, its ceilings, and restarting it after it dies | How the result is worded - that is `tools.handle_run_python` |
| `mcp_client.py` | MCP transports, JSON-RPC, MCP tool schemas, which servers are described now | Whether the *built-in* tools are shown - that is `toolspec` |
| `websearch.py` | Retrieval, extraction, BM25 reranking | |
| `skills.py` | Skill discovery and loading | |
| `tui.py` / `renderer.py` | Terminal chrome and markdown | Decisions |
| `atomic.py`, `sse.py`, `paths.py` | One job each. Stdlib only, importing nothing local. `paths` also owns the two names derived from something that was not chosen to be a filename: `safe_name` and `workspace_slug` | |
| `terms.py` | What the harness does to this machine, asked once before it does it | |

---

## 7. Deepthink

`deepthink.run(messages)` replaces one `chat_turn` with six, each preceded by a
stage instruction appended as a user message. All six share the one
conversation.

```
1 plan     read, decide, change nothing        edits = False
2 check    argue against the plan               edits = False
3 build    carry it out                         edits = True
4 review   read the real git diff, list faults  edits = False
5 revise   fix what stage 4 listed              edits = True
6 verify   run it, check it against the plan    edits = True
```

**4 and 5 are separate on purpose.** Review used to fix what it found, and a
stage that may fix stops looking once it has something to fix - the rest of its
own list went unread. Review is read-only now and its output is a numbered list;
revise works through that list and is told not to widen it. If the list is
empty, revise changes nothing and says so.

**The chain can start itself over.** When stage 6 reports the work unfinished -
`MORE_MARKER` on its last line, or `_needs_another_pass` reading that report back
in one short call - `run` recurses with `pass_number + 1` and the six stages run
again from stage 1. Not from the middle: what is left after a failed pass is a
different piece of work, and stage 1 is what plans it. The recursion is bounded
twice over - `config.DEEPTHINK_MAX_PASSES` (3) is the hard ceiling, and
`CARRY_OVER` tells the next pass to end with `STOP_MARKER` if the report named
nothing left, so a pass with no work in it costs one turn rather than six. The
two gates lean opposite ways on purpose: `_needs_building` treats anything
unclear as work to do, `_needs_another_pass` treats anything unclear as finished,
because a chain that restarts itself on a maybe does not terminate.

Five mechanisms make it more than a prompt:

- **`config.DEEPTHINK_READONLY`** is set for every stage with `edits = False` -
  1, 2 and 4 - and `dispatch_tool` refuses anything in `tools._CHANGES_THINGS`.
  Asking a model to hold off does not hold it off; a 4B model tried to edit
  fifteen times before this existed. Adding a stage does not need this rewired:
  the flag is `not stage.edits`.
- **Stage 4 is handed `git_ops.diff_since(sha)`**, the real patch, not a request
  to recall what it changed. Without git it is told to re-read the files.
- **Stage 6 is pointed back at the plan**, item by item, not just at the
  request. Code that runs and is not what was agreed is still not finished.
- **`_report_checks`** counts the commands stage 6 actually ran and prints the
  truth after the model's summary (invariant 5.10).
- **`run(messages, pass_number)`** is one function calling itself, so a repeat
  pass is the same six stages with the same gating rather than a second code
  path that would drift from the first.

Two early exits: a plan with nothing to build ends the chain after stage 1
(`_needs_building`, which believes the `NO_PLAN_NEEDED` marker for free and
otherwise asks one short question about the plan text); a build that changed
nothing ends it at stage 4 rather than reviewing work that never happened.

---

## 7a. Auto-verify

`verify.py` decides *what* the check is and runs it; `chat_turn` decides *when*
and *how often*. That split is the whole design, and each half is uninteresting
on its own.

```
dispatch_tool  →  verify.note_written(paths)     # a set, per turn
chat_turn      →  verify.run_pending()           # once, after the whole reply
                  ok      → nothing, unless it had been failing
                  not ok  → verify.failure_message(report) appended as a message
                  3 in a row → verify.gave_up_message(), and off for this turn
```

- **The check is chosen by the written file's extension**, not by the first
  marker found. A repository with both a `pyproject.toml` and a `package.json`
  runs pytest for a `.py` file and npm for a `.ts` one, with no precedence rule
  to get wrong.
- **Nothing is invented.** A marker file has to exist, the runner has to be
  installed, and `npm test` has to be a real script rather than the placeholder
  `npm init` writes. `_why_not` is asked once per project and the answer cached
  in `_off` - it is a fact about the machine, and probing costs a subprocess.
- **The marker search stops at the git working tree.** A `package.json` in a
  parent directory belongs to somebody else's project.
- **`ok is None` is not a failure.** A timeout or a runner that would not start
  is never shown to the model - it says nothing about the code that was just
  written - and it puts that project in `_off` rather than costing the same
  wait after every edit. `/autoverify on` calls `verify.reset()` to try again.
- **`clear()` runs at the top of `chat_turn`, and only at depth 0.** A
  sub-agent runs inside one of its parent's tool calls; clearing there would
  throw away the parent's pending check.

`MAX_VERIFY_FAILURES` lives in `llm_client` beside `MAX_REFUSALS_IN_A_ROW`, for
the same reason: past three, another round is not converging. It counts *the
same* failure, not failures - a turn that starts with a red tree burns two of
its three lives on problems the model did not cause, which is what happened in
the run this rule came from. A failure whose text differs is progress and
starts the budget again; `MAX_TOOL_CALLS` still bounds the turn either way.

`/tdd` is the other half of the same loop. It holds `deny` rules for
`verify.TEST_PATTERNS` in `permissions` (`hold` / `release`, memory only, never
a file) for exactly one turn, released in the turn loop's `finally` so an error
or a Ctrl-C cannot leave a project locked. `config.TDD_LOCK` says it is armed;
`TDD_VERIFY_FAILURES` gives the loop a longer budget while it is. Enforcement
is `dispatch_tool` step 5, unchanged - the mode adds no new gate, it writes
rules for the gate that is already there. `run_cmd` is outside it for the same
reason it is outside the channel's claims: what a shell command writes is not
knowable from the call.

This does not replace deepthink's stage 6, and the overlap is deliberate. Stage
6 asks a different question - whether what runs is what was *planned* - and it
runs the suite itself through `run_cmd`, which is what `_report_checks` counts
(5.10). Auto-verify only ever answers "does it still pass", which is worth
knowing at stage 3 rather than three stages later.

---

## 8. The agent channel

`channel.py`. Several harnesses run in one project at once - that is how people
use this - and every one of them is its own process, so nothing in a
conversation can tell you the others exist. Two of them read the same file, both
write, and the second write throws the first one's work away with nothing on
either screen to say so.

One JSON file per workspace, in `~/.aetheris/channel/`, holds three things:

```
agents    who is running here: id (a1, a2, ...), pid, model, what they are on
messages  what they have said, and two read cursors per agent
claims    which file each of them is in the middle of changing
```

**The workspace is the git working tree**, falling back to the working
directory. A terminal opened in `src/` and one opened at the top are working on
the same thing and have to see each other, and `git rev-parse` is the only thing
on the machine that already knows where a project ends.

**The board is not in the project.** It is a note about who is running right
now, not something to commit, and a directory appearing inside somebody's
repository the first time they open two terminals would be a rude surprise.
Invariant 5.7a is not broken by this: what lives with a project is its
*configuration*, and this is not that.

**Two cursors per agent, because a message has two audiences.** `read` is the
model's: `app.py` calls `turn_note()` before each turn and appends what came
back as a user message, ahead of the line the person typed, so the request stays
last. `shown` is the person's: `_watch_channel` polls while the prompt is
waiting and prints arrivals *above* it through `patch_stdout`. Without that
second one, a question asked of an idle terminal sits unread until somebody
presses Enter - which is exactly the terminal you most need to reach.

**The model's cursor is also read from inside the turn.** Once per pass of the
tool loop, `chat_turn` asks for `turn_note(mid_turn=True)` and appends it before
the next request. The top of the turn is not often enough: the turn is where the
time goes - twenty tool calls is minutes - and a message delivered only at the
next one waits for the person to type again. "I am holding parser.py" read after
the file was written over is the message the board exists to prevent.

Three details make that safe rather than merely eager. It is skipped on the
first pass, where `app.py` has just put the board's messages in *ahead* of the
line the person typed so the request stays last, and appending would undo
exactly that. It is skipped for a sub-agent (`SUBAGENT_DEPTH`), which is
somebody's errand and has no standing to answer the room. And the wording
differs: a note arriving mid-turn tells the model to answer and to respect a
claim it has just been told about, but to carry on with the job it is in the
middle of - it is news, not a new request.

`chat_turn` beats on the same pass, with no label. Nothing else says this agent
is alive while a long turn runs, and an agent presumed gone (`CHANNEL_STALE`)
has its claims dropped - the files it is in the middle of writing, while it
writes them. `heartbeat("")` is therefore a keepalive and not a rename: an empty
label leaves `_last_label` alone, or the loop and the prompt would clear each
other's and write the board on every beat.

A question asked mid-turn is chased by the same end-of-turn nudge as one asked
at the top of it - `turn_note` records the sender in `_asked_by` whenever it is
read, so the two arrive at the same place.

**And an idle prompt is exited, not waited on.** `app._maybe_auto_turn`. The
mid-turn delivery above covers a session that is *working*; a session sitting at
its prompt is the opposite problem and the worse one, because a message put into
its context is put into nothing until a person comes back and types. So the
watcher calls `prompt_app.exit(result=...)` and `prompt_async` returns a line
nobody typed. `_auto_woke` distinguishes it from a real one on the way out of
`_read_line`.

The result is deliberately *not* the message. The message is already going in
ahead of it, through `turn_note` and the model's own cursor; what the synthesised
line adds is the part a typed line carries and a delivered message does not -
that an answer is wanted now, and how far the turn may go ("do what they asked
only where it concerns files you are holding, then stop").

Four bounds, and they are the design:

| Bound | Why |
|:---|:---|
| Direct messages only (`channel.is_direct`) | A broadcast is news; six agents would all wake and answer the same one |
| Empty input buffer only | A half-typed line is that person's, and exiting the prompt would discard it unseen |
| `CHANNEL_AUTO_TURN_MAX` per human line | Two idle agents are a loop, and on a hosted model a loop costs money all night |
| `prompt_toolkit` only | `input()` cannot be interrupted; without it the message waits for the next Enter, as before |

`_auto_asked_by` holds a question across the one boundary where it would
otherwise be lost: arrivals are drained just *before* the prompt opens, when
there is no running application to exit out of, so a message that landed between
the last turn ending and this prompt opening is queued there and fired by the
watcher's first poll.

**Talking is measured against working.** `channel.note_tool`, called from
`dispatch_tool` past every refusal, sets `_worked` for any tool that is not
`list_agents` or `send_agent_message` and clears every talk streak. That one
signal answers two questions that turned out to be the same question.

The failure it exists for is not hypothetical. Two 4B models on one job spent
the whole run handing it back and forth - *could you test it?* / *yes, could you
test it?* - neither running anything, both recording that the other was on it.
Replying is one tool call and doing the job is twenty, so the loop is the cheap
path, and a model told to answer its messages has been told to take it.

- `handle_send_agent_message` refuses after `CHANNEL_MAX_IDLE_REPLIES` messages
  to one peer with nothing done between them. `[System]`, so §4's refusal
  counter ends a turn that keeps knocking. `0` disables it.
- `app._auto_turns` counts only auto-turns that came back with nothing done -
  `_read_line` reads `channel.worked()` before opening the next prompt and
  refills the budget if the turn did anything. That is what makes
  `CHANNEL_AUTO_TURN_MAX = 0` (no ceiling, as in `MAX_TOOL_CALLS`) a sane thing
  to set: unbounded *work* is fine, unbounded *chat* is what costs money.

`app` calls `channel.reset_work()` as each turn begins, so the reckoning is
per-turn. The note's wording carries the same instruction - *if they asked you
to do something, DO IT yourself; never say something is done that you have not
done* - but the wording is the hint and the refusal is the mechanism, in that
order, as everywhere else here.

**Drift, which is a different failure from the loop.** The loop was *neither
agent does the work*. Drift is *both of them do work, on the wrong thing* - a
suggestion, a counter-suggestion, and two agents solving a problem nobody asked
for. It has three causes and each gets a mechanism, because none of them is
fixable by wording alone.

| Cause | Mechanism |
|:---|:---|
| A peer's message arrives as `role: "user"` - the flat history (§3) has no role meaning "another agent" - and mid-turn it is the *newest* message. Same badge as the person's request, later in the list, so the peer becomes the user. | `channel._job` + the anchor `turn_note` ends every note on. `app` sets it from each human line and, crucially, **not** when `_auto_woke` - letting a peer's question install itself as the job is the drift, mechanised. |
| `MAX_TEXT` was 2000 characters, which is room to negotiate the problem instead of solving it. | 250. A physical limit, not an instruction. |
| Nothing anywhere said what the joint goal was, so drift was unmeasurable and therefore uncorrectable. | `channel.plan_note()` - see below. |

**`send_agent_message` takes a `kind`.** `channel.KINDS`: question, answer,
claim, release, done, warn. Coordination is always about something on disk, so
no kind means "let us rethink the parser", and that absence is the mechanism -
a model made to classify its own move has nowhere to put a design debate.
`channel.NEEDS_PATH` requires claim, release, question and warn to name a file.
That last rule carries the most weight, and `question` is tightened further by
`channel.mentions_held_file`: it must name a file that another agent currently
*holds*. Naming a file alone was the first version and it did not survive
contact - two gemma4:e4b instances both sent "who should run and test
server.js?", a delegation wearing a filename, and it passed. The claim system
supplies the sharper test: you never have to ask permission to touch a free
file, because writing takes the claim for you and a held file refuses your write
by name. So a question about an unclaimed file asks for something that could not
have been denied, and a question about no file is a `list_agents` query. Neither
is coordination. A fileless `question` gets a `[System]` refusal naming it as delegation,
pointing at `list_agents` for who-holds-what, and at `AUTO_VERIFY` for the
specific case that started all this - asking another agent to test your work
asks for something the harness already does after every edit. An unknown or
missing kind is `[Error]`, not `[System]`: the call was malformed and remaking
it correctly is exactly what should happen next. `/agents say` posts with no
kind, because a person is not what this guards against.

> The parameter order in `toolspec` is load-bearing. `_run_tool` calls
> `handler(*tool.bind(arguments))` - **positional** - so a spec listing its
> parameters in a different order from the function hands every argument to the
> one next door. `_check_registry` compares parameter *counts* and cannot see
> it; names cannot be compared either, since thirteen tools deliberately differ
> (`id` → `memory_id`). `tests/test_registry.py` pins this tool's order
> explicitly instead.

`channel.wakes(entry)` is the kinds' second job: question, claim, release and
warn mean somebody is blocked or about to be, so they may start a turn on an
idle terminal; `done` is news, and waking an unattended terminal to read news is
how it ends up holding a conversation. A missing kind wakes - that is a person
on `/agents say`.

`answer` wakes **only the agent that asked** - `channel._asked_of`, the mirror
of `_asked_by`, filled by `note_message` when a `question` goes to one named
agent and emptied in `turn_note` when the reply reaches the model. This was
found by running it: with every answer classed as news, a2 asked a1 about
`server.js`, a1 answered, and a2 slept through the reply and left. The reply to
your own question is not information; it is what you stopped for.

**The shared plan is a note, not a board field.** `channel.plan_note()`.
`notes.project_dir()` is keyed by `channel.workspace()` (`notes.py:64`) - the
very function the board is filed under - so every agent that can see another on
the board is already reading the same notes, and their titles are already in
every system prompt. A `goal` field on the board would be a second answer to
"what are we all doing", waiting to disagree with the first. `notes` imports
`channel` for `workspace()`, so `plan_note` imports `notes` *inside the
function*; the dependency only goes one way at import time. `/agents plan [text]`
reads and writes it.

**Clearing.** `channel.clear_messages(kind)` - `dm`, `everyone`, or everything
including the board's own arrival notices. Reached only from `/agents clear`,
never from a tool, for the same reason as `force_release`: the board is shared,
so clearing it clears it for every agent in the project, and the person at the
keyboard is the only one who can see every terminal. Clearing direct messages
drops `_asked_by` with them - a question nobody can read any more is not still
outstanding.

**The agent's own id is in the system prompt.** `channel.prompt_section()`,
composed in `app._compose_system_prompt` alongside memory and notes - not in
`systemprompt()`, because `config` builds that at import time and this session
has not joined a board yet. `main` calls `_refresh_system_prompt` once, straight
after `channel.join`, while nothing has been sent. Everything else about the
board is a tool call away; an id is the one fact with no question that returns
it, and without it `a1, are you done with parser.py?` is addressed to a name the
model has never been given.

**What reaches a screen is not what another process wrote.** Two halves. A
message has its control characters stripped on the way *onto* the board
(`channel._clean`, keeping tab and newline), because it is printed straight onto
another terminal above a prompt somebody is typing at, and an escape sequence
there would move their cursor from another process entirely. And the prompt is
patched with `patch_stdout(raw=True)`: the default routes printed text through
`Output.write()`, documented as "removes vt100 escape codes" and implemented as
`data.replace("\x1b", "?")`, which is what turned an arrival into a literal
`?[38;2;211;134;155m✉ a2 joined this workspace` on screen. Raw is safe here
precisely because of the first half.

Nothing is pushed on a quiet turn. A roster repeated every turn would cost
context in every solo session; what is pushed is what changed - an arrival, a
departure, something said - and `list_agents` is there for the model that wants
to look.

**Claims are the part that actually works.** See 5.11. `dispatch_tool` reads
`_WRITES_FILES` - the same table auto-commit reads - so a file tool is covered
here the moment it is added there. `run_cmd` is not covered and cannot be: what
a shell command touches is not knowable from the call, and a lock that reads as
protection while protecting nothing is worse than none.

Four things keep a claim from becoming a deadlock:

- it dies with the process (`leave`, and a pid check for the ones that are
  killed outright);
- it expires - `CHANNEL_CLAIM_TTL` for an explicit one, the much shorter
  `CHANNEL_WRITE_TTL` for one taken automatically by writing;
- the refusal is `[System]`, so an agent that keeps retrying ends its turn after
  three attempts (§4) instead of spending the whole budget on a closed door;
- `/agents release <path>` overrules it. That is `force_release`, and no tool
  reaches it: the person is the only one here who can see both terminals.

**Concurrency.** Every change is a locked read-modify-write, and the write goes
through `atomic.py`. The lock is an `O_EXCL` file, broken when its holder has
clearly died, and *given up on* after `_LOCK_WAIT` - a lost message is a bad
afternoon; a harness that stops responding at the prompt because another one
died holding a lock is a worse one. `tests/test_channel.py` runs six real
processes at the board to check that giving up that easily still loses nothing.

---

## 8a. The Python VM

`vm.py`, behind the `run_python` tool. One `python` process is kept alive beside
the harness with its globals intact, so the model can work something out in
steps instead of guessing at it - which is the whole point for a 4B model, and
the reason this is not simply `run_cmd python3 -c "..."`. `README.md` §9 has the
four differences that matter; this is how it is wired.

```
parent (vm.Kernel)                   kernel (vm._DRIVER, written to disk and run)
------------------                   -------------------------------------------
stdin pipe   -- one JSON line -->    read from a dup of fd 0
                                     fd 0 itself is /dev/null
stdout pipe  <- one JSON line ---    written to a dup of fd 1
                                     fd 1 and fd 2 are the capture file
capture file <-------------------    everything the code prints
read from the offset we left at
```

**The dups are the design.** Redirecting the capture at the file-descriptor
level rather than swapping `sys.stdout` means output from a C extension or a
subprocess is caught too, and - the part that actually broke first - code that
reads stdin cannot eat the next request, and a `print` cannot be mistaken for a
reply. The protocol and the output travel on channels that cannot touch.

**A trailing expression is answered.** `2 ** 10` alone prints nothing under
`python -c`, and a model told it has a calculator stops believing it after one
try. `_run` splits the last `ast.Expr` off the module, `exec`s the rest and
`eval`s that, and the kernel's own frames are stripped off any traceback -
`_below_kernel` - so the model sees only lines it wrote.

**Dying is a reported outcome, not an error.** `run()` returns `timeout` or
`crashed` as separate keys from `error`, because those two mean the namespace is
gone. The handler turns each into a result that says so; a model told only "that
failed" goes on referring to variables that no longer exist.

**It is isolation from mistakes, not a sandbox**, and nothing in the code or the
prose pretends otherwise. The code runs as the user. That is why `run_python` is
in `tools._CHANGES_THINGS` alongside `run_cmd` - what a snippet touches is no
more knowable from the call than what a shell command touches - and why it still
asks. What the separate process buys is that a runaway loop or a 40GB allocation
takes down the scratch process and not the harness.

**`_limiter` captures its values in the parent.** A `preexec_fn` runs between
fork and exec, in a process holding the parent's threads' locks and none of its
threads; importing a module or reading `config` there can deadlock. RLIMIT_CPU
is deliberately absent: it counts CPU seconds over the whole life of a process
that is meant to live for the whole session, so a limit sized for one call would
kill a healthy kernel after twenty of them. A loop that will not end is a
wall-clock problem and `Kernel.run` handles it as one.

**And the rlimits are the optimisation, not the guarantee.** The wall clock is
what contains a runaway; RLIMIT_AS is what turns an over-large allocation into
a `MemoryError` the model can read instead of a killed process. Only Linux
gives it: Darwin accepts the same `setrlimit` call and does not enforce it, and
Windows has no `resource` module at all. This was written as "POSIX only",
which is what `resource` being importable means and not what the platform then
does with it - macOS CI is what told the difference, and `test_vm.py` now
asserts what each platform actually provides rather than what was asked for.

**The scratch directory is the person's, not the project's** (5.7a):
`~/.aetheris/vm`, and the process runs there, so a stray write lands in the
scratchpad instead of the repository and never in an auto-commit. The working
directory is on the child's `PYTHONPATH` so project code can be imported and
tried, with `PYTHONDONTWRITEBYTECODE` set so importing it leaves no
`__pycache__` behind in somebody's `git status`.

---

## 9. Recipes

### Add a tool

1. `toolspec.py` — a `Tool(...)` entry in `TOOLS`. Parameters in the order the
   handler takes them. Set `optional=True` on anything a call may omit; set
   `block=True` if the value arrives in a raw `<name>` block; add
   `native_description` if the text and native protocols need different words.
2. `tools.py` — write `handle_<name>(...)` and add one line to `_handlers()`.
3. Nothing else. The prompt, the native schemas and dispatch all follow.
   If they do not agree, `_check_registry` raises on the first tool call.
4. If it changes files, add it to `_WRITES_FILES` and `_CHANGES_THINGS`.
   `_WRITES_FILES` is read three times over - auto-commit, the claim check that
   refuses another agent's file, and the claim taken after a write - so one
   line there is all three.

### Add a provider

1. Subclass `providers.Provider`: `name`, `label`, `key_env`,
   `default_base_url`, `list_models()`, `stream()`.
2. `stream()` must yield the §3 event shape and nothing else.
3. Set `supports_native_tools` and implement `encode_tools()` if it has a
   function-calling interface; leave both alone if it does not.
4. Register it in `providers.PROVIDERS`.
5. `merge_runs()` if it rejects consecutive same-role messages, `split_system()`
   if it wants the system prompt as its own field. Both already exist.

### Add a slash command

`app.py`, in the command block before `if config.PLANMODE:`. Then add it to the
`SlashCommandCompleter` list in `app.py` and to the help table in `tui.py` -
a command in none of the three lists is a command nobody finds.

### Change what the model is told

`systemprompt.py`. Text used by both protocols goes in the shared body of
`tool_rules()`; text that differs goes in the `_TEXT_*` / `_NATIVE_*` pair. Never
write a second copy - `subagent.py` reads the same function.

---

## 10. Tests

No framework. Each file is a script that prints `[ok]` / `[FAIL]` lines and
exits non-zero on failure. Run them all:

```bash
for t in tests/*.py; do python "$t" || echo "FAILED: $t"; done
```

| File | Guards |
| :--- | :--- |
| `test_registry.py` | The tool table, the prompt and the handlers still describe the same tools (5.1) |
| `test_native_tools.py` | Each vendor's tool-call wire format; both protocols reach the same place (5.3, 5.5) |
| `test_deepthink.py` | Stage sequencing, both early exits, read-only really read-only (§7) |
| `test_git_ops.py` | Commit and undo against real repositories, including undo refusing (5.8) |
| `test_durability.py` | Atomic writes, including killing a writer mid-write; the token estimate (5.7) |
| `test_subagent.py` | What a sub-agent may do, and that only its report crosses back |
| `test_platform.py` | Waiting-for-input detection on *this* machine. Run it on any new one, especially Windows |
| `test_permissions.py` | What an allow rule covers, and the two ways one used to cover more than it said |
| `test_paths.py` | That state resolves under `~/.aetheris` and never into the working directory |
| `test_terms.py` | That the terms are shown before anything runs, asked once, and never assumed from a pipe |
| `test_tool_parsing.py` | The text protocol's repair engine: the shapes it reads, and the ones it refuses |
| `test_resume.py` | That `--resume` and `-c` resolve on the command line, and refuse rather than guess |
| `test_tool_reporting.py` | That the result markers are read as anchors (5.9), and that nothing warns onto stderr mid-tool |
| `test_mentions.py` | What `@` attaches, what it refuses to, that the menu reads the real directory, and that the command menu previews what each command does and what may follow it |
| `test_channel.py` | That another harness's file cannot be written from here, that a claim dies with its terminal, that a message sent mid-turn is read inside that turn rather than after it, and that concurrent writes to the board lose nothing (5.11, §8) |
| `test_hashline_edit.py` | That an anchor reaches the line it names, and that a stale one is refused rather than applied a few lines off (5.12) |
| `test_vm.py` | That `run_python` takes its code as a raw block, remembers between calls, and says the namespace is gone every way it can die (§8a) |
| `test_usage.py` | That a turn's requests are counted as one turn, that a resumed session carries on past its own, and that a session recorded before turns existed still renders |
| `test_settings.py` | That `/set` records only what changed, that a broken settings file still starts, that a saved API key can be deleted, and that the banner fits the terminal |
| `test_caching.py` | That the cache breakpoints reach Anthropic, that all three hosted providers' cache counters are read back, and that the prefix this harness builds is byte-stable enough to cache |
| `test_verify.py` | That auto-verify picks the right check, runs it once per turn, refuses one it cannot run, and turns off a suite that will not finish (§7a) |
| `test_mcp_lazy.py` | That a big MCP server is announced rather than described, that asking for it hands over the parameters, that a call to an unloaded one still works, and that names and schemas cannot come apart (5.13) |
| `test_tdd.py` | That `/tdd` reaches a test file however its path is written, refuses in `dispatch_tool`, holds nothing on disk, and lifts itself (§7a) |
| `test_malformed_state.py` | State that is not the shape the code assumed: a message with no content, a hand-edited `memory.json`, a setting of nought - and that writing a file through a tool is atomic and leaves its line endings alone (5.7) |
| `test_memory.py` | That a memory marked `important` reaches the prompt a session opens on, that the mark survives being saved over, and that the block is capped and byte-stable (5.15) |
| `test_images.py` | That an image is recognised by extension and by its bytes, that a large one is resized and relabelled as what it became, that all four wire shapes are right with the cache breakpoint still on the text, and that a model which cannot see is found out before the request (5.17) |
| `test_notes.py` | That a note is one markdown file whose name is its id, that projects do not share notes while a subdirectory shares one, that a model-chosen id cannot write outside the notes directory, and that the prompt gets sorted titles only (5.16) |
| `test_vault.py` | That a `.env` value never reaches the model by any route, that a placeholder reaches the shell as the real key, and that a file is neither how it gets out nor how it is lost (§8b) |
| `test_docs.py` | That this file and `README.md` still describe the program that exists |
| `test_compat.py` | That the commands, settings, tool names and files people build habits on are still there under the same names (5.14) |

`test_docs.py` is why the two documents can be trusted: it fails if either names
a file, function or setting that is gone, if `README.md` misses a tool or a
slash command, or if a number stated here stops matching the code. Prose cannot
be generated from the source the way the tool schemas are, but it can be held to
it.

`test_platform.py` runs the whole suite twice on Linux, the second time with
`/proc` switched off - which is exactly the code Windows and macOS take.

---

## 11. Things that surprise people

- **`config.py` imports `systemprompt`**, not the other way round. See 5.2.
- **There is no `tool` message role.** Tool results are user messages.
- **`toolspec.py` imports nothing local** and must stay that way.
- **`git_ops._git` never raises.** No git installed is a "no", not a crash.
- **Ollama's tool support is per model.** `providers.OllamaProvider.supports_native_tools`
  is a property; reading it off the class gives you the property object, not a bool.
- **`stream_reply` returns text only.** Native tool calls come back through the
  `calls_out` list you pass in.
- **The token estimate calibrates itself** against `prompt_tokens` from each
  provider. There is no bundled tokenizer, deliberately: `cl100k_base` is not
  the tokenizer of any model this runs against.
- **Sub-agents and deepthink both run through `dispatch_tool`**, so permission
  rules apply to them exactly as to the assistant. Neither is a way around a
  `deny`.
- **What `/set` may change is derived, not listed.** Any `UPPER_CASE` name in
  `config.py` holding a number, switch, string or list is a setting; the
  exceptions are named in `config._NOT_A_SETTING`, because they are far fewer
  and far more stable than an inclusion list would be. `settings.json` records
  only the values that differ from this file's, so a default improved in a
  later release still reaches anybody who never overrode it.
- **`_get_conv_pairs` does not count turns**, and `/usage` no longer calls its
  answer one. It opens a new block at every user message that is not a tool
  result, and the harness writes several of those itself - each of deepthink's
  six stage instructions, the nudge after an empty reply, the one after an
  unparseable call, a channel note, a `!` command. One question answered by
  deepthink is one turn and seven blocks. It is the right unit for what it is
  for, which is what compression keeps or drops.
- **Only Anthropic needs code to cache a prompt.** OpenAI and Gemini cache
  automatically above a per-model minimum; Anthropic caches nothing without a
  `cache_control` breakpoint. Its render order is `tools` -> `system` ->
  `messages`, so the one marker on the system block covers the tool schemas
  too - `providers._cached_system`. What all three needed was the *reporting*:
  a prefix match fails silently, as a bill rather than an error.
- **The tool list costs the same wherever it travels.** Over the text protocol
  it is in the system prompt; over a native interface it is in the request's
  own `tools` field. Against gemma4:e4b the two prompts come to 6,013 and 5,958
  tokens - a difference of 55. `tui._fixed_overhead` adds the schemas back in
  for the native case, because counting `messages[0]` alone would report 1,866
  and hide two thirds of what each request actually pays.
- **`config.token_history` has one entry per request to the model, not per
  turn.** A question answered with four tool calls leaves five entries. They
  carry the turn they belong to, and `context.token_turns` is the only thing
  that groups them - `/usage` and the end-of-turn token line both read it, so
  they cannot disagree about what one question cost.
- **`run_python` keeps a process alive between calls**, and it is the same
  process for the whole session. A `reset` is the model's way to empty it; a
  timeout or a crash empties it whether anyone wanted that or not.
- **A `reset` argument is read with `tools._truthy`, not `bool`.** A model that
  sends `"reset": "false"` means the opposite of what a non-empty string is
  worth, and getting that backwards throws away everything it was working with.
