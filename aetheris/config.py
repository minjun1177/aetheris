# Only what this file itself uses. Everything else moved out with the code that
# needed it, and leaving the imports behind was not free: `bs4`, `requests`,
# `psutil` and `duckduckgo_search` were imported here unguarded, so any one of
# them missing stopped the whole program from starting - `config` is the base of
# the import graph and builds SYSTEM_PROMPT at import time (ARCHITECTURE 5.2).
# A machine with no `bs4` could not open a chat window, let alone search.
import json
import os
import platform
import re
import shutil

from aetheris import atomic
from aetheris import paths

# `Parser`, `Query` and `QueryCursor` look unused here and are not: `tools.py`
# reaches them as `config.Parser` and friends, so that the optional-dependency
# guard lives in exactly one place. Do not delete them.
try:
    from tree_sitter import Language, Parser, Query, QueryCursor
    import tree_sitter_python as _ts_python
    import tree_sitter_javascript as _ts_javascript
    import tree_sitter_typescript as _ts_typescript
    import tree_sitter_java as _ts_java
    import tree_sitter_c as _ts_c
    import tree_sitter_cpp as _ts_cpp
    import tree_sitter_go as _ts_go
    import tree_sitter_rust as _ts_rust
    import tree_sitter_c_sharp as _ts_csharp
    TREE_SITTER_AVAILABLE = True

    _TS_LANGUAGES = {
        "python":     Language(_ts_python.language()),
        "javascript": Language(_ts_javascript.language()),
        "typescript":  Language(_ts_typescript.language_typescript()),
        "tsx":         Language(_ts_typescript.language_tsx()),
        "java":       Language(_ts_java.language()),
        "c":          Language(_ts_c.language()),
        "cpp":        Language(_ts_cpp.language()),
        "go":         Language(_ts_go.language()),
        "rust":       Language(_ts_rust.language()),
        "csharp":     Language(_ts_csharp.language()),
    }

    _EXT_TO_LANG = {
        ".py": "python", ".pyw": "python",
        ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
        ".ts": "typescript", ".tsx": "tsx",
        ".java": "java",
        ".c": "c", ".h": "c",
        ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hxx": "cpp",
        ".go": "go",
        ".rs": "rust",
        ".cs": "csharp",
    }
except ImportError:
    TREE_SITTER_AVAILABLE = False
    _TS_LANGUAGES = {}
    _EXT_TO_LANG = {}

