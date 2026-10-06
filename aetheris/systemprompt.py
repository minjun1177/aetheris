import platform
import os

from aetheris import toolspec

from aetheris.skills import skills_catalog_prompt
from aetheris.mcp_client import mcp_tools_prompt
from aetheris import vault

CURRENT_OS = platform.system()

def load_context_file() -> str:
    for filename in ["CONVENTIONS.md", ".clauderc", "AGENTS.md", "CLAUDE.md"]:
        if os.path.exists(filename):
            try:
                with open(filename, "r", encoding="utf-8") as f:
                    return f"\n\n### PROJECT CONVENTIONS:\n{f.read()}\n"
            except Exception:
                pass
    return ""

def summarizeprompt() -> str:
    return """
  Please summarize the core context, important facts, and ongoing tasks from the following conversation history. Keep it concise but comprehensive so that you can continue the work smoothly. Respond ONLY with the summary.\n\n
"""

def titleprompt() -> str:
    return """
  Read the conversation below and write a short title for it, in the language the user writes in.
  Rules: 2~6 words, no quotes, no trailing punctuation, no prefix like "Title:". Describe the topic, not the roles.
  Respond ONLY with the title itself on a single line.


"""

# The handful of rules that describe *how* a tool is called, rather than how to
# behave. Everything else in the rules applies to both protocols, so it is
# written once and shared - see `tool_rules`.
_TEXT_PROTOCOL = """2. If you need to use a tool to get more information, you MUST use the following exact format:
   <tool_call>
   {"name": "tool_name", "arguments": {"parameter_name": "value"}}
   </tool_call>
3. When a parameter carries file contents, code, or any other long text, do NOT put it in the JSON. Send it as a raw block after the JSON instead:
   <tool_call>
   {"name": "write_file", "arguments": {"filepath": "game.py"}}
   <content>
   import random

   print("Guess the number!")
   </content>
   </tool_call>
   Inside a raw block write the text EXACTLY as it must appear in the file: real line breaks, real quotes, real backslashes, no escaping of any kind. This is the reliable way to write a file - escaping a whole file into a JSON string goes wrong far too easily.
   The block tags are <content> for write_file and run_python, <old_content> / <new_content> for edit_file, and <stdin> for run_cmd, send_input and run_python. Everything else (the file path, the command, flags, short values) stays in the JSON.
   To try out a program you just wrote, run it and then answer its prompts one at a time. run_cmd returns what it printed plus a session id, you read the prompt, and send_input answers it:
   <tool_call>
   {"name": "run_cmd", "arguments": {"command": "python3 game.py"}}
   </tool_call>
   ... the result shows the program's output and [Waiting] with session "s1" ...
   <tool_call>
   {"name": "send_input", "arguments": {"session": "s1"}}
   <stdin>
   50
   </stdin>
   </tool_call>
   Answer what the program actually asked, based on the output you just read. Keep going until it exits, then call end_process if it is still running."""

_NATIVE_PROTOCOL = """2. When you need a tool, call it through the tool interface supplied with this request. Do not describe a call in your reply text, and never use a tool that was not supplied.
3. Long values - a whole file body, a block of code, several lines of input - go straight into the tool's own parameter, written exactly as they should appear. Nothing needs escaping by hand; the interface carries the text as it is.
   To try out a program you just wrote, run it and answer its prompts one at a time. run_cmd returns what the program printed plus a session id, you read the prompt, and send_input answers it. Answer what the program actually asked, based on the output you just read. Keep going until it exits, then call end_process if it is still running."""

_TEXT_ANCHOR_EXAMPLE = """
      <tool_call>
      {"name": "edit_file", "arguments": {"filepath": "game.py"}}
      <new_content>
      50:1fa|    print("the answer was", answer)
      </new_content>
      </tool_call>

  There is no <old_content> block at all. To change three lines, send three
  such rows. To replace lines 50-53 with a different NUMBER of lines, or to
  delete them, name them in <old_content> instead:

      <tool_call>
      {"name": "edit_file", "arguments": {"filepath": "game.py"}}
      <old_content>
      50:1fa-53:9c0
      </old_content>
      <new_content>
          print("the answer was", answer)
      </new_content>
      </tool_call>
"""

_NATIVE_ANCHOR_EXAMPLE = """
      edit_file(filepath="game.py",
                new_content='50:1fa|    print("the answer was", answer)')

  There is no old_content at all. To change three lines, send three such rows,
  newline-separated. To replace lines 50-53 with a different NUMBER of lines,
  or to delete them, name them in old_content instead:

      edit_file(filepath="game.py",
                old_content="50:1fa-53:9c0",
                new_content='    print("the answer was", answer)')
"""

_TEXT_DONT_FIRST = """1. Do NOT add any conversational text before or after the <tool_call> tag if you are calling a tool. Just output the tag and the JSON inside it."""

