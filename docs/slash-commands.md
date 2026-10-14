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
| `/clear` | Clear the terminal display and reset conversation history |
| `/sessions [title]` | List saved sessions, newest first, numbered - and open one: answer with its number or a few letters of its title, or press Enter to stay. Letters that fit several narrow the list and ask again |
| `/load <id or title>` | Open a saved session directly, found by id or title. On its own it is `/sessions` |
| `/title` | Show the current session's title and id |
| `/title <name>` | Retitle the current session and rename its file to match |
| `/export [filename]` | Export current chat history into a Markdown file |
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
| `Ctrl+C` | Stop what is running: a reply being written, a tool, a `!command`. The conversation keeps what was said and done up to then, and the prompt comes back. Pressed again within five seconds it exits - once is never enough to close the program, wherever it is pressed |
| `Ctrl+D` | Exit at once |
| Mouse drag | Select and copy what is above the prompt: a thought without its `│`, an answer without what is under it. The highlight flashes and `⧉ copied` appears beside the prompt |
| Right click | Paste. The terminal's own right-click paste stops while the mouse is the program's, so the program does it |
| Mouse wheel | Scroll back, as always - see below |
| `Shift` + drag | The terminal's own selection, as it was |

**Important:** a terminal reports all of the mouse or none of it, so while
`MOUSE_SELECT` is on and the prompt is open the wheel is reported too. The
first turn of it therefore hands the mouse back to the terminal: that one
notch does nothing, every one after it scrolls as usual, and the prompt says
`scrolling - any key takes the mouse back`. Until a key is pressed a drag is
the terminal's own selection again. `/set MOUSE_SELECT off` gives the mouse
back for good.