# Re-exported the same way: `app.py` reads `config.PromptSession`,
# `config.FileHistory` and `config.ANSI` behind `PROMPT_TOOLKIT_AVAILABLE`.
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.formatted_text import ANSI
    # What lets another agent's message be printed *above* a prompt that is
    # already waiting for a line, instead of on top of what is being typed.
    from prompt_toolkit.patch_stdout import patch_stdout
    # ...and what lets the prompt be told where to draw, so its own
    # rendering can be kept out of what the remote mirrors.
    from prompt_toolkit.output.defaults import create_output
    PROMPT_TOOLKIT_AVAILABLE = True

    from prompt_toolkit.completion import merge_completers

    class SlashCommandCompleter(Completer):
        """The commands, with what each one does shown beside it.

        It used to offer bare names and nothing else: a menu of forty words
        with no hint of what any of them did, and it stopped the moment a space
        was typed - so `/mcp ` and `/set ` , where the thing you cannot
        remember is exactly what comes next, offered nothing at all.

        What to suggest is not decided here. `suggest(text)` is handed the line
        so far and answers with (what to insert, what to show, what it does),
        which keeps the command table in `tui` where `/help` reads it too.
        """

        def __init__(self, suggest):
            self.suggest = suggest

        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            if not text.startswith("/") or "\n" in text:
                return
            try:
                rows = self.suggest(text)
            except Exception:
                return          # a menu is a convenience; it never stops typing
            for insert, shown, meta in rows:
                yield Completion(insert, start_position=-len(text),
                                 display=shown, display_meta=meta)

    # `@` at the start of a word opens this, and it lists what is actually in
    # the directory being typed - so the path is picked from disk rather than
    # remembered and mistyped. Arrow keys move through the menu and Tab inserts;
    # both come from prompt_toolkit once the completions are yielded.
    _AT_WORD = re.compile(r'(?:^|(?<=\s))@("[^"]*|\'[^\']*|[^\s]*)$')

    class PathMentionCompleter(Completer):
        """Files and directories for the `@` mention under the cursor.

        A directory completes with its separator still attached and no space
        after it, which is what makes typing straight on through `@src/` work:
        the next keystroke re-opens this against the directory just entered.
        That separator is `/` on every platform, so the completion reads back
        the way the path was typed.
        """

        def get_completions(self, document, complete_event):
            match = _AT_WORD.search(document.text_before_cursor)
            if not match:
                return
            typed = match.group(1).lstrip("\"'")
            directory, prefix = os.path.split(typed)
            base = os.path.expanduser(directory) if directory else "."
            try:
                names = sorted(os.listdir(base), key=str.lower)
            except OSError:
                return                      # half-typed directory: nothing to offer

            for name in names:
                # Dotfiles stay out of the way until the dot is typed, which is
                # the difference between a useful menu and 40 lines of .git.
                if name.startswith(".") and not prefix.startswith("."):
                    continue
                if not name.lower().startswith(prefix.lower()):
                    continue
                is_dir = os.path.isdir(os.path.join(base, name))
                # Joined with a forward slash rather than os.sep: the text here
                # replaces what the user typed, and they typed `src/`. Windows
                # opens either separator, so the one that matches the line wins.
                full = f"{directory}/{name}" if directory else name
                if is_dir:
                    full += "/"
                yield Completion(
                    full,
                    start_position=-len(typed),
                    display=name + ("/" if is_dir else ""),
                    display_meta="dir" if is_dir else _entry_size(os.path.join(base, name)),
                )

    # `!` at the front means the line is a command for this machine rather than
    # a message for the model - two very different things typed into one box.
    # It used to look identical to a message until Enter, and the first sign
    # that a line had run as a shell command was the shell command running.
    from prompt_toolkit.application import get_app
    from prompt_toolkit.lexers import Lexer

    SHELL_PREFIX = "!"

    def typing_shell() -> bool:
        """Whether the line being typed now is a `!` command."""
        try:
            return get_app().current_buffer.text.lstrip().startswith(SHELL_PREFIX)
        except Exception:
            return False        # no application running: it is not being typed

    from prompt_toolkit.filters import Condition

    def _wants_the_menu() -> bool:
        """Whether the line being typed is one the completion menu speaks to.

        `complete_while_typing` is what makes `/` and `@` open their menus
        without anybody pressing Tab, and it is also what makes prompt_toolkit
        keep `reserve_space_for_menu` rows free *under the prompt at all times*
        - eight blank lines below the cursor, whether or not a menu is coming,
        for the whole time you are sitting there typing an ordinary sentence.

        The reservation is read on every render, so it can be earned rather
        than held: this says yes only for a line that starts with `/` or has an
        `@` word in it, which is exactly when a menu is about to appear. Tab
        still completes anything, any time - that is a different path.
        """
        try:
            text = get_app().current_buffer.document.text_before_cursor
        except Exception:
            return False
        return text.lstrip().startswith("/") or bool(_AT_WORD.search(text))

    COMPLETE_WHILE_TYPING = Condition(_wants_the_menu)

    class ShellLineLexer(Lexer):
        """Colour the whole line while it is a shell command.

        A lexer rather than anything cleverer because prompt_toolkit already
        re-runs it on every keystroke, so the colour appears the moment the
        `!` is typed and goes the moment it is deleted.
        """

        def __init__(self, style: str):
            self.style = style

        def lex_document(self, document):
            shell = document.text.lstrip().startswith(SHELL_PREFIX)
            style = self.style if shell else ""

            def get_line(number):
                return [(style, document.lines[number])]

            return get_line

    def _entry_size(path) -> str:
        try:
            size = os.path.getsize(path)
        except OSError:
            return ""
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
            size /= 1024
        return ""
except ImportError:
    PROMPT_TOOLKIT_AVAILABLE = False

# `smrp` and `ttlp` are read as `config.smrp` / `config.ttlp` by the summariser
# and the session titler.
from aetheris.systemprompt import systemprompt as syp
from aetheris.systemprompt import summarizeprompt as smrp
from aetheris.systemprompt import titleprompt as ttlp


MODEL = "gemma4:e4b"

# Where the local Ollama daemon is. It was `localhost:11434` with no way to say
# otherwise: an Ollama on another machine, on another port, or behind a tunnel
# could only be reached by editing this file. `/set OLLAMA_HOST http://box:11434`
# is what that should have been all along.
#
# `$OLLAMA_HOST` still wins while this is at its default, because that is the
# variable the Ollama tools themselves read and it would be rude to ignore it;
# changing this setting is an explicit instruction and wins over both.
OLLAMA_HOST = "http://localhost:11434"

