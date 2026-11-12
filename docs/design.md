# Design

[← README](../README.md)

## Architecture

The codebase is organized cleanly around the following components:

- **`ARCHITECTURE.md`**: How the codebase is put together - the turn's control flow, the data shapes, the invariants, and what to touch for a given change. Read that before editing; read this to use it.
- **`app.py`**: Event loop, slash command router, and system prompt composition.
- **`llm_client.py`**: The conversation loop - streaming a reply, parsing the tool calls out of it, running them. Knows nothing about which provider answered.
- **`tools.py`**: Tool implementations, and the table binding each one to its entry in `toolspec.py`.
- **`toolspec.py`**: What every built-in tool is - name, description, parameters. The system prompt is rendered from it and dispatch binds arguments through it, so the two cannot drift apart.
- **`qr.py`**: A QR encoder, stdlib only - byte mode, level M, versions 1 to 9 - and the half-block drawing `/remote qr` prints.
- **`remote.py`**: The one door into a running session - the token-locked HTTP server, the transcript mirrored off `sys.stdout`, and the question that follows whoever is driving the turn.
- **`channel.py`**: The board the harnesses running in one project share - who is here, what they have said, and which files each is in the middle of changing.
- **`subagent.py`**: `spawn_agent` - a second model, hired for one self-contained job, working in its own context and handing back only its report.
- **`skills.py`**: Skill discovery, frontmatter parsing, and on-demand loading.
- **`providers.py`**: The provider abstraction - Ollama, Anthropic, OpenAI, Gemini, Attacca - and the saved connection.
- **`zyris.py`**: The Zyris wire - one websocket, msgpack envelopes, calls in both directions, streams with credit, and the reconnecting reader thread that keeps it up while a prompt waits on a person.
- **`attacca.py`**: Attacca as the provider - pairing by code, this machine's tool table announced as a capability, and the turn that streams an agent's reply and runs the tools it calls back for.
- **`connect.py`**: The `/connect` flow.
- **`sse.py`**: Reading server-sent events without waiting for data that has not been sent. Shared by the providers and the MCP client.
- **`permissions.py`**: Permission rule loading, matching, and the allow/deny/ask decision.
- **`shell_session.py`**: Live commands - output draining, waiting-for-input detection, and the session registry.
- **`deepthink.py`**: The six-stage chain - the stage instructions, what each stage may do, when the chain stops early, and when it starts over.
- **`git_ops.py`**: A commit per AI edit, and the undo that makes it worth having.
- **`atomic.py`**: Writing a file so a crash cannot leave half of it behind. Used for sessions, memory, permission rules and the saved API keys.
- **`terms.py`**: What the harness does to the machine it runs on, shown once before it does it.
- **`vault.py`**: The `.env` values the model is never told, and the placeholder the harness expands on its way to a tool - and the harness's own keys, hidden from every tool result and never filled back in.
- **`keystore.py`**: Saved API keys in the OS keyring when `aetheris[keyring]` is installed and there is one to use; `providers.json` otherwise.
- **`tests/test_platform.py`**: Checks the waiting-for-input detection on the machine it is run on. Worth running on any new machine, and especially on Windows - see below.
- **`tests/test_registry.py`**: Fails if the tool table, the system prompt and the handlers stop describing the same tools.
- **`tests/test_durability.py`**: Atomic writes (including killing a writer mid-write) and the token estimate.
- **`tests/test_deepthink.py`**: Stage sequencing, both early stops, the repeat pass and its ceiling, and that the planning stages really cannot edit.
- **`tests/test_git_ops.py`**: Auto-commit and undo against real repositories - including that undo refuses when it would destroy something.
- **`tests/test_native_tools.py`**: Each provider's tool-call wire format, and that both protocols end up in the same place.
- **`tests/test_docs.py`**: Fails when README.md, `docs/` or ARCHITECTURE.md names something that is gone, or misses something that is new - and when README.md grows past a page.
- **`tests/test_compat.py`**: The public surface written down - fails when a slash command, a `/set` setting, a tool name or a file on disk is renamed or dropped.
- **`tests/test_subagent.py`**: What a sub-agent may do, what it may not, and that only its report crosses back.
- **`tests/test_permissions.py`**: What an allow rule covers - and that it stops at the command it names, rather than at whatever the shell was told to run next.
- **`tests/test_paths.py`**: That nothing personal is written into whatever directory you started in, and that state from an older version is named rather than moved.
- **`tests/test_terms.py`**: That the terms are shown before the harness can act, asked once, and never assumed from a pipe.
- **`tests/test_tool_parsing.py`**: Every shape a model wraps a tool call in, and every shape that must not be read as one.
- **`tests/test_retry.py`**: That a rate limit, an overloaded server or a dropped connection is tried again - after a wait that doubles, or as long as the server asks - and that a reply which has started arriving is never started over.
- **`tests/test_sse.py`**: That a hosted provider's event stream is read whether or not the server gzipped it - Anthropic does - one event at a time rather than at the end, and that a compressed reply is not read as an empty one.
- **`tests/test_headless.py`**: That `aetheris -p` puts the answer alone - or one JSON object naming every tool call and how it ended - on stdout, reads a piped prompt, never waits on a stdin nobody sent anything on, and reports a failure as an exit status.
- **`tests/test_bench.py`**: That each benchmark grader passes a right answer and fails the wrong ones a small model actually gives - a file rewritten down to the one function asked about, a call made when none was wanted - and that the edit fixture still has its bug.
- **`tests/test_search.py`**: That `search_in_file` leaves out what `.gitignore` excludes and binaries, narrows by glob, shows context once per stretch - and that ripgrep and the Python fallback give the same answer; and that `find_files` finds by name at any depth, by path with a `/`, and stops at 200.
- **`tests/test_loops.py`**: That the third identical tool call carries a warning and the fourth is not run, that a file written in between resets the count, that polling a running command is left alone, and that a model which keeps going is ended by the same escalation refusals get.
- **`tests/test_todos.py`**: That `update_todos` reads a list in any shape a model sends it, that the open items reach the model before every request and never the stored conversation, and that the list is saved and resumed with its session.
- **`tests/test_rewind.py`**: That `/rewind` puts the history back exactly - channel notes kept, a compaction undone - returns the prompt to the box and the checklist to where it was, leaves the files and names the AI commits in them, and that a new or loaded conversation starts with nothing to rewind.
- **`tests/test_btw.py`**: That `/btw` leaves the conversation untouched, sends it unchanged with the question after it and the tools a turn would offer, and names a tool the model reaches for without running it.
- **`tests/test_copy.py`**: That `/copy` finds the answers newest first without calls or thinking, offers a code block on its own, sends to the clipboard tool and the terminal both, and writes a file only where none exists.
- **`tests/test_window_effort.py`**: That the context window comes from `/context`, then the hosted model's own report, then `NUM_CTX` - asked once, kept per model, never a hosted figure for Ollama - and that `/effort` reaches each provider in its own field, on a turn's request only and never to an Ollama model that cannot think.
- **`tests/test_read_range.py`**: That `read_file` with `start_line`/`end_line` returns those lines under their real numbers and anchors, an edit from them lands, a cut for length falls between lines and says where to read on, and the prompt lets independent calls share a reply.
- **`tests/test_encoding.py`**: That a file that is not UTF-8 - CP949, as older Windows editors save Korean - is read under the anchors its text has, edited and written back in CP949 with its line endings, that a character CP949 cannot hold is refused with nothing written, and that a file in neither encoding is still refused.
- **`tests/test_transcript.py`**: That every turn is recorded whole - thinking, every argument, every result - that Ctrl+O draws all of it expanded, answers as markdown, and pressed again - or when a message is sent - puts back exactly the rows that were on screen before, that a redraw records nothing and `/clear` or a loaded session starts the record again, that hidden thinking leaves a line saying how long it took, that the record is saved in the session file and brought back - thinking and whole results - when the session is loaded, and that the key reaches the main loop with the half-typed line kept.
- **`tests/test_update.py`**: That versions compare as numbers, the start-up check runs at most daily and says so once, and `/update` asks before running anything, upgrades the way this copy was installed, and never pip-installs over a git checkout.
- **`tests/test_dist.py`**: That `.github/check_dist.py` refuses a wheel missing a module or carrying the tests, an sdist without the files the suite reads, and classifiers that disagree with the CI matrix - and that `pyproject.toml` agrees with `ci.yml` right now.
- **`tests/test_resume.py`**: That `--resume` and `-c` open the conversation they name - and that neither hands back a blank one, or guesses, when they cannot.
- **`tests/test_tool_reporting.py`**: That a tool result is judged by the marker it *starts* with, not one it happens to contain, and that no library writes an unasked-for paragraph to stderr while a tool is running.
- **`tests/test_hashline_edit.py`**: That `38:ff7|print()` reaches the line it names, that a stale or mistyped anchor is refused rather than applied a few lines off, and that everything which is not an anchor still behaves as it did.
- **`tests/test_multi_edit.py`**: That `multi_edit`'s SEARCH/REPLACE blocks are read, that two edits changing one file's length both land at the lines `read_file` named, that edits across files land together after one approval, that one edit which cannot be made leaves every file untouched, and that permission rules, `/tdd` locks, agent claims and the commit see every file in the batch.
- **`tests/test_qr.py`**: That a symbol is one a scanner can read - it reads each one back the way a scanner does, from the mask in its own format bits through the zigzag and the blocks, and checks that every block still satisfies its Reed-Solomon parity; plus what fits in which version, and that the drawing is the symbol.
- **`tests/test_zyris.py`**: The Zyris wire against a server written from the spec, not from this client - a msgpack handshake, answers matched by id, a stream that skips a chunk failed rather than delivered, a dropped connection failing what was in flight and coming back with its tools announced again, and a refused or revoked credential that stops it for good.
- **`tests/test_attacca.py`**: Attacca's seam against a fake that plays scripted turns - pairing at the server's pace, a turn drawn by the ordinary renderer, a tool call refused by a deny rule exactly as a local one would be, a turn that waits out one already running, Ctrl+C reporting how much was seen, and a call from the web app that closes the prompt and gives the half-typed line back. Also: a plan handed back and approved with Enter or answered with what to change; a message typed in the web app drawn here live, read back from the cursor after being away, and never sent, answered or stopped from this end; an answer said in parts kept whole; and the session filed under Attacca's own title.
- **`tests/test_resize.py`**: That a resized terminal is drawn again for its new size - once the size has settled rather than at every step of a drag, with the screen and its scrollback cleared, the conversation replayed, and whatever was half-typed given back to the prompt; and that Ctrl+C at the prompt reaches the main loop instead of escaping the event loop.
- **`tests/test_interrupt.py`**: That Ctrl+C stops what is running rather than the program - one press is counted once however it arrives, a second within a second leaves, how to leave is said beside the prompt rather than printed above it, one pressed half-way through a slash command is put off instead of ending everything, and the thread behind a reply nobody is reading stops reading it.
- **`tests/test_select.py`**: That a drag selects by block - the ledger of what was printed wraps where the terminal does, a selection stays inside the thought or answer it began in and inside the text of each row, the copy is the line as it was written, a terminal's and a Windows console's mouse reports are read alike, a drag held while the wheel turns - or at the top edge - selects on into what had scrolled away and leaves the screen as it found it, the wheel on its own scrolls those rows without giving up the mouse, so a drag works after it, an empty line copies as an empty line, and a pasted block is shown as its size and sent whole.
- **`tests/test_wrap.py`**: That a line too long for the window keeps its rail on every row - a thought's `│`, a tool result's, a quotation's `┃` - cut by terminal cells, so Hangul counts double and colour codes count nothing.
- **`tests/test_remote.py`**: That the remote refuses a request with no token, a token that is nearly right and a `Host` this machine was never called by, that a `.env` value on this terminal does not go out over it, that a question cannot be answered by a phone still showing the last one, and that closing it frees the port and puts `sys.stdout` back.
- **`tests/test_channel.py`**: That a file one harness is changing cannot be written from another, that the refusal names who to ask, that a claim dies with the terminal that took it, and that several processes writing to the board at once lose nothing.
- **`tests/test_mentions.py`**: What `@` attaches and what it must leave alone - an email address is not a file - that the completion menu reads the real directory, and that the command menu previews what each command does and what may follow it.
- **`tests/test_images.py`**: That an image is detected by extension and by its first bytes, that one too large is resized rather than refused and relabelled as whatever it became, that each of the four providers is handed the shape it asks for with the cache breakpoint still on the text, that `@shot.png` attaches a picture instead of a wall of bytes, and that a model which cannot see is found out before the request rather than after.
- **`tests/test_notes.py`**: That a note is one markdown file whose name is its id and whose bytes are its content, that two projects do not share notes while one project is the same project from any directory inside it, that a note id the model chose cannot write outside the notes directory, and that the prompt gets the titles only - capped, sorted, and identical between builds.
- **`tests/test_memory.py`**: That a memory marked `important` is in the system prompt a session opens on - a new one and a resumed one - that the mark survives a later rewrite of the memory's text, that the block is capped in both directions and says when it cut something, and that a hand-edited `memory.json` cannot break the prompt.
- **`tests/test_keys.py`**: That a key the harness holds never reaches the model - hidden in what tools print and never filled back in, `providers.json` refused to every tool even through a symlink, and absent from the environment the model's commands run in - that a keyring takes the key out of the file without losing one it refuses, and that the home directory is closed to other accounts.
- **`tests/test_vault.py`**: That a `.env` value never reaches the model - not through `read_file`, not through a command that prints it, not through an `@` attachment - that the placeholder reaches the shell as the real key, and that a file is neither how a secret gets out nor how it gets lost.
- **`tests/test_malformed_state.py`**: What happens when the state is not the shape the code assumed - a message whose content is `null`, a `memory.json` somebody edited by hand, a setting typed as nought - and that `write_file` and `edit_file` replace a file in one step rather than truncating it first, without rewriting its line endings on the way past.
- **`requirements-lock.txt`**: The exact dependency set the harness was tested against. `requirements.txt` gives the tested floors and a ceiling before the next breaking release.
- **`mcp_client.py`**: MCP transports (stdio / streamable HTTP / SSE), the JSON-RPC session, tool and resource calls, and the prompt section they are advertised in.
- **`websearch.py`**: Multi-source retrieval, page extraction, and BM25 reranking.
- **`context.py`**: Token budgeting, tool-result trimming, and context compression. The token estimate is script-aware and calibrates itself against the counts each provider reports.
- **`renderer.py`** / **`tui.py`**: Markdown rendering and the terminal chrome.
- **`screen.py`**: The program's own account of what is on screen, row by row - what a drag is selected from - and the clipboard.
- **`promptkeys.py`**: What the prompt does with a paste and with the mouse.
- **`session.py`**: Session save/load/list, the persistent memory store, and the block of `important` memories that goes into every session's system prompt.
- **`systemprompt.py`**: The system prompt - the assistant's own instructions, plus the tool-protocol rules that `subagent.py` shares. The tool schemas themselves come from `toolspec.py`.
- **`skills/`**: Project-level skills. Personal skills live in `~/.aetheris/skills/`.
- **`.permissions.json`**: Project-level tool permission rules (see `.permissions.json.example`). Personal ones live in `~/.aetheris/permissions.json`.
- **`.mcp.json`**: Project-level MCP server declarations (see `.mcp.json.example`). Personal ones live in `~/.aetheris/mcp.json`.
- **`images.py`**: Recognising an image, resizing one that is too big, and the base64 a provider sends. Images ride on a message as paths, so a saved session never carries a screenshot around.
- **`notes.py`**: Markdown notes about one project - where a project's notes live, the five tools over them, and the block of titles that goes into the system prompt.
- **`memory.json`**: Key-value JSON storage backing the long-term memory system. Each record carries its content, when it was written, and whether it was marked `important`.
- **`sessions/`**: Session directory containing JSON transcript backups for conversation history. Each file is named after the session's title (slugified, e.g. `웹-검색-랭킹-개선.json`); untitled sessions fall back to a timestamp until a title exists. Each also records the working directory it was last saved from, which is what `-c` matches against.
- **`.chat_history`**: History file managed by `prompt_toolkit` for command history recall across terminal runs.

