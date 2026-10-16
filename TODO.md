# TODO
> For ai harness

## list
* [x] Upgrade system prompt
* [x] Enhance file write (almost file_edit?)
* [x] When use url_get, do not return raw html
* [x] Memory
* [x] Get user's input.(Choose from a list of options - multiple questions per call)
* [x] Skills (SKILL.md instruction packs, loaded on demand)
* [x] Session titles (AI-generated or set by hand with /title)
* [x] MCP servers (.mcp.json, stdio/http/sse, tools + resources + prompts)
* [x] Hide <tool_call> when the model talks before calling a tool
* [x] Reasoning models: keep <think> off the screen and out of the history
* [x] Tool permissions (.permissions.json allow/deny, 'a' at the prompt)
* [x] Several tool calls in one turn
* [x] Repair console input that arrives as surrogate escapes (Korean input on a cp949 console)
* [x] Raw <content> blocks so a file body never has to be escaped into JSON
* [x] Repair malformed tool JSON (unclosed brackets, flattened arguments, broken payload)
* [x] Ask the model to resend a tool call we could not parse, instead of ending the turn
* [x] run_cmd: no stdin + timeout, so an interactive program can no longer freeze the app
* [x] Live shell sessions: run_cmd hands the prompt to the model, send_input answers it
* [x] Waiting-for-input detection per platform (/proc on Linux, CPU elsewhere) + tests/test_platform.py
* [x] Decode command output by what it actually is (UTF-8, else the console code page)
* [x] /connect: Anthropic, OpenAI (+compatible), Gemini alongside Ollama
* [x] Pin dependencies to the versions that were actually tested (+ requirements-lock.txt)
* [x] Atomic file writes, so a crash cannot empty a session, the memory or the saved API keys
* [x] Token estimate that knows Korean from English, and calibrates against what providers report
* [x] One tool table (toolspec.py): the prompt is rendered from it, dispatch binds through it, drift is a startup error
* [x] Sub-agents: spawn_agent hires a second model for one job and returns only its report
* [x] Answer instead of a blank turn when a reasoning model thinks and says nothing
* [x] Native function calling for Anthropic, OpenAI and Gemini - same tool table, 12KB less prompt
* [x] Native tool calling on Ollama too, decided per model from what the model says it supports
* [x] A git commit per AI edit and /undo to take it back, toggled with /autocommit
* [x] Deepthink: plan -> check -> build -> review the real diff -> verify, driven by the harness
* [x] Stop a model that keeps calling a tool it has just been refused
* [x] Auto-verify: run the project's own check after a turn that wrote a file, and hand a failure back to fix. Three tries, then it has to explain
* [x] Repair an anchor spelling that can only mean one thing; refuse the rest with the real lines attached
* [x] Three-character hashes, and the quoted line beats the hash when they disagree
* [x] An edit hands back the lines around it, so the next one needs no re-read
* [x] pytest runs with --showlocals: a failure arrives as the state that caused it
* [x] /tdd: lock this project's test files for one request, so "make the test pass" cannot mean "edit the test"
* [x] Announce a big MCP server by name instead of describing it; use_mcp_server sends its tools when the model asks
* [x] Remote control: `/remote on` opens one token-locked door into the running session - the transcript, the prompt, and the approvals, on a phone
* [x] Ollama: keep the model loaded (OLLAMA_KEEP_ALIVE), and temperature / seed when they are set
* [x] Retry 429 / 5xx / a dropped connection with backoff, never once the reply has started
* [x] move_file: git mv for a tracked file, one commit, one /undo
* [x] Ring the terminal bell when a long turn ends (TURN_BELL_SECONDS)
* [x] Headless: aetheris -p "..." with piped input, --auto-allow and --json
* [x] bench/run.py: the README tasks N times, graded, saved, --compare
* [x] search_in_file: .gitignore, binaries skipped, glob, context lines, ripgrep when present
* [x] find_files(pattern): glob over the project, .gitignore honoured, up to 200
* [x] Loop guard: the same call 3 times is warned, the 4th refused; a write in between resets it
* [x] update_todos: a checklist kept with the session, its open items shown before every request
* [x] The nine on/off commands are /set switches; the old names still answer, for the session
* [x] /help shows the six common commands; /help all shows every one
* [x] README: a 150-line front page; the rest in docs/, one file per subject
* [x] Classifiers: 3.14, and Linux/macOS/Windows instead of OS Independent
* [x] .github/check_dist.py: wheel, sdist and classifiers checked in CI; the sdist runs the suite
* [x] py.typed, and a library: MCPServer, start_shell in aetheris.__all__
* [x] /rewind: the conversation before the last prompt, and that prompt back in the box
* [x] /btw: a side question answered from the conversation and not kept
* [x] /copy: an answer or a code block of it, without the frame, to the clipboard or a file
* [x] /context: a window per model - the model's own where it reports one, or set by hand
* [x] /effort: reasoning effort per provider (Anthropic, OpenAI, Gemini, Ollama think)
* [x] read_file start_line/end_line; a long file is cut between lines, not inside one
* [x] Independent read-only calls may share a reply
* [x] Deepthink stage 1 names all six stages
* [x] Ctrl+O: the last turn uncut

## next

* [ ] Whole-project Tree-sitter index (symbol table) instead of plain text search
* [ ] Split a part out to its own PyPI package only when someone asks for it - `mcp_client` first (the library in `aetheris.__all__` comes before any split)