CURRENT_OS = platform.system()

SYSTEM_PROMPT = syp()

FILE_MAX_DISPLAY_LENGTH = 1000

# Under ~/.aetheris, not the working directory - see paths.py for why. Both
# stay plain settings, so a caller that wants them somewhere else still can.
MEMORY_FILE = paths.state("memory.json")
SESSION_DIR = paths.state("sessions")
HISTORY_FILE = paths.state("history")

# A memory written with `important` is put into the system prompt at the start
# of every session rather than waiting to be looked up (`session
# .memory_prompt_section`). The prompt is paid for on every turn, so both the
# number of them and the length of each are capped: a model that marks forty
# things important should not spend a tenth of the context window saying so.
MEMORY_IMPORTANT_MAX = 20
MEMORY_IMPORTANT_CHARS = 600

# Notes are the other half of remembering: markdown about one project rather
# than facts about the person, one file per note under `NOTES_DIR/<project>/`.
# Only the titles reach the prompt (`notes.notes_prompt_section`), so the cap
# here is on how many lines that list may run to - the bodies cost nothing
# until `read_note` asks for one.
# An image rides on a message as a path and is encoded on the way out
# (`images.py`). The long edge is what the hosted providers scale to anyway, so
# resizing here costs nothing and stops a phone photograph from being refused
# by the API instead of by the harness. The byte ceiling is the strictest of
# the four (Anthropic's 5MB), applied after any resize.
IMAGE_MAX_EDGE = 1568
IMAGE_MAX_BYTES = 5 * 1024 * 1024
# Images per message. A model that has been handed nine screenshots has not
# been helped, and each one is prompt paid for on every turn of the tool loop.
IMAGE_MAX_PER_MESSAGE = 4

NOTES_DIR = paths.state("notes")
NOTES_TITLES_MAX = 40
# A note comes back into the conversation whole, so the ceiling is the point at
# which one note stops being something a small model can take in at once.
NOTE_MAX_CHARS = 20000

# Sessions are filed under a human title instead of a timestamp. AUTO_TITLE lets
# the model name a new session after its first exchange; /title overrides it.
AUTO_TITLE = True
SESSION_TITLE = ""
SESSION_TITLE_MAX_LEN = 60
SESSION_SLUG_MAX_LEN = 48

# The two markers a tool result may begin with, and the only two the harness
# reads back out of one. `[Error]` means the tool ran and failed; `[System]`
# means it was refused before it ran - a deny rule, a read-only stage, an
# approval the user declined. Anything else is a result.
#
# They are anchors, never searched for: a page fetched by `get_url`, a file read
# by `read_file` or the output of a `grep` may contain either string as ordinary
# content, and treating that as a failure marks good output bad. They live here
# because three modules need them and none can import the others - `tools`
# writes them, `llm_client` counts them, `tui` colours them.
TOOL_ERROR_PREFIX = "[Error]"
TOOL_REFUSAL_PREFIX = "[System]"

# A single `@path` can name a file of any size, and the context window is the
# scarce thing. Cut at this many characters with a line saying so, rather than
# letting one mention crowd out the conversation it was meant to inform.
MENTION_MAX_CHARS = 40000

SEARCH_MAX_RESULTS = 5

# Point this at a self-hosted SearXNG (e.g. "http://localhost:8080") to make
# candidate generation fully local. Empty = use the keyless public sources only.
SEARXNG_URL = ""

SEARCH_CANDIDATES = 10          # results requested per source
SEARCH_FETCH_PAGES = 8          # pages actually downloaded and read
SEARCH_PAGE_CHARS = 60000       # per-page text kept for ranking
SEARCH_PASSAGE_CHARS = 900      # passage size, and the snippet size returned
SEARCH_RESULT_CHARS = 6000      # ceiling on the whole tool result
SEARCH_SOURCE_TIMEOUT = 12
SEARCH_FETCH_TIMEOUT = 10
SEARCH_TOTAL_TIMEOUT = 25

NUM_CTX = 32768*2
NUM_PREDICT = 6144          # 2048 and 4096 both truncated file writes mid-way.
                            # Keep this an int: every hosted API rejects a float
                            # in max_tokens, and 4096*1.5 is a float.

AUTO_ALLOW = False
RETURN_ALL_FILE_CONTENT = True
SAVE_CHAT_HISTORY = True
CUSTOM_PERSONA = ""