---

## How This Differs From Other Harnesses

Most terminal AI harnesses - Claude Code, Cursor, Aider, Continue, OpenHands -
are built against one or two hosted, native-tool-calling models and treat
anything smaller as an afterthought, if they support it at all. Aetheris
was built the other way: for a model too small to be trusted, with the hosted
providers added on top of the same code path rather than the other way round.
That ordering is where most of the differences below come from.

| Feature | Aetheris | Claude Code | Cursor | Aider |
| :--- | :--- | :--- | :--- | :--- |
| Works with a model that has no native tool calling | ✅ text protocol, chosen per model | ❌ native tools only | ❌ | ✅ text edit formats, no tool calls at all |
| File bodies sent as raw text, never JSON-escaped | ✅ `<content>` blocks | ❌ JSON tool arguments | ❌ | ✅ fenced edit blocks |
| Edits addressed by line + fingerprint instead of reproduced old text | ✅ hashline | ❌ old/new string | ❌ | ❌ search/replace or diff |
| Several instances in one project see each other | ✅ shared board, automatic file claims | ⚠️ agent teams (experimental), no file claims | ❌ | ❌ |
| Messaging between running sessions | ✅ | ✅ cross-session messaging | ❌ | ❌ |
| Planning with editing tools disabled by the harness | ✅ deepthink plan/check/review stages | ✅ plan mode is read-only | ⚠️ ask mode | ✅ `/ask`; `/architect` splits plan from edit |
| Review stage fed the real `git diff` | ✅ | ❌ | ❌ | ❌ |
| Undo one AI change | ✅ a commit per tool call, `/undo` refuses if the file changed since | ✅ `/rewind` to a checkpoint per prompt (shell-made changes not tracked) | ⚠️ checkpoint restore | ✅ a commit per change, `/undo` |
| Project tests run after an edit with no setup or flag | ✅ detected from the project | ❌ a hook you write | ❌ | ⚠️ lint on by default; tests need `--test-cmd` + `--auto-test` |
| A failing check fed back to the model, capped | ✅ 3 tries, then it must explain | ❌ unless a hook does it | ❌ | ✅ fixes in a loop, capped at 3 reflections |
| Atomic writes for all saved state (sessions, memory, permissions, keys) | ✅ | ⚠️ not documented | ⚠️ not documented | ⚠️ not documented |
| One table generates the prompt, schemas and dispatch (tested) | ✅ `toolspec.py` + CI test | ⚠️ closed source | ⚠️ closed source | ⚠️ not documented |
| Tuned and benchmarked for small (4B-12B) local models | ✅ primary use case | ❌ Claude models | ❌ hosted models | ⚠️ connects to Ollama with per-model settings, benchmarks larger models |

