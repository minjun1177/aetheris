# Providers

[← README](../README.md)

## Providers

The harness starts on Ollama and stays there until told otherwise. `/connect`
moves it:

```
/connect                     pick a provider, then a model from its own list
/connect anthropic           pick a model from Anthropic
/connect openai gpt-4o       connect straight to a model
/connect status              every provider, and what each one still needs
/connect forget anthropic    delete the API key saved for a provider
```

| Provider | Endpoint | Key from |
| :--- | :--- | :--- |
| `ollama` | local, `OLLAMA_HOST` or a `base_url` | none needed |
| `anthropic` | `api.anthropic.com` | `ANTHROPIC_API_KEY` |
| `openai` | `api.openai.com/v1`, or any compatible `base_url` | `OPENAI_API_KEY` |
| `gemini` | `generativelanguage.googleapis.com` | `GEMINI_API_KEY` or `GOOGLE_API_KEY` |
| `attacca` | `wss://attacca.cc`, or `ATTACCA_SERVER_URL` | a pairing code, or `ATTACCA_CREDENTIAL` |

Because `base_url` is settable, the `openai` entry also reaches anything that
speaks the same protocol - a local vLLM or llama.cpp server, OpenRouter, Groq,
Together.

Keys are read from the environment first. A key typed at the `/connect` prompt
is written to `~/.aetheris/providers.json` - never into the project directory,
which is a place people commit from. On Linux and macOS the file is owner-only
(0600) from the moment it is created. Windows has no POSIX mode bits, so there
the file takes whatever ACL its directory gives it; `%USERPROFILE%` is
per-user, but if that matters to you, keep the key in the environment instead.

### Where keys are kept

**With `aetheris[keyring]` and a keyring to put them in**, a saved key goes to
the macOS Keychain, the Windows Credential Manager or the Secret Service on a
Linux desktop, and `providers.json` keeps only `"key_store": "keyring"` in its
place. A key already in the file moves the next time the harness starts.
Without the package, or with no keyring to use - WSL, a server, a container -
or with `AETHERIS_KEYRING=off` or `/set KEYRING_ENABLED off`, keys stay in the
file at 0600 as before, and a
key the keyring refuses is written there rather than lost. **A keyring that
does not answer within 10 seconds** - gnome-keyring on WSL or over SSH, waiting
on an unlock prompt with no screen to show it - is skipped for the rest of the
run, and the start says so; `/set KEYRING_ENABLED off` (or `AETHERIS_KEYRING=off`)
skips the wait.

**What a keyring does not do** is keep out a program running as you: it can
ask the keyring through the same API, and on most desktops gets an answer
without a prompt. What it removes is the plaintext sitting in a well-known
file - the one a script, a backup or another assistant finds first.

**The model never sees a key either way.** That does not depend on the keyring:

- **Hidden in everything it is shown.** A key the harness holds - saved, from
  the keyring, or from `ANTHROPIC_API_KEY` and the rest - is replaced in every
  tool result, `@` attachment and `!` output by `[hidden: anthropic key]`.
  Unlike a `.env` placeholder it is never filled back in, so it cannot be sent
  anywhere either. `SECRET_REDACT` does not switch this off.
- **`providers.json` is off limits to every tool.** Reading, copying, moving or
  writing it is refused before the permission rules, so no allow rule and no
  `/automode` reaches it, and a symlink to it is the same file. A write is the
  one redaction could not undo: a `base_url` pointed elsewhere would send the
  key there with the next request.
- **Not in the model's environment.** `run_cmd` and `run_python` start without
  the provider key variables, so `env` has nothing to print. A command you type
  with `!` keeps your whole shell. A project whose commands need a key keeps it
  in `.env`, where the vault fills it in by name.
- **Closed to other accounts.** `~/.aetheris` is created 0700, and an existing
  `~/.aetheris` or `~/.localchat` is closed at startup - sessions and the input
  history used to be readable by anyone who could reach the directory.
  Sessions are written 0600. A directory named with `AETHERIS_HOME` is created
  closed but never changed under you.

### Attacca