# run_cmd stays connected to the command instead of waiting for it to finish.
# When the output goes quiet for CMD_IDLE_TIMEOUT the process is most likely
# sitting at a prompt, so what it printed is handed to the model, which answers
# with send_input. It never gets the user's terminal: its prompts are captured,
# so holding the terminal only froze the app with nothing on screen.
CMD_IDLE_TIMEOUT = 0.6      # silence this long is worth looking at
CMD_WAIT_TIMEOUT = 8        # ...but silence only counts as a prompt after this,
                            # unless the output looks like one or /proc confirms it
CMD_IDLE_GRACE = 2.5        # Windows/macOS, where /proc cannot be asked: how long
                            # silence with no CPU burned has to last instead
CMD_TIMEOUT = 120           # ceiling on any single read from a command
CMD_OUTPUT_CHARS = 6000     # per-read output ceiling; the newest is kept
CMD_MAX_SESSIONS = 3        # live commands kept at once; the oldest is dropped
CMD_SESSION_LIFETIME = 900  # seconds a live command may sit idle before it is killed

# run_python: one Python process kept alive beside the harness, so the model can
# work something out - a calculation, a regex, what a function really returns -
# instead of guessing at it, and keep what it defined for the next call. See
# vm.py. It is isolation from mistakes, not from a hostile program: the code
# runs as you, and that is why it still asks before running.
VM_TIMEOUT = 20             # wall clock per call; over it the VM is killed and
                            # restarted, and the model is told its variables are gone
VM_OUTPUT_CHARS = 4000      # ceiling on printed output; the middle is dropped
VM_MEMORY_MB = 512          # address space the code may take (POSIX only; 0 = no limit)
VM_FILE_MB = 64             # largest file the code may write (POSIX only; 0 = no limit)

MAX_TOOL_CALLS = 10

# Sub-agents (spawn_agent). A sub-agent gets a hard turn budget instead of the
# assistant's "shall I keep going?" prompt - the point of delegating is not
# being asked about it. Depth 1 means sub-agents cannot hire sub-agents, which
# is the only setting with a bounded cost.
# Hosted providers get the tool list through their own function-calling
# interface instead of the <tool_call> text protocol. It is more accurate, and
# it is about 12KB less *prompt text* - but not less prompt, which is what
# costs: measured against gemma4:e4b the two come to 6,013 and 5,958 tokens, a
# difference of 55. The schemas move out of the system prompt and into the
# request's own `tools` field, and they are tokenised either way. Choose this
# for accuracy, not to save context. Set False to force text everywhere, e.g.
# against an OpenAI-compatible server that has no tool support.
NATIVE_TOOLS = True

# Commit each file an AI tool changes, on its own, so /undo can take it back.
# Only the paths the tool named are committed - whatever else you have staged
# or changed is left alone. Toggle it in a session with /autocommit.
GIT_AUTO_COMMIT = True

# Auto-verify: after a turn changes a file, run the project's own check and put
# a failure in front of the model instead of hoping it thinks to check itself.
# Only a check the project already declares is ever run - see verify.py, which
# also explains why a slow one turns itself off. Toggle it with /autoverify.
AUTO_VERIFY = True
VERIFY_TIMEOUT = 90         # seconds one check gets before it is killed and disabled
VERIFY_OUTPUT_CHARS = 2000  # of a failure, the tail - which is where it is written down

# /tdd locks this project's test files for one request, so "make this test
# pass" cannot be answered by editing the test. It lifts itself when the turn
# ends. The whole point is to stay in the loop longer than usual, so it gets a
# larger budget than the ordinary one in llm_client.
TDD_LOCK = False            # armed by /tdd for one turn; not a user setting
TDD_VERIFY_FAILURES = 6

SUBAGENT_MAX_TURNS = 12
SUBAGENT_MAX_DEPTH = 1
SUBAGENT_DEPTH = 0              # how deep we currently are; not a user setting

PLANMODE = False

# Deepthink: one request becomes plan -> check -> build -> review -> revise ->
# verify, driven by the harness rather than left to the model to remember. Off
# by default - it costs six turns where one would often do. Toggle with
# /deepthink. See deepthink.py.
DEEPTHINK = False
DEEPTHINK_READONLY = False      # set per stage by deepthink.py; not a user setting
# A pass whose final check says the work is still not done starts the chain
# again from the plan. This is the ceiling on that: a model that is never
# satisfied would otherwise never stop, and three passes is already 18 turns.
DEEPTHINK_MAX_PASSES = 3

LOADED_SKILLS = []