*Checked against each project's published documentation as of November 2026.
"❌" means not found there, not proven absent - Claude Code and Cursor are
closed source and all three ship often, so any row can go out of date. Please
open an issue if one has.*

**A 4B local model is the case it is built for, not one it tolerates.** Claude
Code and Cursor hand every model a JSON tool-call contract, and a 4B model
fails that contract constantly - a bare quote inside `print("hi")`, an
uncounted brace, and the whole generation is thrown away. Aider avoids JSON
entirely with text edit formats, which is the right instinct; Aetheris takes
the same view for models that need it and keeps native calls for those that
do better with them. It asks Ollama per model whether a native `tools`
interface exists and, when it does not, switches to a text protocol where a
file body is a raw block (`<content>...</content>`) rather than a JSON
string - the one thing small models get wrong most often stops being asked of
them at all. Everything else on top (anchor repair, three-character hashes,
auto-verify) was tuned against 4B models rather than tested on them afterwards.

**Hashline editing removes the failure mode every other diff/patch format
has.** Claude Code, Aider and Cursor all ask the model to reproduce the exact
old text it wants to change, then match that text back into the file - and a
line that appears twice, or one reproduced with a stray space, makes the edit
ambiguous or wrong. `read_file` here returns `50:1fa|<content>`, a line number
plus a fingerprint of that exact line, and an edit is just that row handed
back with different text after the `|`. There is no old-text block to get
subtly wrong, a stale anchor is refused rather than landing a few lines off,
and a duplicated line is no longer ambiguous because its number disambiguates
it. This is a correctness property, not a convenience one - it is what makes
hashline edits reliable coming from a model too small to retype a line
perfectly.

