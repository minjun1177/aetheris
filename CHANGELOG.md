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

## Unreleased

### Attacca: an agent somewhere else, the tools here

For a machine that cannot run a model of its own. `/connect attacca` pairs this
machine with [Attacca](https://attacca.cc) by an eight-character code, and from
then on a turn goes to an agent hosted there - Qwen among the models on offer -
while every tool it calls runs here.

Attacca is not a model, so this is not one more wire format. It runs the agent
loop on its own servers and reaches back over Zyris, its websocket protocol,
whenever it wants a file read or a command run. So connecting to it changes who
drives a turn. `chat_turn`, compaction, `/deepthink` and the session titles all
step aside for it; what stays is everything that touches this machine.

- **The same rules.** A call from Attacca's agent goes through `dispatch_tool`
  exactly as a local model's does: permission rules, approval prompts, the
  `.env` vault, `/undo`, the file claims on the agent channel. The agent sees
  the harness's own tool table, MCP tools included, minus `spawn_agent` and
  `view_image`, which only mean something to the local loop.
- **The narrowest grant that works.** Four scopes - `agents:read`,
  `sessions:read`, `sessions:write`, `events:read` - and a `zc_` credential
  saved owner-only like an API key, or read from `ATTACCA_CREDENTIAL`. A
  credential revoked in Attacca is forgotten here, with the way to pair again.
- **Reachable between turns.** An agent in Attacca's web app can use this
  machine while the prompt waits. The call closes the prompt, says it came from
  outside, meets the same rules, and gives back whatever was half-typed.
- **Stopping is honest.** Ctrl+C stops the turn on Attacca too, keeping only as
  much of the answer as reached the screen.
- **`/usage` asks Attacca.** Context, input, output and credits for the
  session, as Attacca meters them. The local graph had nothing to draw and its
  context estimate counted only the questions and final answers kept here:
  "no data" and ~8,700 tokens for a session Attacca had metered at 864,077.

`pip install "aetheris[attacca]"` - two packages, `websockets` and `msgpack`,
that nothing imports until Attacca is the provider.

Checked against attacca.cc itself, not only the spec: pairing, the parameter
shapes, how its events name this machine's tools, and when a turn has ended.
That last one was wrong at first - attacca.cc reports the session's state the
moment a stream opens, which is "not running" before the message goes in - and
`tests/test_attacca.py` now plays exactly that.

### Attacca: what a turn is doing, and the questions it asks

Checked on attacca.cc.

- **What it is thinking, in a line.** Attacca titles each reasoning block and
  heads each stretch of work by rewriting the event; those titles now appear
  (`✻ …`, `▾ …`), once each however often the event is rewritten. The last
  ones land after the turn has ended, so the turn listens up to two seconds
  more for them.
- **Questions answered inside the turn.** The agent's `question` waits on the
  server for the next message, and the prompt only came back once the turn
  ended - so the turn sat until the server gave up. The options are now put to
  the person at once, and the answer goes back with its question.
- **Errors, reports and sub-agents.** An `error` event is shown instead of
  dropped; `report_result` reads as the run's result; a sub-agent shows when it
  starts and when it is done or failed.
- **The project, told to the agent.** The session preamble carries the
  project's conventions file, the skills catalogue and the `.env` names - what a
  local model is told in its system prompt, which Attacca's agent never sees.
- **Closing the window stops the turn.** SIGHUP and SIGTERM mid-turn cancel it
  on Attacca first, instead of leaving it running and failing every call.

### The harness's own keys, out of the model's reach

`providers.json` held every API key in plaintext, and nothing kept a model
with `read_file`, `cat` or `env` away from it - one call and the key was in the
conversation, at the provider and in a session file.

- **Hidden in everything a model is shown.** Saved keys, keyring keys and the
  provider variables in the environment read as `[hidden: anthropic key]` in
  every tool result, `@` attachment and `!` output. Never filled back in, so
  it cannot be sent anywhere; `SECRET_REDACT` does not switch it off.
- **`providers.json` refused to every tool**, ahead of the permission rules
  and `/automode`, by any path that resolves to it. A write would otherwise be
  the way round: a `base_url` pointed elsewhere sends the key there.
- **The model's commands start without the provider keys** in their
  environment (`run_cmd`, `run_python`). A `!` command keeps the whole shell.
- **The OS keyring, optionally.** `pip install "aetheris[keyring]"` keeps
  saved keys in the Keychain, the Credential Manager or the Secret Service,
  and moves existing ones out of the file at the next start. No keyring, or
  `AETHERIS_KEYRING=off`: the file at 0600, as before. A keyring that does
  not answer within 10 seconds - gnome-keyring on WSL, waiting on an unlock
  prompt it has nowhere to show - is skipped for the rest of the run, with a
  warning at startup, instead of hanging it.
- **Closed to other accounts.** The home directory is created 0700, an
  existing default one is closed at startup, and sessions are written 0600.
  They used to be 0644 in a 0755 directory.

### A reply nobody counted is not a line of zeros

`stream_reply` used to record and print a token line for every reply, counted
or not. A provider that reports no counts now gets neither, instead of
`tokens: 0 in · 0 out` and a zero row in `/usage`.

### Resizing the terminal redraws the conversation

A terminal does not reflow what was printed for another width: narrower, every
rule, table and code frame wrapped into a second ragged line; wider, the
transcript stayed in the left part of the window. Now a resize under the prompt
clears the screen and its scrollback and draws the banner and the conversation
again at the new size, the way `--resume` replays one. It waits for the size to
settle, so a dragged edge is drawn once; whatever was half-typed is put back;
and a resize during a turn is redrawn when the prompt returns. Output that never
entered the conversation - `/help`, a listing - is not kept and does not come
back.

### A replayed conversation looks like the one that was had

`/load` used to print the session below whatever the screen already held, and
the replay drew turns as nothing had drawn them: `!echo 1` as a message reading
`[Shell] $ echo 1`, the harness's own notes to the model as if typed, the files
an `@` attached pasted under the line, and every answer after the tools it had
called instead of before. Now `/load` clears the screen and draws from the top,
as `--resume` does, and each turn is drawn the way it was live.

### Ctrl+C at the prompt says goodbye again

Pressed at the prompt, Ctrl+C left through `asyncio.run` - past the main
loop's goodbye and shutdown - and printed `Task exception was never retrieved`
with a traceback on the way out. The prompt runs as a task so it can be raced,
and asyncio raises a `KeyboardInterrupt` from a task out of the event loop
rather than to whoever awaits it. It is now handed back and raised where the
main loop catches it.

### `/set KEYRING_ENABLED`

`AETHERIS_KEYRING=off`, as a setting. Switched off, the saved keys move into
`providers.json` (0600) at once, rather than at whatever saves next; switched
on, they move back into the keyring. The environment variable still switches
it off when set, and `/set` says so instead of appearing to work.

---

## 1.0.0 - 2026-10-06

Three things, and the version number is about the third. The session can be
driven from a phone; the harnesses running in one project can talk to each
other while they work; and the project is called Aetheris.

### Remote control: one door into the session that is already running

A harness is a terminal, and a terminal is somewhere you have to be. The moment
a request takes minutes rather than seconds - a `/deepthink` pass, a suite the
model is chasing - the two things you need are *what is it doing* and *yes, go
ahead*, and both are behind a keyboard you have walked away from. Worse than
slow: a turn that stops at `Allow? [y/n]` on a screen nobody is looking at has
hung, and nothing says so.

`/remote on` prints a link. Open it on a phone and you are at the prompt - the
transcript as it is printed, a box that types into the same loop the keyboard
types into, and the approval prompts themselves, with buttons.

The rule that makes it a control rather than a viewer: **a question is asked
wherever the person driving the turn is.** A line typed on the phone marks the
turn, and every blocking question in the harness - the approval prompt,
`get_input`, `submit_plan_for_approval` - now goes through one place that knows
which that is. Both are still printed on the terminal, so the person at the
desk can read what was asked and what came back. Nobody answering inside
`REMOTE_ASK_TIMEOUT` is a no.

What is behind the link is a shell, so: off until `/remote on`; loopback unless
`/remote on lan`, which says what it is doing in as many words; a token made
when the door opens, printed once, never written to disk and gone when it
closes - 128 bits on loopback, 256 for `lan`, which is the one that crosses a
network somebody else is also on; a `Host` that is not this machine refused
before the token is read; wrong tokens counted per address and shut out after
`REMOTE_MAX_BAD_TOKENS` of them; and the mirrored transcript redacted the way
the model's copy is, so a `.env` value that is on your screen because *you* ran
`!cat .env` does not go out over the wire.

And you are told who is there. The first request from an address, and the first
wrong token from one, arrive at your prompt the way another agent's message
does - `◆ 192.168.0.14 opened the remote link.` On a shared network the
question worth answering is not whether somebody *could* get in but whether
they did, and nothing else here can answer it.

**Over a network the link is not enough on its own.** A browser that arrives
over `lan` is shown a box rather than the transcript: six digits, printed in the
terminal the harness runs in, good for two minutes and three guesses. Type them
on the phone and it gets a session of its own; anything else stays outside.
That is a second factor rather than a second copy of the first - the link
crosses the network and can be photographed, read aloud or left in a history,
and the terminal cannot. `REMOTE_PAIR` chooses when it is asked (`lan`,
`always`, `never`) and `/remote forget` drops every browser that has paired.

**`/remote qr`** draws the link as something to point a camera at, because
nobody types forty-three random characters into a phone twice. Black modules on
a white ground the harness paints itself, so it scans in any terminal theme.
There is no library behind it: `qr.py` is a byte-mode encoder in the stdlib,
level M, versions 1 to 9. `tests/test_qr.py` reads each symbol back the way a
scanner does - the mask out of its own format bits, the zigzag, the blocks - and
checks that every block still satisfies its Reed-Solomon parity, which is one
check over the format bits, the placement, the block tables, the interleaving
and the arithmetic at once.

It is plain HTTP, which on loopback is the whole story and over `lan` is a
network you are choosing to trust. There is deliberately no TLS and no account:
from anywhere else, forward the port over `ssh -L`.

`REMOTE_PORT` and `REMOTE_HOST` are ordinary settings, and `/set REMOTE_PORT
9000` at a prompt with a remote already open *moves* it - new token, new link,
printed on the spot - rather than waiting for a restart.

### Fixed, from a read of every file in the tree

Seven faults, each with a test that fails without its fix.

- **An anchor on line 0 was applied instead of refused.** `_anchor_problem`
  looked for the first out-of-range line number with `next(..., 0)`, so the one
  value it could not report was the one it was looking for. `0:<hash>` skipped
  the range check, fell through to the hash comparison - which indexes
  `lines[0 - 1]`, the *last* line of the file - and a hash that matched made
  the anchor "verified". `_edit_by_anchor(start=0)` then assigns to
  `lines[-1:0]`, an empty slice, so the replacement was **inserted** near the
  end of the file and reported as `[Success] line 0 replaced`. A write nobody
  asked for, under a success message, is exactly what invariant 5.12 exists to
  prevent.
- **The refusal that hands back the lines it meant could itself raise.**
  `38|print()` carries no hash and is refused with what those lines actually
  say, so the next call can be right without a `read_file` round trip. It built
  its example from `numbers[0]` unconditionally, so a call naming one real line
  and one imaginary one - enough for a listing, not enough to index - raised
  `IndexError` out of the handler, and the model was told `edit_file raised
  IndexError` instead of being shown the anchors it was missing.
- **A damaged session file could not be reopened.**
  `skills.loaded_skill_names` asked every entry for a key without checking it
  was a dict, the way `mcp_client.loaded_in` beside it does. One bare string in
  the list raised `AttributeError` out of `app._adopt_session`, which runs
  *before* `_replay_session` - so the resume died earlier than, and in spite of,
  all the care taken to make replaying a holey file survivable.
- **A token that was not ASCII got past the lockout.**
  `secrets.compare_digest` refuses two *strings* when either holds a character
  outside ASCII: it raises `TypeError` rather than answering False. So `?k=%C3%A9`
  left the handler through `handle_error` - the connection dropped with no HTTP
  reply at all, `note_bad_token` was never reached, and the count that leads to
  `REMOTE_MAX_BAD_TOKENS` never moved. An address could knock all afternoon
  and appear at the prompt only as a stream of TypeErrors, which is the one
  warning that module promises on a network somebody else is on. Both sides are
  compared as UTF-8 bytes now. The pairing code had the same fault one factor
  along, because `str.isdigit()` is true of `٣` as well as `3`, and a guess made
  of those raised instead of spending one of its three tries.
- **`/system` lower-cased the persona.** It read its argument off `cmd`, the
  line lower-cased so the command itself can be matched case-insensitively, and
  it is the only command whose whole argument is prose meant to reach the model
  verbatim: "You are a Korean tutor named Minji" was stored as "you are a korean
  tutor named minji". `/export` had it too, and wrote its markdown out under a
  lower-cased spelling of the filename - which on a case-sensitive filesystem is
  a different file from the one asked for. Every other command in the loop
  already reads `user_input`.
- **Setting a persona dropped the important memories from the prompt.**
  `/system` wrote the system message by hand as `persona + SYSTEM_PROMPT +
  summary`, which is everything `_compose_system_prompt` builds except the two
  blocks it adds: the memories marked important (5.15) and this project's note
  titles (5.16). Both went missing until something else happened to rebuild the
  prompt. It composes through `_refresh_system_prompt` now, which carries the
  `<SUMMARY>` across on its own.
- **A sub-agent was offered a tool and then refused it.** `withheld(depth)`
  exists so that the listing, the schemas and the refusal cannot disagree, and
  the refusal in the loop was the one of the three still reading the raw
  `DENIED` tuple. With `SUBAGENT_MAX_DEPTH` raised above 1 - an ordinary `/set` -
  a depth-1 sub-agent was handed `spawn_agent` in its prompt and in its schemas
  and then told it did not have it, so it spent its budget knocking.

`/export` and `/system` are now `_export_command` and `_system_command`
alongside the `/set`, `/agents`, `/remote` and `/vm` helpers they sat between,
which is what makes both testable.

### Fixed, from the first afternoon of it running on Windows

- **A message that arrived while you were at the prompt lost its colours** and
  arrived as `?[38;2;250;189;47m◆ …` instead. Printing above a live prompt goes
  through prompt_toolkit's own console writer on Windows, which hands escape
  sequences to the console as characters; they are handed over as `ANSI(...)`
  now. The agent channel's messages had the same fault and the same fix.
- **Opening the link reported you at your own prompt as an intruder** - twice,
  once for the tab icon and once for the page. A browser fetches `/favicon.ico`
  and friends by itself, without the token; those paths answer 404 and are
  counted as nothing.
- **A phone that locked its screen printed a stack trace** into the middle of
  the conversation: `socketserver` reports a handler's exception that way, and
  a dropped long poll is `ConnectionAbortedError` on Windows. A socket giving
  way is now the ordinary end of a request, and anything that is not one is a
  single line at the prompt.
- **The notice marker was a glyph Windows Terminal cannot draw.** U+26BF, the
  "squared key", is not in its default font and came out as a box. It is `◆`
  now, from the Geometric Shapes block everything else in this interface uses.
- **`/model` from the phone asked the terminal.** `connect` now asks through
  the same place every other blocking question does, and passes its numbered
  list along as buttons. An API key is the deliberate exception: it is not
  typed over plain HTTP, whoever is driving.
- **The page now knows what may be typed into it.** `/` lists the slash
  commands with what each does - the table `/help` renders, served as
  `/commands` - and tapping one inserts it. `!` turns the box amber and says
  it runs on that machine as you, which is the warning the terminal has had
  over its own prompt since the shell escape existed.
- **Eight blank lines sat under the prompt, all the time.**
  `complete_while_typing=True` is what opens the `/` and `@` menus without a
  Tab, and it is also what makes prompt_toolkit hold `reserve_space_for_menu`
  rows free below the cursor - for the whole time somebody is typing an
  ordinary sentence that will never have a menu. The reservation is read on
  every render, so it is earned now: a `Condition` says yes for a line that
  starts with `/` or carries an `@`, and nothing else. Tab still completes
  anything, any time.
- **Everything printed above a live prompt lost its escapes**, on every
  platform - `?[38;2;250;189;47m◆ …` - because `patch_stdout` sanitises what it
  is handed unless it is opened `raw=True`. It is opened `raw=True` now. The
  first pass at this blamed the Windows console and special-cased it; a pty
  said otherwise.
- **The menu's reserved rows outlived the `/` that earned them.** Deleting the
  slash left the completion state open, and the reservation answers to either
  that or the condition, so the band stayed until Escape. The buffer now closes
  a menu the line has stopped asking for.
- **A line typed while the model worked could answer a question.** It already
  reached the next prompt - the terminal buffers it - but a mid-turn approval
  prompt would take it as its answer, unseen. The keyboard buffer is emptied
  before a question is asked, and the person is told their line was set aside.
- **The tool-call-limit prompt asked the terminal even when a phone was
  driving.** It is the one blocking question that never went through
  `ask_the_driver`; a remote-driven turn stopped there with nothing on the
  phone to say why. It goes through it now, with its two answers as buttons.
- **The page was a wall of grey.** The transcript was stripped of colour on
  its way out; it now keeps the terminal's `ESC [ … m` and the page paints it.
  Every other escape is still removed before sending, and text only ever lands
  as `textContent`, so nothing that arrives can be markup.
- **The prompt itself was being mirrored.** With a remote open before the
  prompt was built, prompt_toolkit drew through the mirror, so every render -
  the bare `❯`, the menu, the redraw after each keystroke - went to the phone.
  The prompt is pointed at the real stream now and the tee sits inside
  `patch_stdout`, where it catches what the program prints and not what the
  renderer draws.
- **Colour that spanned lines was lost.** The banner opens with one escape and
  closes four lines later; published by the line, everything between came out
  white. The page carries the state from line to line, as a terminal does.
- **`/exit` from the page is refused.** It is the one command the link cannot
  undo from where it is typed.
- **A line sent from the page appeared twice** - once echoed locally and once
  when the harness printed it at the prompt and the mirror carried it back.
  The local echo is gone; the transcript's own copy is the one you see, exactly
  as the terminal shows it.
- **The page now says what the conversation costs**: tokens against the context
  window and the number of turns, on a strip above the box, with the spinner
  the terminal turns on the left of it. It is the half of `/usage` that fits on
  a phone.
- **The spinner reached the phone as every frame it had ever drawn.** It is one
  line, rewritten many times a second and never ended with a newline, and the
  `\r` rule was applied to finished lines but not to the one still being
  written - so the buffer held the lot, laid end to end. It holds the frame it
  is on, and nothing once it stops.
- **Redaction was silent about itself.** A `.env` value that is also an
  ordinary word - `PROJECT_DIR=aetheris` - is a secret by the only rule
  that never lets a key through, so `!dir` came back full of
  `{{env:PROJECT_DIR}}` with nothing to say why. A `!` command whose output was
  redacted now names what was hidden. README §13a states the three conditions
  outright.

The transcript is a tee on `sys.stdout` rather than a second rendering, which is
why what the phone shows is exactly what the terminal shows, tool boxes and all.

New: `/remote` (with `qr` and `forget`), `remote.py`, `qr.py`,
`tests/test_remote.py`, `tests/test_qr.py`, and `REMOTE_ENABLED`, `REMOTE_HOST`,
`REMOTE_PORT`, `REMOTE_LINES`, `REMOTE_ASK_TIMEOUT`, `REMOTE_PAIR`,
`REMOTE_MAX_BAD_TOKENS`, `REMOTE_LOCKOUT`.

### The board the harnesses share

People run three of these at once in one repository, and until now none of them
knew the others existed: two would read the same file, and the second write
silently threw the first away. The instances in one working tree now share one
locked JSON board - who is here, what they have said, and which files each is
in the middle of changing. A claimed file is refused to the others **by name**,
so the refusal says who to ask.

Three decisions are what keep it from becoming a chat room:

- **A message is 250 characters and must name a path.** Two 4B models given
  room for an essay negotiate the problem instead of solving it. It is a
  physical limit rather than an instruction, for the same reason everything else
  here is.
- **There is no message kind meaning "please do this for me."** That absence is
  the fix for the one thing two small models reliably did to each other: "could
  you test it?" - "yes, could you test it?" - for a whole run, neither ever
  running anything. A question about state can always name a file; a delegation
  never can, so the shape of the message tells the two apart with no guessing at
  intent.
- **A question put to a terminal nobody is sitting at is answered.** A message
  delivered into an idle session is delivered to nothing - the model reads it on
  the next turn, and the next turn is whenever somebody comes back. So the
  channel presses Enter, but only for a direct question, only on an empty
  prompt, and only `CHANNEL_AUTO_TURN_MAX` times between one human line and the
  next. The budget is spent only by turns that came back with *nothing done*, so
  two agents genuinely working can go back and forth all day.

New: `CHANNEL_AUTO_TURN`, `CHANNEL_AUTO_TURN_MAX`, `CHANNEL_MAX_IDLE_REPLIES`
and `CHANNEL_PLAN_NOTE`; `/agents plan`, `/agents clear` and `/agents release`.

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

---

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
- CI now catches a tag that disagrees with `aetheris.__version__`, before
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