# --- secrets -----------------------------------------------------------------
# A `.env` is the one file whose contents are the secret, and a value the model
# reads does not stay read: it goes to the provider and into the session file
# on disk. So the harness reads it, hands the model `{{env:NAME}}`, and puts
# the real value back when the model uses that placeholder in something that
# runs. See vault.py, which also says plainly what this does not cover.
SECRET_REDACT = True
SECRET_MIN_LENGTH = 8       # below this a "secret" is a word like `dev`, and
                            # hiding it would rewrite every tool result
SECRET_FILES = []           # extra filenames to treat the same way

# --- tool permissions --------------------------------------------------------
# Rules live in .permissions.json / ~/.aetheris/permissions.json. Empty rules
# behave exactly as before: the approval prompt decides.
PERMISSIONS_ENABLED = True
POLICY_AUTO_ALLOW = False       # set per call by dispatch_tool; not a user setting

# --- the agent channel -------------------------------------------------------
# Several harnesses run in one project at once, and without this none of them
# knows the others exist - so two of them edit the same file and the second
# write throws the first away. The board lives in ~/.aetheris/channel/, one
# file per workspace. See channel.py; /agents shows it.
# A big MCP server is announced by name rather than described in full, and its
# tools' parameters arrive when the model calls `use_mcp_server`. One 24-tool
# server costs 3,549 tokens of prompt or 4,637 of native schema on *every*
# request; its name and its tools' names cost 130. Below MCP_LAZY_MIN_TOOLS the
# index would cost about what the schemas cost, so a small server is shown
# outright and no round trip is spent on it. False restores the old behaviour
# exactly. See mcp_client.
MCP_LAZY_TOOLS = True
MCP_LAZY_MIN_TOOLS = 6
LOADED_MCP_SERVERS = []     # what this conversation has asked for; not a setting

CHANNEL_ENABLED = True
CHANNEL_CLAIMS = True           # refuse an edit to a file another agent is holding
CHANNEL_CLAIM_TTL = 1800        # seconds a claim_files claim lasts
CHANNEL_WRITE_TTL = 300         # ...and one taken automatically by writing a file
CHANNEL_STALE = 120             # heartbeat age past which an agent is presumed gone
CHANNEL_POLL_SECONDS = 2        # how often an idle prompt looks for a new message

# A message addressed to an *idle* terminal starts a turn by itself: the channel
# presses Enter, because otherwise the answer waits for a person to come back to
# a keyboard and the agent that asked sits there for the afternoon. Only a
# direct message does it - a broadcast is news, and news needs no answer.
#
# `MAX` counts only the turns that came back with *nothing done* - see
# `channel.note_tool`. A turn that read a file, made a change or ran a test
# refills it, so two agents genuinely working can go back and forth all day;
# what runs out is the patience for an exchange that is only an exchange.
# **0 means no ceiling at all**, the same as `MAX_TOOL_CALLS`, and it is only a
# sane thing to set because of the counting above and because
# `CHANNEL_MAX_IDLE_REPLIES` refuses the loop from the other end too.
CHANNEL_AUTO_TURN = True
CHANNEL_AUTO_TURN_MAX = 3

# Messages one agent may send another with nothing done in between, before
# `send_agent_message` is refused. Two 4B models on one job spent the whole run
# asking each other to do it - "could you test it?" / "yes, could you test it?" -
# and recording that the other was handling it. Talking is one tool call and
# doing the job is twenty, so the loop is the cheap path and has to be closed
# rather than discouraged. 0 removes the limit.
CHANNEL_MAX_IDLE_REPLIES = 3

# The note every agent in a project shares as its plan. Not a field on the
# board: `notes.py` is already a per-project markdown store keyed by the same
# `channel.workspace()`, already shared by everybody here, and already
# announced in each of their system prompts. One answer to "what are we all
# doing", not two waiting to disagree. Empty turns the pointer off.
CHANNEL_PLAN_NOTE = "plan"

# --- remote control ----------------------------------------------------------
# `/remote on` opens one door into this session: a token-locked HTTP server on
# this machine, and anything that can open the URL it prints is at the prompt -
# it reads the transcript, types lines, and answers the approval prompts that
# would otherwise wait for somebody who has left the desk. Off by default, and
# loopback unless `/remote on lan` says otherwise, because it is a door into a
# shell. The token is made when the server starts and is deliberately not a
# setting: there is nothing here for `settings.json` to keep. See remote.py.
REMOTE_ENABLED = False
REMOTE_HOST = "127.0.0.1"       # `/remote on lan` binds every interface instead
REMOTE_PORT = 8765              # busy? the next 19 are tried before giving up
REMOTE_LINES = 500              # transcript lines kept for a phone to scroll
REMOTE_ASK_TIMEOUT = 300        # seconds a question waits before it counts as no
REMOTE_PAIR = "lan"             # when a browser must also type a code shown on
                                # this terminal before it can drive anything:
                                # "lan" (only when opened to the network),
                                # "always", or "never". The link crosses the
                                # network; the terminal does not, which is what
                                # makes the code a second factor rather than a
                                # second copy of the first.