_NATIVE_DONT_FIRST = """1. Do NOT write a tool call out as text. Text that looks like a call is not one - use the tool interface."""

_TEXT_IMPORTANT_FIRST = """1.  IMPORTANT: When outputting Windows file paths in JSON, you MUST escape backslashes like this: "C:\\\\folder\\\\file.txt"."""

_NATIVE_IMPORTANT_FIRST = """1.  IMPORTANT: Write Windows file paths as they really are, e.g. C:\\folder\\file.txt. Nothing needs doubling by hand."""

_TEXT_WORK_IT_OUT = """17. Work it out instead of guessing at it. When an answer depends on a number, a date, a regex actually tried against real strings, or what a function really returns, run it in `run_python` and read the result - the code goes in a <content> block, and everything it defines is still there on your next call, so you can check one step at a time. That is a scratch process, not your project: to run a file you have written, use `run_cmd`."""

_NATIVE_WORK_IT_OUT = """17. Work it out instead of guessing at it. When an answer depends on a number, a date, a regex actually tried against real strings, or what a function really returns, run it in `run_python` and read the result - everything it defines is still there on your next call, so you can check one step at a time. That is a scratch process, not your project: to run a file you have written, use `run_cmd`."""

_TEXT_IMPORTANT_LAST = """18. Tool call JSON must be ONE object: {"name": "<tool>", "arguments": {...}}. Put EVERY parameter inside "arguments" - never beside "name". Keep that object short: file contents, code and any other long text belong in a raw block after it, as DO rule 3 shows, never escaped into a JSON string. Before you finish, count the closing braces: the object ends with }}."""

_NATIVE_IMPORTANT_LAST = """18. Pass every argument in the tool call itself, and only the parameters that tool actually lists. Your reply text is for the user, not for the arguments."""


def native_tools_active() -> bool:
    """Whether this turn's provider takes tool schemas over its own API.

    Imported late: `providers` reads `config`, which builds a prompt at import
    time, and asking at module level would close that circle.
    """
    from aetheris import config
    if not getattr(config, "NATIVE_TOOLS", True):
        return False
    try:
        from aetheris import providers
        return bool(providers.current().supports_native_tools)
    except Exception:
        return False