**Multiple instances in one project know about each other without being set
up as a team.** Run Cursor and Aider in two terminals against the same working
tree and neither knows the other exists - two agents editing the same file is
a silent last-write-wins. Claude Code can message other sessions and run an
experimental agent team, but a file one session is editing is not held
against the others. Aetheris keeps a shared board per
workspace (`channel.py`): every instance sees who else is running and what
they are doing, can message them, and a file one instance is mid-edit on is
refused to the others *by name*, with the holder named back. The claim is
taken automatically on write, so protection does not depend on any model
having thought to ask for one - and it expires with the process that took it,
so a crashed terminal cannot lock a file for the afternoon.

**Deepthink runs plan, check, build and review as one enforced chain.**
Claude Code's plan mode and Aider's `/ask` already keep a planning model from
editing, and that is the right design - an instruction alone is not enough, a
local 4B model tried to edit fifteen times in planning before it was enforced
here. What deepthink adds is the chain around it, driven by the harness rather
than by the person switching modes: the editing tools are off at the
dispatcher during the plan, check and review stages and on for build. It also
gives the
review stage the real `git diff` of what changed rather than asking the model
to recall its own edit, and finding problems (review) is a separate,
read-only stage from fixing them (revise) - so review's list is not cut short
by the model stopping to patch the first thing it finds.