REMOTE_MAX_BAD_TOKENS = 20      # wrong tokens from one address before it is shut out
REMOTE_LOCKOUT = 300            # ...and for how long. The first wrong one is reported
                                # at the prompt either way: on a network somebody
                                # else is on, that is the only warning there is.
# Changing the host or the port while a remote is open moves it there and prints
# the new link - a setting that takes effect only after something is turned off
# and on again is a setting that looks broken.

# --- reasoning ("thinking") models -------------------------------------------
# Reasoning models wrap their scratch work in <think> tags, or return it in
# Ollama's separate `thinking` field. It is never the answer, so by default it
# is neither shown nor kept in the conversation history.
SHOW_THINKING = False
STORE_THINKING = False

# --- MCP (Model Context Protocol) -------------------------------------------
# Servers are declared in ./.mcp.json (project) or ~/.aetheris/mcp.json (user).
MCP_ENABLED = True
MCP_STARTUP_TIMEOUT = 30        # seconds to wait for a server's initialize
MCP_CALL_TIMEOUT = 120          # seconds to wait for a tools/call result
MCP_HTTP_TIMEOUT = 60           # per-request timeout for http/sse transports
MCP_RESULT_CHARS = 8000         # ceiling on a single MCP result handed to the model
MCP_MAX_TOOLS_PER_SERVER = 40   # keeps one chatty server from flooding the prompt
MCP_TRUSTED_SERVERS = []        # server names whose calls skip the approval prompt
MCP_AUTO_APPROVE_READONLY = False  # trust a tool's own readOnlyHint annotation

# One entry per request sent to the model, each carrying the turn it belongs to.
#
# A turn is one thing the person asked for, and answering it is very often
# several requests: the first one, then one more after every tool result, six
# of them under deepthink, and a sub-agent's whole conversation on top. `/usage`
# used to draw a bar per request, so a single question that took four tool calls
# read as five separate things the person had asked - and the graph said the
# conversation was five times as long as it was. `turn` is what groups them back
# together; `context.token_turns()` is the only place that does the grouping.
token_history = []

turn_index = 0


def next_turn() -> int:
    """Begin a turn. Everything sent to the model until the next call is in it."""
    global turn_index
    turn_index += 1
    return turn_index


def resume_turns() -> None:
    """Carry on past the turns a loaded session already recorded.

    Without this a resumed conversation restarts at 1 and its first request
    lands in the same group as an old one, silently merging two questions that
    were asked days apart. A history from before this existed has no turn
    numbers at all, which reads as 0 and leaves the counter where it started.
    """
    global turn_index
    turn_index = max((entry.get("turn") or 0) for entry in token_history) \
        if token_history else 0


class S:
    R     = "\033[0m"
    BOLD  = "\033[1m"
    DIM   = "\033[2m"
    ITAL  = "\033[3m"

    ACCENT    = "\033[38;2;217;119;87m"
    USER_CLR  = "\033[38;2;184;187;38m"
    OK        = "\033[38;2;142;192;124m"
    WARN      = "\033[38;2;250;189;47m"
    ERR       = "\033[38;2;251;73;52m"
    INFO      = "\033[38;2;131;165;152m"
    GRAY      = "\033[38;2;146;131;116m"
    WHITE     = "\033[38;2;235;219;178m"
    MUTED     = "\033[38;2;102;92;84m"
    PURPLE    = "\033[38;2;211;134;155m"
    CYAN      = "\033[38;2;131;165;152m"


def tw() -> int:
    try:
        return shutil.get_terminal_size().columns
    except Exception:
        return 80


def th() -> int:
    """Terminal rows. The companion to `tw`, and needed for the same reason:
    anything printed taller than this scrolls off the top before it is read."""
    try:
        return shutil.get_terminal_size().lines
    except Exception:
        return 24


def _visible_len(text: str) -> int:
    return len(re.sub(r'\033\[[^m]*m', '', text))


_SURROGATE_PATTERN = re.compile(r'[\ud800-\udfff]')