[Attacca](https://attacca.cc) is not a model, and this is not one more API to
send a conversation to. It runs the agent loop on its own servers - its own
prompt, its own context, its own model; Qwen among them - and reaches back into
this machine whenever the agent wants a file read or a command run. That makes
it the way to use the harness on a machine that cannot run a model at all.

```
/connect attacca          pair this machine (once), then pick an agent
/connect forget attacca   drop the saved credential
```

**Pairing.** The first `/connect attacca` shows an eight-character code and the
page to type it on - from any device with a browser - plus a QR code of that
page. Approve all five scopes it asks for: `agents:read`, `sessions:read`,
`sessions:write`, `events:read` - everything a turn uses - and `jobs:write`,
which is the one way into Attacca's plan mode (see below). Works, files and
peers are never touched, so they are never asked for. A machine paired before
`jobs:write` was asked for keeps working without it; `/connect forget attacca`
and pairing again grants it. What comes back is a long-lived `zc_` credential, saved owner-only in
`providers.json` like an API key; `ATTACCA_CREDENTIAL` supplies one from the
environment instead. Revoking it from Attacca's settings ends it everywhere:
the next connection is refused, and the harness forgets it and says to pair
again.

**The agent list is the model list.** `/connect attacca` lists your agents with
the model each runs on, and `/models` does the same. The choice is kept by name
and never quietly swapped for another: an agent that has since been deleted is
an error that names it.

**A turn.** Your line goes to an Attacca session, and the reply streams back
through the same renderer every reply goes through. The session's cost is
shown once at the end, as Attacca reports it. `/clear` starts a new session
there too; `/resume` picks the old one back up, because its id is saved with
the session file. Ctrl+C stops the turn on Attacca as well, keeping only as
much of the answer as was actually on your screen - and so does closing the
terminal (SIGHUP) or a SIGTERM, which a turn left running there would go on
paying for. An agent that speaks, calls a tool and speaks again has said two
things, and both are kept in the saved conversation.

**Messages typed in the web app show up here.** A session belongs to your
account, not to a terminal: the one this terminal is on is open in Attacca's
web app too. A message sent from there closes the prompt - what you had
half-typed comes back - and its turn is drawn here the way one of your own is:
the line itself, marked `↑ sent from Attacca's web app`, the answer as it
streams, the tools it runs on this machine. Both go into the conversation and
the session file. Messages sent while this terminal was closed are read back
the next time it is at the prompt on that session, from the last event it had
drawn. Ctrl+C there stops the *following*, not the turn - it was never this
terminal's to stop. A question or a plan raised in such a turn is shown with a
note instead of being asked a second time: your next message answers it, from
either screen.

**`/planmode` is Attacca's own plan mode.** With it on, your next message
starts a plan-mode job: the agent investigates, hands the whole plan back and
waits. It is drawn as it was written, and Enter approves it; anything else you
type is sent as what to change, and the agent revises and submits again. There
is no separate approve call - the decision is simply the next message - so this
terminal and the web app decide the same way.

| | |
|:---|:---|
| **A job has a session of its own** | The plan and the work happen there, and this terminal moves to it. What was said before stays in the earlier session - said when it happens |
| **One job per stretch of work** | What you type after the plan goes on in the job's session. `/planmode on` again, or `/clear`, makes the next message a new plan |
| **Without `jobs:write`** | A pairing that was not granted it is told so once, and the plan is asked for through this machine's own `submit_plan_for_approval` instead, as it is of a local model |

**What `!commands` printed goes with your next message.** Attacca keeps its
own conversation and is sent one message a turn, so the commands you ran since
its last answer - and their output - are put in front of that message.
`/set RETURN_SHELL_OUTPUT off` sends neither.

**Artifacts are read here.** An agent files a long result as an artifact
(`job_create_artifact`) rather than saying it. The call is shown with the
artifact's title and first lines, and `/artifacts` shows it in full - read from
the session's own history, so one made while this terminal was closed is there
too.

| Command | What it does |
|:---|:---|
| `/artifacts` | The artifact, or the list when the session has several |
| `/artifacts <n>` | That one, in full |
| `/artifacts open [n]` | Open it in the browser instead |

**Important:** nothing a node can call reads an artifact back from Attacca, so
what is shown is what the agent *wrote* - the content its call carried. One
that is a page rather than text (HTML, SVG), or whose call carried no content,
is opened in the browser: at its own address when the call gave one, at
Attacca's web app otherwise.

**What it is doing, as it does it.** Attacca titles each block of reasoning and
heads each stretch of work, and those lines appear as they land:
`✻ 사용자 요청에 맞춰 도구 목록을 정리하는 중`, `▾ Looking for the scroll math`.
The last ones are written after the turn has ended, so they can appear under
the answer. A sub-agent shows while it runs and when it is done, an error
Attacca recorded is said rather than dropped, and the run's own report reads as
its result.

**Questions are answered where they are asked.** When the agent asks with
Attacca's `question` tool, the options are put to you inside the turn - a
number, several for a multi-select, or your own words - and the answer goes
back with the question it answers. Blank skips a step;
nothing at all tells the agent you will not answer, rather than leaving it
waiting.

**What the agent is told.** The session starts with where it is working and
what a local model would have been told about the project: its conventions
file (the same one the local system prompt reads), the skills on disk, and the
names in `.env`. Fixed for the
session's life; `/clear` picks up a changed file.

**The tools run here, under your rules.** The agent sees this harness's own
tool table as the capability `aetheris` - the same tools, MCP ones included,
described the same way - and every call it makes is run by the same dispatcher
a local model's calls are: permission rules, approval prompts, the `.env` vault,
`/undo`, and the file claims on the agent channel all see it the same way. Two
tools are left out, because they only mean something to the local loop:
`spawn_agent` and `view_image`.

**Calls nobody here made.** The machine stays reachable while the prompt is
waiting, so an agent you are talking to in Attacca's web app can use it too.
Such a call closes the prompt for as long as it runs - whatever you had
half-typed comes back when it reopens - and is marked as coming from outside
this terminal. It meets exactly the same rules and prompts; Ctrl+C at its
approval prompt refuses that call rather than quitting.

**The title is Attacca's.** Attacca names every session from its first
message, in the language it was written in, and the saved session is filed
under that name - the one the web app shows for it. Nothing here asks a model
for a title; `/autotitle off` stops it being taken, and `/title` still renames
by hand.

**What does not apply.** `/deepthink` and the context compaction both work by
asking a model this harness drives, and there is none here: Attacca keeps its
own context. A message sent while another turn is already running on the same
Attacca session waits for that one to finish first.

**`/usage` asks Attacca.** The stream carries no token counts, and what the
agent reads - Attacca's prompt, the tool table, every tool result - never
passes through here. So `/usage` shows the session as Attacca meters it: the
context the agent holds now, input and output summed over every request of
every turn, and the credits used. Input runs far ahead of context, because each
tool round re-reads the whole conversation. The account's plan and the balance
left on it are shown above that, as Attacca words them, and the plan is named
on the one-line cost under each answer - the "credits used" there is a share
of it.

### Prompt caching

Every request re-sends the same ~6,000 tokens of system prompt and tool
schemas. Against Ollama that is re-counted but not re-computed - llama.cpp
reuses the KV cache for an unchanged prefix, so a 6,000-token prefix costs
about 0.05s to "process" the second time. Against a hosted API it is billed
every time, and a turn that takes sixteen tool calls pays it sixteen times.

The three hosted providers split two ways, and only one needed code:

| Provider | Caching | What the harness does |
| :--- | :--- | :--- |
| Anthropic | Explicit - nothing is cached without a `cache_control` breakpoint | Marks two: the end of the system prompt, and the end of the conversation so far |
| OpenAI | Automatic above a per-model minimum | Nothing to send; reads `cached_tokens` back |
| Gemini | Automatic ("implicit caching") on 2.5 and newer | Nothing to send; reads `cachedContentTokenCount` back |

Anthropic renders `tools`, then `system`, then `messages`, so the single
breakpoint on the system block covers the tool schemas too - which is two
thirds of the fixed cost. The second breakpoint sits on the last message, so
the next request in a tool loop reads the whole conversation before it back
out of the cache instead of paying for it again.

**The reporting is the part that matters.** Caching is a prefix match: one byte
that moves invalidates everything after it, and it fails *silently* - as a
bill, not an error. So `/usage` prints what actually happened:

```
  cache 12,200 prompt tokens read from Anthropic's cache - 63% of all input, billed at a fraction
```

...and says so when it is not working:

```
  cache nothing read from Anthropic's cache in 4 requests. Either the prompt is
        under that model's minimum, or something is rewriting the prefix
```

Nothing is claimed for Ollama, which reuses its prefix locally, charges nothing
for it and reports nothing about it.

Two things in this harness rewrite the prefix and cost one miss each when they
happen: the `<SUMMARY>` that compression writes into the system message, and
`context._trim_tool_results` the first time a tool result exceeds its ceiling.
Both converge - a result is not trimmed twice - so neither is a permanent miss.

`/connect forget <provider>` takes a saved key back out. `/connect` only ever
asked for a key when there was none, so one pasted into the wrong provider, or
one that has since been revoked, used to stay in that file with nothing in the
program able to remove it. Only the key goes - the model beside it is not a
secret, and keeping it makes reconnecting one step. A key coming from an
environment variable is not touched, because this program cannot unset your
shell's variables, and it says so rather than appearing to have done something.

### How tools are asked for

Two protocols, chosen by who is answering:

| | Tools are | Tool calls come back as |
| :--- | :--- | :--- |
| **Anthropic, OpenAI, Gemini** | sent with the request | the API's own tool-call events |
| **Ollama, model supports tools** | sent with the request | Ollama's own tool-call events |
| **Ollama, model does not** | listed in the system prompt | `<tool_call>` text, repaired if malformed |

For Ollama this is decided **per model, not per provider**: whether a model can
call a tool depends on the template it was built with, and Ollama says so
outright. Of the twenty models installed on the machine this was written on,
five have no tool support. Each one is asked once and the answer cached; a
daemon that is down, or a model that cannot be asked, means the text protocol -
which works everywhere.

That fallback is the point. A model whose template cannot format a tool call
will simply never make one, and the text protocol plus its JSON repair engine
is what makes those models usable at all. A model that *can* is more accurate
through the real interface, and about **12KB of prompt cheaper per turn**,
because the tool list no longer has to be spelled out:

```
text protocol      system prompt 17.9KB   (tool schemas + <tool_call> rules)
native tool calls   system prompt 5.9KB   (schemas travel with the request)
```

Both protocols describe the same tools, from the same table in `toolspec.py`,
so they cannot drift apart. Both end up as the same `(name, arguments)` pair,
so dispatch, permissions, display, session files and context compression see no
difference - the history stays plain text either way, and a session saved from
one provider replays under another.

Set `NATIVE_TOOLS = False` in `config.py` to force the text protocol everywhere,
which is what an OpenAI-compatible server without tool support needs.
`/connect status` shows which protocol is in use.

**Why this is still small.** `providers.py` normalises four wire formats into
one event shape - text, thinking, tool call, done - and everything downstream is
untouched by which provider is answering. No vendor SDKs.

The shapes that do differ are handled in one place: Anthropic and Gemini take
the system prompt as its own field rather than a message, Gemini calls the
assistant role `model`, and both want consecutive same-role messages merged -
which the harness produces constantly, since every tool result is its own user
message.

---

## Reasoning Models

Reasoning models (qwen3, deepseek-r1, gpt-oss) emit their scratch work before
the answer - either wrapped in `<think>` tags in the content stream, or in
Ollama's separate `thinking` field. It is not the answer, so:

- it is **not printed** (`/think on` shows it dimmed if you want to watch),
- it is **never stored** in the conversation history, which matters most: on a
  local model the context budget is small, and reasoning is often longer than
  the answer it produces.

Set `config.STORE_THINKING = True` to keep it in history anyway, or
`config.SHOW_THINKING = True` to have it shown from startup.

The same stream filter hides the `<tool_call>` tag, so a model that explains
itself and *then* calls a tool shows only the explanation.
