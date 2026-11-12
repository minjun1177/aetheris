# Configuration

[← README](../README.md)

## Configuration

Most behaviour is reachable from a slash command, and those changes last for the
session. **`/set` changes what it starts as, without editing any source.**

```
/set                      every setting, with the changed ones marked
/set NUM_CTX 32768        change one - for this session and the next
/set NUM_CTX default      put it back to what config.py says
```

What was changed is written to `~/.aetheris/settings.json` and applied over
`config.py` at startup. **Only the deviations are recorded**, so a default that
improves in a later version still reaches you if you never overrode it - writing
all sixty out would freeze this release's values the first time you changed one.

A setting is any `UPPER_CASE` name in `config.py` holding a number, a switch, a
string or a list, so a setting added there is settable the moment it exists.
What is *not* settable is named rather than listed, and it is a short list: the
system prompt and the model (they have commands of their own), live state such
as the session title, facts about the machine, the two tool-result markers
(invariant 5.9 - a protocol, not a preference), and the paths under
`~/.aetheris`, which `AETHERIS_HOME` moves together.

Values are checked against the type the setting already has - `on`/`off` for a
switch, a number for a number, commas for a list - and a negative number is
refused, because none of them mean anything below zero and one fails much later
and somewhere else. Nothing above zero is second-guessed: this file was always
editable by hand, and the same latitude belongs here. A `settings.json` that
will not parse is ignored entirely and the harness starts on the defaults.

`config.py` is still where the defaults live, and where each one is commented.
The settings worth knowing:

| Setting | Default | What it does |
| :--- | :--- | :--- |
| `MODEL` | `gemma4:e4b` | The Ollama model used until `/connect` says otherwise |
| `OLLAMA_HOST` | `http://localhost:11434` | Where the Ollama daemon is. `/set OLLAMA_HOST http://box:11434` points the harness at another machine - the chat, the model list, the tool-support probe and the summariser all follow it. While this is at its default a `$OLLAMA_HOST` in the environment is honoured instead; changing it here wins over both |
| `NUM_CTX` | 65536 | Context window asked of Ollama, and the one used for any model whose own is unknown. A hosted model that reports its window (Anthropic, Gemini, many OpenAI-compatible servers) uses that instead; `/context <tokens>` sets one model's by hand. The system prompt and tools alone are ~6-8k tokens, so below 16384 there is no room left to work in (the harness says so rather than compressing). A big window costs GPU memory for the KV cache: on an 8GB card Ollama may spill to the CPU and slow down - lower it there with `/context` |
| `NUM_PREDICT` | 6144 | Output cap. Must be an int - every hosted API rejects a float |
| `UPDATE_CHECK` | `True` | Look once a day, in the background, for a newer Aetheris on PyPI and say so once. Nothing is installed without `/update` and a yes |
| `TURN_BELL_SECONDS` | 30 | Ring the terminal bell when a turn that took at least this long ends - not after Ctrl+C, and never into a pipe. 0 turns it off |
| `PROVIDER_RETRIES` | 3 | Times a request refused with 429 or a 5xx, or a dropped connection, is tried again - after 1, 2 and 4 seconds, or as long as `Retry-After` asks, up to a minute. Never once a reply has started arriving. 0 turns it off |
| `OLLAMA_KEEP_ALIVE` | `30m` | How long Ollama keeps the model loaded after a request. Its own default is five minutes, after which the next turn waits for the model to load again. `-1` keeps it loaded; empty leaves it to the server |
| `OLLAMA_TEMPERATURE` | unset | Sampling temperature sent to Ollama. Unset sends nothing and the Modelfile decides; `/set OLLAMA_TEMPERATURE none` puts it back |
| `OLLAMA_SEED` | unset | A fixed seed, for runs that have to be repeatable - a benchmark, a comparison |
| `NATIVE_TOOLS` | `True` | `False` forces the `<tool_call>` text protocol everywhere |
| `MAX_TOOL_CALLS` | 10 | Tool calls per turn before asking whether to continue |
| `AUTO_ALLOW` | `False` | `True` is `/automode on` from startup - no approval prompts |
| `PERMISSIONS_ENABLED` | `True` | Whether `.permissions.json` rules are consulted at all |
| `SECRET_REDACT` | `True` | Whether `.env` values are hidden from the model and pasted back in by the harness - see [Secrets in `.env`](permissions.md#secrets-in-env) |
| `SECRET_MIN_LENGTH` | 8 | Below this a value is a word like `dev`, not a secret, and hiding it would rewrite every result that mentions it |
| `SECRET_FILES` | `[]` | Extra filenames to treat the way `.env` is treated |
| `KEYRING_ENABLED` | `True` | Keep saved API keys in the OS keyring where there is one. `/set KEYRING_ENABLED off` moves them into `providers.json` (0600) at once, and `on` moves them back. `AETHERIS_KEYRING=off` in the environment still switches it off |
| `GIT_AUTO_COMMIT` | `True` | A commit per AI edit, so `/undo` has something to take back |
| `AUTO_VERIFY` | `True` | Run the project's own check after a turn changes a file |
| `VERIFY_TIMEOUT` | 90 | Seconds one check gets before it is killed and turned off |
| `VERIFY_OUTPUT_CHARS` | 2000 | Of a failing check, how much of the tail the model is shown |
| `DEEPTHINK` | `False` | Start with the six-stage chain on |
| `DEEPTHINK_MAX_PASSES` | 3 | Times the chain may start over when the final check says it is not done |
| `SUBAGENT_MAX_TURNS` | 12 | Turns a sub-agent gets before it must report |
| `SUBAGENT_MAX_DEPTH` | 1 | 1 means sub-agents cannot hire sub-agents |
| `SHOW_THINKING` | `False` | Show a reasoning model's scratch work |
| `STORE_THINKING` | `False` | Keep it in the history too. Expensive on a local model |
| `CHANNEL_ENABLED` | `True` | Join the board other harnesses in this project share |
| `CHANNEL_CLAIMS` | `True` | Refuse an edit to a file another agent is holding |
| `CHANNEL_CLAIM_TTL` | 1800 | Seconds a `claim_files` claim lasts |
| `CHANNEL_WRITE_TTL` | 300 | ...and one taken automatically by writing a file |
| `CHANNEL_STALE` | 120 | Heartbeat age past which an agent is presumed gone |
| `CHANNEL_POLL_SECONDS` | 2 | How often an idle prompt looks for a new message |
| `MENTION_MAX_CHARS` | `40000` | Ceiling on what one `@path` may add to the context |
| `REMOTE_ENABLED` | `False` | Open the remote-control door at every start. `/remote on` opens it for one session |
| `REMOTE_HOST` | `127.0.0.1` | What the remote binds to. `/remote on lan` binds every interface for one session |
| `REMOTE_PORT` | 8765 | The port it tries first; the next 19 are tried before it gives up |
| `REMOTE_LINES` | 500 | Transcript lines kept for the remote to scroll back through |
| `REMOTE_ASK_TIMEOUT` | 300 | Seconds a question waits on the remote before it counts as a no |
| `REMOTE_PAIR` | `lan` | When a browser must also type a code shown on this terminal: `lan`, `always` or `never` |
| `REMOTE_MAX_BAD_TOKENS` | 20 | Wrong tokens from one address before it is shut out |
| `REMOTE_LOCKOUT` | 300 | Seconds it is shut out for. The first wrong token is reported at the prompt either way |
| `AUTO_TITLE` | `True` | Let the model name each new session |
| `SAVE_CHAT_HISTORY` | `True` | Write session files at all |
| `CMD_TIMEOUT` | 120 | Seconds before a runaway command is killed |
| `CMD_WAIT_TIMEOUT` | 8 | Silence before a command is called "probably waiting" |
| `VM_TIMEOUT` | 20 | Seconds a `run_python` snippet gets before the VM is killed and restarted |
| `VM_OUTPUT_CHARS` | 4000 | Ceiling on what one snippet may print back; the middle is dropped |
| `VM_MEMORY_MB` | 512 | Address space the VM may take. Enforced on Linux; accepted and ignored on macOS, absent on Windows. 0 for no limit |
| `VM_FILE_MB` | 64 | Largest file the VM may write. POSIX only; 0 for no limit |
| `MCP_ENABLED` | `True` | Attach MCP servers on startup |
| `MCP_LAZY_TOOLS` | `True` | Announce a big MCP server by name; send its tools when asked |
| `MCP_LAZY_MIN_TOOLS` | 6 | Tools a server needs before it is announced rather than described |
| `MEMORY_IMPORTANT_MAX` | 20 | Memories marked `important` that the system prompt will carry |
| `MEMORY_IMPORTANT_CHARS` | 600 | Characters of each one before it is cut, with a note saying to `read_memory` for the rest |
| `IMAGE_MAX_EDGE` | 1568 | Long edge an attached image is resized to, which is what the hosted providers scale to anyway |
| `IMAGE_MAX_BYTES` | 5242880 | Ceiling on one image after any resize - the strictest of the four APIs |
| `IMAGE_MAX_PER_MESSAGE` | 4 | Images one message may carry |
| `NOTES_TITLES_MAX` | 40 | Project-note titles the system prompt will list |
| `NOTE_MAX_CHARS` | 20000 | Ceiling on one note, which comes back into the conversation whole |
| `SEARXNG_URL` | `""` | A self-hosted search instance to prefer over the public sources |

The rest are tuning knobs for search, MCP and command sessions; they are
documented in the sections above, commented where they are defined, and all of
them are listed by `/set`.

State that outlives a session lives outside `config.py`:

Everything about *you* lives in one directory, `~/.aetheris`. Everything about
*a project* is read from that project's own directory first, and from
`~/.aetheris` second - so a repository can carry its own rules, servers and
skills, and they win.

| Path | Holds |
| :--- | :--- |
| `~/.aetheris/providers.json` | The connected provider and any API keys typed at `/connect` - unless a keyring holds them (*Where keys are kept*). Owner-only on POSIX, and no tool may touch it |
| `~/.aetheris/sessions/*.json` | Conversation transcripts, named after the session title, each recording the directory it was last worked in so `-c` can find it |
| `~/.aetheris/memory.json` | The long-term key-value memory |
| `~/.aetheris/history` | Input history for the prompt |
| `~/.aetheris/channel/*.json` | One board per project: which harnesses are running in it, what they have said to each other, and which files each is holding |
| `~/.aetheris/vm/session-<pid>/` | The `run_python` scratch directory - where the VM runs and anything it writes ends up; deleted when the session ends |
| `~/.aetheris/settings.json` | The settings `/set` changed - only those, never the whole table |
| `./.permissions.json`, then `~/.aetheris/permissions.json` | Allow and deny rules |
| `./.mcp.json`, then `~/.aetheris/mcp.json` | MCP server declarations |
| `./skills/`, then `~/.aetheris/skills/` | Skills |

Set `AETHERIS_HOME` to put that directory somewhere else - two profiles, or a
throwaway one for trying something out. Set `AETHERIS_KEYRING=off` - or
`/set KEYRING_ENABLED off` - to keep keys in `providers.json` even where a
keyring is available.

**Upgrading from simple-harness.** The directory was `~/.localchat` before
1.0.0, and the rename does not move it. If `~/.localchat` is there and
`~/.aetheris` is not, that stays the home and is read exactly where it is -
your sessions, memory and saved keys carry over because nothing happens to
them. Move it yourself if you would rather have the new name:

```bash
mv ~/.localchat ~/.aetheris        # optional, and only once
```

`LOCALCHAT_HOME` is still read when `AETHERIS_HOME` is unset, and
`SIMPLE_HARNESS_ACCEPT_TERMS` still counts as agreeing to the terms, so a shell
profile or an unattended job that sets either one keeps working untouched.

Before 0.2.0 the sessions, the memory and the input history were written into
whatever directory the harness started in. If you have those, they are not read
any more and nothing has moved them; the harness names them at startup and
prints the one line that moves them across.