def _console_encodings() -> list[str]:
    """What the console might actually be handing over, most likely first.

    UTF-8 is tried first on purpose: a legacy code page will happily decode
    almost any byte into mojibake, so guessing it before UTF-8 would quietly
    corrupt text that was fine.
    """
    candidates = ["utf-8"]
    if CURRENT_OS == "Windows":
        try:
            import ctypes
            for code_page in (ctypes.windll.kernel32.GetConsoleCP(),
                              ctypes.windll.kernel32.GetACP()):
                if code_page:
                    candidates.append(f"cp{code_page}")
        except Exception:
            pass
    try:
        import locale
        preferred = locale.getpreferredencoding(False)
        if preferred:
            candidates.append(preferred)
    except Exception:
        pass

    seen, ordered = set(), []
    for encoding in candidates:
        key = encoding.lower().replace("-", "")
        if key not in seen:
            seen.add(key)
            ordered.append(encoding)
    return ordered


def safe_text(text):
    """Repair console input that arrived as surrogate escapes.

    In Python's UTF-8 mode `sys.stdin` is read as UTF-8 with the
    `surrogateescape` error handler. On a Windows console that hands over cp949
    (or any other legacy code page) bytes, Korean input therefore comes back as
    lone surrogates. Those cannot be encoded again, so the moment such a string
    enters the conversation both `json.dump` and the request to Ollama raise
    `UnicodeEncodeError` - and every later turn fails too, because the bad
    string is still sitting in the history.

    The escaped bytes are not lost: encoding them back recovers the original
    bytes, which then decode properly.
    """
    if not isinstance(text, str) or not _SURROGATE_PATTERN.search(text):
        return text
    try:
        raw = text.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        return _SURROGATE_PATTERN.sub(" ", text)

    for encoding in _console_encodings():
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return _SURROGATE_PATTERN.sub(" ", text)


def repair_messages(messages: list[dict]) -> list[dict]:
    """Repair the conversation in place so it can always be encoded.

    Repairing rather than copying is deliberate: a broken string left in the
    history would fail on every later turn as well, and the conversation would
    stay stuck until it was cleared.
    """
    for message in messages:
        content = message.get("content", "")
        repaired = safe_text(content)
        if repaired is not content:
            message["content"] = repaired
    return messages


def _hr(char: str = "─", width: int = 0, style: str = S.MUTED) -> str:
    w = width or max(1, tw() - 6)
    return f"{style}{char * w}{S.R}"


# ---------------------------------------------------------------------------
# settings you can change without editing this file
# ---------------------------------------------------------------------------
#
# Everything above was a source edit away. That is fine for the person who
# wrote it and useless for anybody else: an installed copy's `config.py` is
# somewhere under site-packages, editing it is lost on the next upgrade, and
# "change NUM_CTX" should not mean "find and edit a Python file".
#
# `/set` writes the ones that were changed to `~/.aetheris/settings.json`, and
# they are applied over the defaults above at import. Only the deviations are
# recorded, so a default that improves in a later version still reaches anyone
# who never overrode it - writing all of them out would freeze this file's
# values forever the first time somebody changed one.
#
# What is settable is *derived*, not listed: an UPPER_CASE name here holding a
# bool, number, string or list is a setting. So a setting added above is
# settable the moment it exists, and there is no second table to drift out of
# step (invariant 5.1, applied to settings rather than tools). The exceptions
# are named instead, because they are far fewer and far more stable.

SETTINGS_FILE = paths.state("settings.json")

_NOT_A_SETTING = frozenset({
    # Facts about the machine, not choices.
    "CURRENT_OS", "PROMPT_TOOLKIT_AVAILABLE", "TREE_SITTER_AVAILABLE",
    # Built, or owned by a command of their own.
    "SYSTEM_PROMPT", "MODEL", "SESSION_TITLE", "CUSTOM_PERSONA",
    # Live state that happens to be spelled in capitals.
    "LOADED_SKILLS", "POLICY_AUTO_ALLOW", "DEEPTHINK_READONLY", "SUBAGENT_DEPTH",
    "TDD_LOCK", "LOADED_MCP_SERVERS",
    # Where the person's own files live. `AETHERIS_HOME` moves all of them
    # together; moving one by hand splits a memory or a session list in two.
    "MEMORY_FILE", "HISTORY_FILE", "SESSION_DIR", "NOTES_DIR",
    # Invariant 5.9: these two are anchors that `tools`, `llm_client` and `tui`
    # each test with `startswith`. They are a protocol, not a preference.
    "TOOL_ERROR_PREFIX", "TOOL_REFUSAL_PREFIX",
    # This machinery's own bookkeeping. Listing the settings file among the
    # settings would offer to move it with a command that writes to it.
    "SETTINGS_FILE", "SETTINGS_APPLIED",
})