**Undo is per tool call and refuses to take your own edits with it.** Aider
got this right first - every change is a commit and `/undo` reverts the last
one - and Aetheris does the same, one commit per tool call, named for the tool
that made it. Claude Code's `/rewind` and Cursor's checkpoints restore a point
in the conversation instead, which is coarser: everything since that prompt.
`/undo` here also refuses outright if a file in that commit has since been
touched by anything else, rather than taking that other change down with it.

**The project's own tests run themselves, with nothing to configure.** Aider
lints after every edit by default and has `--auto-test`, but you supply the
test command and turn the flag on; in Claude Code and Cursor, checking the
work is a hook you write or a thing you remember to do. Here the check is
*detected* - a `pyproject.toml` or a `test_*.py` means pytest, a `Cargo.toml`
means cargo, a `package.json` means npm if its `test` script is a real one -
and it runs on its own after any turn that changed a file, once for the whole
turn rather than once per edit. A failure becomes the next thing the model
reads, which is the form small models handle best: fixing a traceback is
pattern-matching, while noticing unprompted that something might be wrong is
not. And it is capped - three failures in a row (Aider stops at three too) and
the harness stops feeding them back and tells the model to explain what is
broken instead, because a fourth guess is worth less to you than an honest
description. It is safe to leave on precisely because `/undo` is per-edit:
each retry is its own commit.

**State survives being killed mid-write, everywhere, not just in the editor
buffer.** Sessions, memory, permission rules and saved API keys are all
written to a temp file and renamed into place (`atomic.py`). A crash never
leaves a half-written `memory.json` or a corrupt session transcript - a
guarantee most terminal harnesses only apply, if at all, to the file the model
is actively editing.

**The tool table cannot drift from the prompt or the dispatcher, because
there is only one table.** `toolspec.py` is the single source both the system
prompt and the dispatch logic are generated from, and `test_registry.py` and
`test_docs.py` fail the build if a tool, a doc section, or a handler falls out
of sync with it. Harnesses that hand-maintain a prompt description alongside a
separate dispatch table can silently drift; this one cannot pass its own
tests while doing so.

None of this makes the underlying model smarter - a 4B model is still a 4B
model. What it changes is how much of that model's unreliability the harness
absorbs before it reaches you: fewer thrown-away generations, edits that land
where they were meant to, multiple terminals that do not overwrite each
other, an edit that breaks the build saying so in the same turn rather than
the next time you run anything, and a wrong edit that is always one `/undo`
away rather than a reason to `git stash` before every session.
