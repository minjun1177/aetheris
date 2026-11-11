# Slash Commands

[← README](../README.md)

## Slash Commands

The interactive terminal supports special slash commands to control options and inspect state:

| Command | Description |
| :--- | :--- |
| `/help` | The six commands most sessions need, and the two prefixes. `/help all` lists every command and the keys |
| `/usage` | Token cost per turn, as an ASCII chart, plus the cumulative total and what every request pays before the conversation starts. One bar is one thing you asked for, tool calls included |
| `/model` | Show the connected provider and pick another of its models |
| `/models` | List the models the connected provider offers |
| `/context [tokens]` | The context window of the model in use, where the number comes from - set here, reported by the model, or `NUM_CTX` - and where compaction starts. `/context 200k` sets it for this model only; `/context default` takes it back. Kept per model, in `providers.json` |
| `/effort [level]` | How hard the model in use reasons. Alone it offers the levels in its provider's own words - Anthropic `low` to `max`, OpenAI `none` to `max`, Gemini `minimal` to `high`, Ollama `off`/`on`/`low`/`medium`/`high` for a model that thinks. `default` leaves it to the model. Kept per provider; only a turn's requests carry it, not the short side calls |
| `/update` | Ask PyPI whether a newer Aetheris is out, and install it if you say yes - with pip, pipx or uv, whichever installed it. A git checkout is told to `git pull` instead |
| `/clear` | Clear the terminal display and reset conversation history |
| `/sessions [title]` | List saved sessions, newest first, numbered - and open one: answer with its number or a few letters of its title, or press Enter to stay. Letters that fit several narrow the list and ask again |
| `/load <id or title>` | Open a saved session directly, found by id or title. On its own it is `/sessions` |
| `/title` | Show the current session's title and id |
| `/title <name>` | Retitle the current session and rename its file to match |
| `/export [filename]` | Export current chat history into a Markdown file |
| `/rewind [n]` | Take back the last prompt and everything said and done after it - or the last `n` - and put that prompt back in the box to edit or send again. Files stay as they are: it lists the AI commits those turns left, for `/undo`. It reaches the prompts sent since the conversation was opened, up to 20 |
| `/btw <question>` | A side question: answered from the conversation so far, with no tool run and nothing kept, so the next turn never sees it. The request is the conversation as it stands plus the question, so a provider's prompt cache still applies |
| `/copy [n] [file]` | Copy the latest answer - or the `n`th latest - as it was written, without the frame a code block is drawn in. An answer with code blocks asks which: the whole of it, or one block. With a file name it is written there instead, and never over an existing file |
| `/system <prompt>` | Set a custom system persona or reset to default (`/system reset`) |
| `/artifacts [open] [n]` | What an Attacca agent made in this session: shown here, or opened in the browser |
| `/skills` | List discovered skills with their descriptions and paths |
| `/skills reload` | Rescan the skill directories and refresh the system prompt |
| `/skill <name>` | Load a skill into the current conversation by hand |
| `/mcp` | Show every configured MCP server, its state, and what it exposes |
| `/mcp tools [server]` | Expand the tool list of one or every connected server |
| `/mcp resources [server]` | List the resources the servers expose |
| `/mcp reload` | Re-read the config files and reconnect every server |
| `/mcp connect <name>` | Reconnect a single server |
| `/mcp prompt <server> <name> [k=v]` | Run a prompt template the server offers |
| `/mcp <on/off>` | Attach or detach every MCP server for this session |

Two prefixes act on the message itself rather than being commands:

| Prefix | Description |
| :--- | :--- |
| `@<path>` | Attach a file (or a directory's listing) to this message. Typing `@` opens a completion menu of the current directory - arrows to move, Tab to insert |
| `!<command>` | Run a shell command yourself. It skips the approval prompt, because you typed it, and its output is added to the conversation - unless `RETURN_SHELL_OUTPUT` is off |
| `/connect [provider] [model]` | Connect a provider, or pick one interactively |
| `/connect attacca` | Pair this machine with Attacca by a code (once), then pick an agent |
| `/connect status` | Show every provider and whether it is usable |
| `/connect forget <provider>` | Delete the API key saved for a provider. An environment variable is left alone, and said so |
| `/perms` | Show the active tool permission rules |
| `/perms reload` | Re-read the permission rule files |
| `/perms allow <rule>` | Add an allow rule, e.g. `/perms allow run_cmd(git *)` |
| `/perms deny <rule>` | Add a deny rule |
| `/agents` | Show the other harnesses running in this project, what they hold, and what has been said |
| `/agents say <text>` | Say something to all of them yourself |
| `/agents release <path>` | Take a file back from the agent holding it |
| `/agents [on/off]` | Whether this session appears on the board at all |
| `/remote [on/off]` | Whether this session can be driven from a browser. On its own it says which, and reprints the link |
| `/remote on lan` | Open it to this machine's network rather than to this machine only |
| `/remote qr` | The link as a QR code, for pointing a phone at |
| `/remote forget` | Drop every browser that has paired; the link still works |
| `/vm` | Show the `run_python` scratch process: whether it is up, what it has run, and the directory it runs in |
| `/vm reset` | Throw away every variable the model left in it |
| `/vm stop` | End the process; the next `run_python` starts a fresh one |
| `/set` | Every setting that can be changed, and which ones you have changed |
| `/set <NAME> <value>` | Change one, e.g. `/set NUM_CTX 32768`. Saved for next time |
| `/set <NAME> default` | Put it back to what `config.py` says |
| `/set <SWITCH> on/off` | The on/off switches are settings like any other: `DEEPTHINK`, `PLANMODE`, `AUTO_ALLOW`, `AUTO_VERIFY`, `GIT_AUTO_COMMIT`, `AUTO_TITLE`, `SHOW_THINKING`, `RETURN_ALL_FILE_CONTENT`, `SAVE_CHAT_HISTORY` - saved for next time. `/set` on its own shows where each stands |

The nine commands those switches used to be - `/deepthink`, `/planmode`, `/automode`, `/autoverify`, `/autocommit`, `/autotitle`, `/think`, `/fullcontent` and `/record` - still answer, as they always did: for this session only, and each says once that `/set` is the way that lasts. They are no longer in `/help` or the menu.

| `/undo` | Take back the last file change the AI committed |
| `/tdd <request>` | Run one request with this project's test files locked |
| `/tdd` | Arm that for your next message |
| `/tdd off` | Lift it without sending anything |
| `/exit` or `/quit` | Exit the application |
| `Ctrl+C` | Stop what is running: a reply being written, a tool, a `!command`. The conversation keeps what was said and done up to then, and the prompt comes back. Pressed again within a second it exits - once is never enough to close the program, wherever it is pressed. How to leave is said on the prompt's own row, beside what is being typed, rather than above it |
| `Ctrl+D` | Exit at once |
| `Ctrl+O` | At the prompt: the last turn again, uncut. A tool call's arguments are cut to 120 characters as they scroll past and its result to 600, and thinking is not shown unless `SHOW_THINKING` is on; this shows all of it, and puts back whatever was half-typed |
| Mouse drag | Select and copy what is above the prompt: a thought without its `│`, an answer without what is under it. The highlight flashes and `⧉ copied` appears beside the prompt |
| Right click | Paste. The terminal's own right-click paste stops while the mouse is the program's, so the program does it |
| Mouse wheel | Scroll back, as always - see below. With the button held, it scrolls the drag instead: the selection runs on into what had scrolled off the top |
| Drag held at the top edge | Scrolls back a row at a time, selecting as it goes; at the bottom edge, once scrolled back, it comes down again. Letting go copies and puts the screen back |
| `Shift` + drag | The terminal's own selection, as it was |

**Important:** a terminal reports all of the mouse or none of it, so while
`MOUSE_SELECT` is on and the prompt is open the wheel is reported too. The
first turn of it therefore hands the mouse back to the terminal: that one
notch does nothing, every one after it scrolls as usual, and the prompt says
`scrolling - any key takes the mouse back`. Until a key is pressed a drag is
the terminal's own selection again. `/set MOUSE_SELECT off` gives the mouse
back for good. None of this applies while a drag is held: then the wheel is
the drag's, and scrolls what is drawn above the prompt from the program's own
record of it - the last 5,000 rows.