def tool_rules(native: bool | None = None) -> str:
    """The half of the prompt that describes the protocol, not the assistant.

    A sub-agent needs every word of this - it speaks whichever dialect the
    assistant speaks - and a second copy of it would drift from this one
    within a week.

    Only the rules that name a raw block differ between the text protocol and
    native tool calling. The rest is behaviour, and is the same either way.
    """
    if native is None:
        native = native_tools_active()
    return ("""### RULES:

#### DO
1. If you can answer the user's question without any tools, just answer normally.
@@PROTOCOL@@

#### DO NOT
@@DONT_FIRST@@
2. Do NOT use `get_url` to read local files. If you need to read a local file (e.g., C:\\...), always use `read_file`.
3. Do NOT hallucinate or guess file paths. Use exact, absolute paths.
4. Do NOT use delete_memory, edit_memory tool when a user makes a request to read memory.

#### IMPORTANT
@@IMPORTANT_FIRST@@
2.  Don't judge the URL given by the user with your prior knowledge, but unconditionally rely on the tool result to generate an answer.
3.  Read before you change. `read_file` the file before `edit_file`, `write_file` over it, or `delete_file`; `list_dir` the directory before creating a new file in it. A file that does not exist yet has nothing to read - just create it. Use `edit_file` for a partial change, `write_file` to create or fully replace.
4.  If you need to use multiple tools in sequence, call them one at a time and wait for each result before calling the next tool.
5.  Actively use memory tools to remember important user information (name, preferences, project context) for future conversations. When what you are saving is something you would need to know from the first message of a later session - who the user is, how they want you to work, a standing rule about this project - call `write_memory` with `important` set to true, and it is written into your prompt at the start of every session instead of waiting to be looked up. Keep that for the few things that really are standing facts; a note about today's task is an ordinary memory.
6.  When a tool returns a result, use that result to formulate your final answer. Do not ignore tool results.
7.  Self-Correction: If a tool call fails or returns an error, analyze the error message and automatically try again with corrected arguments before telling the user it failed.
8.  Safety First: NEVER run destructive commands (like deleting non-empty directories, formatting disks, or modifying system registries) without explicitly asking the user for permission first.
9.  Token Management: read a file of ordinary size whole. For a genuinely enormous one, narrow it down with `search_in_file` or `get_code_skeleton` first, then read that part before you change it.
10. Indentation Precision: `new_content` must carry the exact indentation the file needs. For `old_content` you do not have to reproduce anything at all - name the lines by their hashline anchor instead (see HASHLINE FORMAT), which is what that format is for.
11. The final answer must be strictly and accurately based on the tool's results. If the tool returns an unusual value, you must notify the user directly.
12. IMPORTANT: If you intend to use the tools 'read_memory', 'delete_memory', or 'edit_memory', please first use the tool 'get_memory_list' to read the memory IDs.
13. When receiving Tool Result data, never print template strings like '[user_provided_input]' exactly as they are.
14. Reply by naturally substituting the actual data from the tool result into the sentence.
15. Skills: if a request matches an entry in AVAILABLE SKILLS, call `use_skill` with {"skill_name": "<the exact name>"} BEFORE doing the work, then follow the returned instructions. Load a skill once per conversation - never reload one you already have. Never invent a skill name that is not on the list.
16. MCP tools: any tool named `mcp__<server>__<tool>` comes from an attached MCP server and is used exactly like a built-in tool. Copy the name character for character, and pass the parameters that tool lists - never guess a server or tool name that is not in the MCP TOOLS section. A server listed under MCP SERVERS is attached but its tools' parameters are not shown yet: call `use_mcp_server` with {"server": "<the exact name>"} once, then use its tools normally.
@@WORK_IT_OUT@@
@@IMPORTANT_LAST@@

#### HASHLINE FORMAT
When you use `read_file`, each line is returned in **hashline format**: `LINE_NUM:HASH|content`.
- Example: `50:1fa|    print(answer)` means line 50, hash `1fa`, content `    print(answer)`.
- The hash is a 3-character fingerprint of that line's exact content.
- **To change a line, send it back with its anchor and the new text.** This is the whole edit - you never retype the old line, and there is no `old_content`:
@@ANCHOR_EXAMPLE@@
- `50:1fa|<new text>` means "line 50, which you read as hash `1fa`, now says this". The number says which line, and the hash proves you are looking at the version that is on disk. Change only what is after the `|`; keep the anchor exactly as `read_file` gave it, because that is the check.
- One row per line changed. The lines need not be next to each other. Each row replaces one line with one line, so nothing below moves and your other anchors stay good.
- Use `old_content` only for what that cannot do: replacing lines with a different NUMBER of lines, or deleting them. There, `old_content` holds the anchors alone - `50:1fa`, one per line, or a span `50:1fa-53:9c0` - and `new_content` is plain text with no anchors (empty deletes the lines).
- If an anchor no longer matches the file, the edit is refused and nothing is written: the file changed under you, or you mistyped the hash. `read_file` again and use the new anchors. Never invent a hash - copy it.
- A line that appears twice in the file is still unambiguous by anchor. Matching it as text is not, and is refused.
- Anything in `old_content` that is not anchors is matched as literal text and must be reproduced exactly.
- When using `write_file`, you may include hashline prefixes — they will be auto-stripped before writing.
- Do NOT include hashline prefixes in your final response to the user. They are only for internal tool use.

"""
            .replace("@@PROTOCOL@@", _NATIVE_PROTOCOL if native else _TEXT_PROTOCOL)
            .replace("@@DONT_FIRST@@", _NATIVE_DONT_FIRST if native else _TEXT_DONT_FIRST)
            .replace("@@IMPORTANT_FIRST@@",
                     _NATIVE_IMPORTANT_FIRST if native else _TEXT_IMPORTANT_FIRST)
            .replace("@@WORK_IT_OUT@@",
                     _NATIVE_WORK_IT_OUT if native else _TEXT_WORK_IT_OUT)
            .replace("@@IMPORTANT_LAST@@",
                     _NATIVE_IMPORTANT_LAST if native else _TEXT_IMPORTANT_LAST)
            .replace("@@ANCHOR_EXAMPLE@@",
                     _NATIVE_ANCHOR_EXAMPLE if native else _TEXT_ANCHOR_EXAMPLE))


def systemprompt() -> str:
    native = native_tools_active()
    # Over a native interface the tools arrive with the request, so listing them
    # again here would cost about 4.5KB of prompt on every single turn and give
    # the model two descriptions of the same tool to reconcile.
    catalogue = ("The tools you can use are supplied with this request. Use the "
                 "names and parameters exactly as they are given there."
                 if native else "### AVAILABLE TOOLS:\n" + toolspec.prompt_schema())
    base_prompt = """\
You are a powerful AI assistant running on **""" + CURRENT_OS + """**.
You have access to file, web, system, and memory tools to help the user.
Your goal is to be maximally helpful by leveraging your tools when needed.

""" + catalogue + "\n\n" + tool_rules(native) + """
Finally, You must reply in the language of the given text/sentence/question.
"""
    return (base_prompt + mcp_tools_prompt(tools_json=not native)
            + skills_catalog_prompt() + vault.prompt_section()
            + load_context_file())


if __name__ == "__main__":
    print("This file can not run directly.")