_TRUE = ("on", "true", "yes", "y", "1")
_FALSE = ("off", "false", "no", "n", "0")


def settable() -> dict:
    """Every setting `/set` may change, with the value it has right now."""
    return {name: value for name, value in sorted(globals().items())
            if name.isupper() and not name.startswith("_")
            and name not in _NOT_A_SETTING
            and isinstance(value, (bool, int, float, str, list))}


def parse_setting(name: str, raw) -> tuple:
    """What was typed, in the type the setting already has. (value, problem).

    The current value is the schema. There is nothing to declare and nothing to
    keep in step: a number stays a number, `on` and `off` are the only spellings
    of a switch, and a list is written with commas.
    """
    current = globals().get(name)
    text = raw.strip() if isinstance(raw, str) else raw

    if isinstance(current, bool):           # before int - a bool *is* an int
        if isinstance(text, bool):
            return text, ""
        spelling = str(text).strip().lower()
        if spelling in _TRUE:
            return True, ""
        if spelling in _FALSE:
            return False, ""
        return None, f"{name} is on or off, not '{raw}'"

    if isinstance(current, (int, float)):
        try:
            value = int(str(text)) if isinstance(current, int) else float(str(text))
        except (TypeError, ValueError):
            kind = "a whole number" if isinstance(current, int) else "a number"
            return None, f"{name} is {kind}, not '{raw}'"
        # None of these mean anything below zero, and a negative one fails much
        # later and somewhere else - a timeout that never waits, a ceiling that
        # trims everything. Nothing above zero is second-guessed: this file was
        # always editable by hand and the same latitude belongs here.
        if value < 0:
            return None, f"{name} cannot be negative"
        return value, ""

    if isinstance(current, list):
        if isinstance(text, list):
            return list(text), ""
        return [part.strip() for part in str(text).split(",") if part.strip()], ""

    return str(text), ""


_saved: dict = {}           # what settings.json holds: only what was changed


def saved_settings() -> dict:
    """Only what was changed - which is all `settings.json` ever holds."""
    return dict(_saved)


def defaults() -> dict:
    """What this file says, before `settings.json` had a say."""
    return dict(_DEFAULTS)


def set_setting(name: str, raw) -> tuple:
    """Change a setting now and for the next session. Returns (ok, message)."""
    name = (name or "").strip().upper()
    if name not in settable():
        return False, (f"'{name}' is not a setting that can be changed here - "
                       "/set on its own lists the ones that can")

    if isinstance(raw, str) and raw.strip().lower() in ("default", "reset"):
        value = _DEFAULTS[name]
    else:
        value, problem = parse_setting(name, raw)
        if problem:
            return False, problem

    globals()[name] = value
    if value == _DEFAULTS.get(name):
        _saved.pop(name, None)      # back to the default: stop recording it
    else:
        _saved[name] = value

    try:
        paths.ensure_home()
        atomic.write_json(SETTINGS_FILE, _saved)
    except Exception as error:
        return True, (f"changed for this session only - {SETTINGS_FILE} could "
                      f"not be written ({error})")
    return True, SETTINGS_FILE


def load_saved_settings() -> list:
    """Apply `settings.json` over the defaults. Returns the names it changed.

    It fails open, one entry at a time: a setting this version no longer has,
    or a value of the wrong type, is skipped and the rest are applied. A file
    that cannot be read at all leaves every default exactly as written above -
    the harness must still start when its settings file is broken.
    """
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []

    applied = []
    allowed = settable()
    for name, value in data.items():
        name = str(name).strip().upper()
        if name not in allowed:
            continue
        parsed, problem = parse_setting(name, value)
        if problem:
            continue
        globals()[name] = parsed
        # Only a *deviation* is kept, which is the whole point of the file (see
        # the note above): re-recording a value that has since become the
        # default would pin it there and stop a later version's better default
        # from ever reaching anyone who once changed the setting and changed it
        # back through a version where the two happened to differ.
        if parsed == _DEFAULTS.get(name):
            _saved.pop(name, None)
        else:
            _saved[name] = parsed
        applied.append(name)
    return applied


# The defaults are whatever this file says, captured before the saved file gets
# a say - so "back to the default" means this file's value, not the last one
# that happened to be written down.
_DEFAULTS = settable()
SETTINGS_APPLIED = load_saved_settings()
