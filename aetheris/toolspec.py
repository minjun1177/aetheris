"""What every built-in tool is, in one place.

The tool list used to live twice: as a hand-written JSON array inside the system
prompt, and again as a chain of `if function_name == ...` branches that pulled
the arguments back out. Nothing tied the two together, so a parameter renamed in
one and not the other produced a tool the model would call correctly and the
harness would silently run with an empty string.

Now both are read from the table below. The prompt is rendered from it, dispatch
binds arguments through it, and `tools.py` refuses to import if a tool in the
table has no handler or a handler has no entry - so the two cannot drift apart
without the program saying so on the way up.

Parameters are listed in the order the handler takes them. Three flags cover
what the old JSON could not say:

* `block`   - the value arrives in a raw <name> block after the JSON, not
              inside it, so it must be kept out of the advertised parameters.
* `hidden`  - accepted if sent, but not advertised. Older argument shapes.
* `aliases` - other keys a model reaches for. `send_input` gets `input` and
              `text` as often as `stdin`, and refusing them helps nobody.

Stdlib only, and imports nothing from the harness: both `tools.py` and
`systemprompt.py` depend on it, so it must be able to depend on neither.
"""

import json
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Param:
    name: str
    description: str = ""
    default: object = ""
    aliases: tuple = ()
    block: bool = False
    hidden: bool = False
    optional: bool = False
    native_description: str = ""     # when the two protocols need different words

    def read(self, arguments: dict):
        """Pull this parameter out of a tool call, under any name it may wear."""
        for key in (self.name, *self.aliases):
            if key in arguments and arguments[key] not in (None, ""):
                return arguments[key]
        # A key that is present but empty still beats the default for anything
        # optional - an empty <stdin> block means "send nothing and listen".
        for key in (self.name, *self.aliases):
            if key in arguments and arguments[key] is not None:
                return arguments[key]
        return self.default

    @property
    def advertised(self) -> bool:
        return not (self.block or self.hidden)

    @property
    def required(self) -> bool:
        """Whether a native tool call must carry this parameter."""
        return not (self.hidden or self.optional) and self.default == ""

    def describe(self, native: bool) -> str:
        return (self.native_description or self.description) if native else self.description


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    params: tuple = ()
    aliases: tuple = ()
    native_description: str = ""     # raw blocks do not exist over native tools

    def schema(self) -> dict:
        """The entry the model sees. Raw-block and legacy parameters stay out."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {p.name: p.description
                           for p in self.params if p.advertised},
        }

    def bind(self, arguments: dict) -> list:
        """Tool-call arguments, in the order the handler takes them."""
        return [p.read(arguments) for p in self.params]

    @property
    def blocks(self) -> tuple:
        return tuple(p.name for p in self.params if p.block)


TOOLS = (
    Tool(
        name='search_web',
        description="Search the web for up-to-date information. Results are passages of real page text gathered from several sources and ranked locally for relevance, NOT search-engine snippets. If the result says 'No relevant results', that is a real outcome: the specific term you asked about was not found on any page that came back. In that case retry with different wording or use get_url on official documentation - never answer from pages the result told you are off-topic.",
        params=(
            Param(name='query', description='The search query string. Keep the distinctive terms (exact identifiers, error strings, product names) and drop conversational filler.'),
        ),
    ),
    Tool(
        name='get_url',
        description='Fetch the content of a URL. HTML pages are automatically converted to clean text.',
        params=(
            Param(name='url', description='The URL to fetch'),
        ),
    ),
    Tool(
        name='run_cmd',
        description="Run a shell command on the user's system. The command stays connected while it runs: if it stops printing and is still alive it is waiting for input, and the result tells you so along with a session id - read its output and reply with send_input. This is how you test an interactive program you just wrote. You may also send the first answers up front in a <stdin> raw block, one per line.",
        native_description="Run a shell command on the user's system. The command stays connected while it runs: if it stops printing and is still alive it is waiting for input, and the result tells you so along with a session id - read its output and reply with send_input. This is how you test an interactive program you just wrote.",
        params=(
            Param(name='command', description="Shell command. On Windows, use 'cmd /c <command>' for built-in commands like echo, dir, etc. On Linux/macOS, use commands directly. The command cannot reach the user's keyboard - you answer its prompts yourself."),
            Param(name='stdin', description='Answers to type in up front, one per line, in a <stdin> raw block.', aliases=('input',), block=True, optional=True, native_description='(Optional) Answers to type in up front, one per line, if you already know what the command will ask.'),
        ),
    ),
    Tool(
        name='send_input',
        description='Answer a command that run_cmd reported as waiting, and read what it prints next. Put the answer in a <stdin> raw block after the JSON, exactly as it should be typed. An empty block sends nothing and just listens for more output. Use this to play through an interactive program one prompt at a time.',
        native_description='Answer a command that run_cmd reported as waiting, and read what it prints next. Use this to play through an interactive program one prompt at a time.',
        params=(
            Param(name='session', description="The session id from the [Waiting] result, e.g. 's1'. Omit it when only one command is running.", optional=True),
            Param(name='stdin', description='What to type, in a <stdin> raw block. An empty block just listens.', aliases=('input', 'text'), block=True, optional=True, native_description='What to type, exactly as it should be typed. Leave it empty to send nothing and just listen for more output.'),
        ),
    ),
    Tool(
        name='end_process',
        description='Stop a command that run_cmd left running, when you are done with it or it will not exit on its own.',
        params=(
            Param(name='session', description="The session id from the [Waiting] result, e.g. 's1'."),
        ),
    ),
    Tool(
        name='run_python',
        description="Run Python in a scratch process and get back what it printed, plus the value of the last line - so `2 ** 10` on its own answers 1024. Use it to work something out before you commit to it: check a calculation, try a regex against real strings, see what a function really returns. Everything it defines - variables, functions, imports - stays for your next run_python call, so you can build up one step at a time. The code is NOT a JSON parameter: put it in a <content> block directly after the JSON object, inside the same <tool_call> (see DO rule 3). It starts in a scratch directory of its own, so whatever it writes lands there and not in your project - to run a file you have written, use run_cmd.",
        native_description="Run Python in a scratch process and get back what it printed, plus the value of the last line - so `2 ** 10` on its own answers 1024. Use it to work something out before you commit to it: check a calculation, try a regex against real strings, see what a function really returns. Everything it defines - variables, functions, imports - stays for your next run_python call, so you can build up one step at a time. It starts in a scratch directory of its own, so whatever it writes lands there and not in your project - to run a file you have written, use run_cmd.",
        params=(
            Param(name='content', description='The Python code, in a <content> raw block. Write it exactly as it should be typed - real line breaks, real quotes, no escaping.', aliases=('code', 'source'), block=True, native_description='The Python code to run, exactly as it should be typed.'),
            Param(name='stdin', description='(Optional) Answers for anything the code reads with input(), in a <stdin> raw block, one per line.', aliases=('input',), block=True, optional=True, native_description='(Optional) Answers for anything the code reads with input(), one per line.'),
            Param(name='reset', description='(Optional) true to throw away every variable from your earlier calls and start with an empty namespace. Leave it out to keep them.', default=False, optional=True),
        ),
    ),
    Tool(
        name='list_dir',
        description='List the contents of a directory.',
        params=(
            Param(name='dirpath', description='The absolute path to the directory.'),
        ),
    ),
    Tool(
        name='read_file',
        description="Read the contents of a file. Output uses hashline format: each line is prefixed with 'LINE_NUM:HASH|'. Example: '1:3d8|import random'. The 3-char hex hash uniquely identifies each line's content.",
        params=(
            Param(name='filepath', description='The absolute path to the file.'),
        ),
    ),
    Tool(
        name='write_file',
        description='Write a file. Use this when creating a new file or completely replacing one. The file body is NOT a JSON parameter: put it in a <content> block directly after the JSON object, inside the same <tool_call> (see DO rule 3). Write it there exactly as it should appear on disk, with no escaping.',
        native_description="Write a file. Use this when creating a new file or completely replacing one. Pass the whole body in 'content', exactly as it should appear on disk.",
        params=(
            Param(name='filepath', description='The absolute path to the file. This one DOES go in the JSON.', native_description='The absolute path to the file.'),
            Param(name='content', description='The whole file body, in a <content> raw block.', block=True, native_description='The whole file body, exactly as it should appear on disk.'),
        ),
    ),
    Tool(
        name='edit_file',
        description='Edit part of an existing file. Use this instead of write_file when you only need to change part of a file. The snippets are NOT JSON parameters: put them in <old_content> and <new_content> blocks after the JSON object, inside the same <tool_call> (see DO rule 3). SHORTEST WAY, and the one to reach for first: send ONLY a <new_content> block holding the changed line with the anchor read_file gave it - "38:ff7|print()" means line 38 becomes print(). No <old_content> at all, and the old text is never retyped. Use <old_content> only when the number of lines changes or they are being deleted. See HASHLINE FORMAT.',
        native_description='Edit part of an existing file. Use this instead of write_file when you only need to change part of a file. SHORTEST WAY, and the one to reach for first: pass ONLY new_content, holding the changed line with the anchor read_file gave it - "38:ff7|print()" means line 38 becomes print(). No old_content at all, and the old text is never retyped. Use old_content only when the number of lines changes or they are being deleted. See HASHLINE FORMAT.',
        params=(
            Param(name='filepath', description='The absolute path to the file. This one DOES go in the JSON.', native_description='The absolute path to the file.'),
            Param(name='old_content', description='(Leave this out when the new lines carry their own anchors.) Which lines to replace, in an <old_content> raw block: their hashline anchors - "50:1fa" for one line, one anchor per line for several, "50:1fa-53:9c0" for a span - or the exact text of the snippet.', block=True, optional=True, native_description='(Leave this out when the new lines carry their own anchors.) Which lines to replace: their hashline anchors - "50:1fa" for one line, one anchor per line for several, "50:1fa-53:9c0" for a span - or the exact snippet as it currently appears in the file.'),
            Param(name='new_content', aliases=('content',), description='The new lines, in a <new_content> raw block. Give each one the anchor of the line it replaces - "38:ff7|print()" - and old_content is not needed. Without anchors it is plain replacement text for whatever old_content named, and empty deletes those lines.', block=True, native_description='The new lines. Give each one the anchor of the line it replaces - "38:ff7|print()" - and old_content is not needed. Without anchors it is plain replacement text for whatever old_content named, and empty deletes those lines.'),
        ),
    ),
    Tool(
        name='delete_file',
        description='Delete a specific file.',
        params=(
            Param(name='filepath', description='The absolute path to the file to delete.'),
        ),
    ),
    Tool(
        name='copy_file',
        description='Copy a file from one path to another.',
        params=(
            Param(name='src', description='The absolute path to the source file.'),
            Param(name='dst', description='The absolute path to the destination file.'),
        ),
    ),
    Tool(
        name='create_dir',
        description='Create a new directory.',
        params=(
            Param(name='dirpath', description='The absolute path to the new directory.'),
        ),
    ),
    Tool(
        name='git_status',
        description='Get the current git status.',
    ),
    Tool(
        name='git_diff',
        description='Get the current git diff.',
    ),
    Tool(
        name='write_memory',
        description='Save important information to persistent memory for future recall.',
        params=(
            Param(name='id', description="Memory ID (a descriptive label). e.g., 'User name', 'User preferences', 'Project goal'"),
            Param(name='content', description="Memory content. e.g., 'Jhon', 'Prefers dark mode', 'Build a chat app'"),
            Param(name='important', description="true for something you must know from the very first message of every future session - who the user is, how they want you to work, a standing rule about this project. Those are written into your prompt at the start of each session; everything else is only read when you go looking for it with read_memory. Leave it out when saving over an existing memory to keep the mark it already has, or send false to take the mark away.", aliases=('is_important',), optional=True),
        ),
    ),
    Tool(
        name='get_memory_list',
        description='Get the list of all stored memory IDs with previews.',
    ),
    Tool(
        name='read_memory',
        description='Read the full content of a specific memory by its ID.',
        params=(
            Param(name='id', description='The memory ID to read.'),
        ),
    ),
    Tool(
        name='delete_memory',
        description='Delete a specific memory by its ID.',
        params=(
            Param(name='id', description='The memory ID to delete.'),
        ),
    ),
    Tool(
        name='edit_memory',
        description='Edit the content of a specific memory by its ID.',
        params=(
            Param(name='id', description='The memory ID to edit.'),
            Param(name='new_content', description='The new content for the memory.'),
        ),
    ),
    Tool(
        name='view_image',
        description="Look at an image file - a screenshot, a diagram, a photo, a chart you just produced. The image is put in front of you together with this tool's result, so answer from what you see in it. read_file cannot do this: an image read as text is broken bytes. Only call this when you actually need to see the picture, and only once per file.",
        params=(
            Param(name='filepath', description='The path to the image. png, jpeg, gif and webp.', aliases=('path', 'image', 'file')),
        ),
    ),
    Tool(
        name='write_note',
        description="Save a markdown note about THIS project - why something is built the way it is, the order a job has to be done in, what is still open. Notes are per project and are not the same store as write_memory, which is about the user and follows them everywhere. The body is NOT a JSON parameter: put it in a <content> block directly after the JSON object, inside the same <tool_call> (see DO rule 3). Write markdown there exactly as it should read, with no escaping. Writing a note under an id that already exists replaces it whole.",
        native_description="Save a markdown note about THIS project - why something is built the way it is, the order a job has to be done in, what is still open. Notes are per project and are not the same store as write_memory, which is about the user and follows them everywhere. Writing a note under an id that already exists replaces it whole.",
        params=(
            Param(name='id', description="The note id, which is also its title - a short descriptive label, e.g. 'deploy-order', 'why-hashline', 'open-questions'. This one DOES go in the JSON.", aliases=('note', 'note_id', 'title'), native_description="The note id, which is also its title - a short descriptive label, e.g. 'deploy-order', 'why-hashline', 'open-questions'."),
            Param(name='content', description='The whole note, as markdown, in a <content> raw block.', block=True, native_description='The whole note, as markdown.'),
        ),
    ),
    Tool(
        name='read_note',
        description='Read one of this project\'s markdown notes in full, by its id. The ids are listed in your prompt under PROJECT NOTES.',
        params=(
            Param(name='id', description='The note id to read.', aliases=('note', 'note_id', 'title')),
        ),
    ),
    Tool(
        name='list_notes',
        description="List this project's markdown notes - id, size and when each was last written.",
    ),
    Tool(
        name='edit_note',
        description='Replace the body of a note that already exists. Use write_note to start a new one. The new body is NOT a JSON parameter: put it in a <new_content> block directly after the JSON object, inside the same <tool_call> (see DO rule 3).',
        native_description='Replace the body of a note that already exists. Use write_note to start a new one.',
        params=(
            Param(name='id', description='The note id to edit.', aliases=('note', 'note_id', 'title')),
            Param(name='new_content', description='The whole new note, as markdown, in a <new_content> raw block.', block=True, native_description='The whole new note, as markdown.'),
        ),
    ),
    Tool(
        name='delete_note',
        description='Delete one of this project\'s markdown notes by its id.',
        params=(
            Param(name='id', description='The note id to delete.', aliases=('note', 'note_id', 'title')),
        ),
    ),
    Tool(
        name='get_user_input',
        description="Ask the user one or more questions and get their answers. Each question is asked separately, with its own list of options; the user picks a number or types their own answer. Use this when a choice is genuinely the user's to make.",
        params=(
            Param(name='what_do', description='', hidden=True),
            Param(name='prompt', description='', default=[], hidden=True),
            Param(name='questions', description='A list of questions. Each item is an object: {"question": "the question text", "options": ["choice 1", "choice 2", "choice 3"]}. Give 2-4 options per question, or an empty list to ask for free text. Do NOT add a \'custom\', \'other\', \'Custom Input\' or \'직접 입력\' option yourself - a Custom Input choice is always appended automatically, so adding one duplicates it. List only the real alternatives. Ask every question you need in ONE call - do not call this tool repeatedly.'),
        ),
    ),
    Tool(
        name='get_system_info',
        description='Show system usage.',
    ),
    Tool(
        name='search_in_file',
        description='Search for a text pattern or regex across all files in the current workspace directory (like grep). Returns matching lines with file paths and line numbers.',
        params=(
            Param(name='query', description='The text or regex pattern to search for.'),
            Param(name='is_regex', description='If true, treat query as a regular expression. Default is false.', default=False),
        ),
    ),
    Tool(
        name='call_api',
        description='Send an HTTP request to an external API endpoint. Supports GET, POST, PUT, PATCH, DELETE methods.',
        params=(
            Param(name='url', description='The full URL of the API endpoint.'),
            Param(name='method', description='HTTP method: GET, POST, PUT, PATCH, or DELETE.'),
            Param(name='headers', description='(Optional) JSON string of HTTP headers. e.g., {"Authorization": "Bearer token", "Content-Type": "application/json"}', optional=True),
            Param(name='payload', description='(Optional) JSON string of the request body for POST/PUT/PATCH requests.', optional=True),
        ),
    ),
    Tool(
        name='get_code_skeleton',
        description='Parse a source code file using AST and return a JSON tree of its structure (functions, classes, parameters, return types, decorators). Useful for understanding architecture without reading the entire file. Supports: Python, JavaScript, TypeScript, Java, C, C++, Go, Rust, C#.',
        params=(
            Param(name='file_path', description='The absolute path to the source code file to analyze.'),
        ),
    ),
    Tool(
        name='query_ast_node',
        description='Search for specific AST patterns in a source file using Tree-sitter S-expression query syntax. Returns matching node locations and code snippets. Useful for finding security vulnerabilities, unsafe patterns, or specific code constructs. Example patterns: \'(call function: (attribute) @fn)\' to find method calls, \'(binary_operator operator: "+" right: (identifier) @val)\' to find string concatenation with variables.',
        params=(
            Param(name='file_path', description='The absolute path to the source code file to search.'),
            Param(name='pattern', description='A Tree-sitter S-expression query pattern. Use @capture_name to capture nodes.'),
            Param(name='language', description='(Optional) Language name (python, javascript, typescript, java, c, cpp, go, rust, csharp). Auto-detected from file extension if omitted.', optional=True),
        ),
    ),
    Tool(
        name='submit_plan_for_approval',
        description='Submit a task plan and diff blueprint to the user for approval. You MUST use this before execution when PLAN MODE is active and the task involves modifying files or complex logic.',
        params=(
            Param(name='context_discovered', description='Summary of what files and context you analyzed to form this plan.'),
            Param(name='diff_blueprint', description='Detailed outline of exactly which files and functions will change and how.'),
            Param(name='verification_steps', description='How you will verify the changes after execution.'),
        ),
    ),
    Tool(
        name='spawn_agent',
        description="Hire a second AI to carry out one self-contained piece of work and report back. It starts with no memory of this conversation, works on its own with the same tools you have, and everything it writes comes back to you as this tool's result - the user never sees it. Use it when a sub-task would take many tool calls whose output you would not need again: searching a codebase for where something is handled, reading several files to answer one question, checking a list of URLs. Do NOT use it for work that is quicker to do yourself, for anything needing a decision from the user, or as a way to avoid reading a result you were given.",
        params=(
            Param(name='task', description="The brief, written for someone who cannot see this conversation and cannot ask you anything. State what to do, which files or paths to start from, and exactly what to report back. A vague brief comes back as a vague report."),
            Param(name='context', description="(Optional) Facts it needs that it could not find on its own - what has already been tried, a decision you have made, the shape of an answer you want.", optional=True),
            Param(name='model', description="(Optional) A different model to run it on, e.g. a smaller one for a long mechanical search. Defaults to the model you are running on.", optional=True),
        ),
    ),
    Tool(
        name='list_agents',
        description="List the other AI agents working in this same project right now, what each one is doing, and which files each has claimed. Other agents are separate assistants in their own terminals, editing the same files you are. Call this before starting work that touches shared files, and whenever you need to know who to ask about one.",
    ),
    Tool(
        name='send_agent_message',
        description="Send one coordination move to another AI agent working in this same project: ask who is changing a file, say what you are taking or have released, warn that a change will break their work, or answer what they asked you. This is NOT a discussion channel - it cannot be used to debate an approach, agree a design or divide up work, and a message is capped at 250 characters for that reason. It is delivered while they work; you cannot wait for a reply, so send it and get on with your own job.",
        params=(
            Param(name='kind', description="What kind of move this is - one of: question (a specific question about a file or a claim), answer (the reply to one), claim (I am taking this file - name it), release (this file is free now - name it), done (I have finished this piece of work), warn (this will break something you are working on). Nothing else is a message."),
            Param(name='message', description='The move itself, in one sentence, naming the file you mean. 250 characters.'),
            Param(name='to', description="(Optional) The agent id from list_agents, e.g. 'a2'. Leave it out to tell everyone here.", optional=True),
        ),
    ),
    Tool(
        name='claim_files',
        description="Announce that you are about to change these files, so no other agent changes them at the same time. While you hold a claim, other agents are refused any edit to those files and are told to ask you. Claim before you start a multi-step change to a shared file, and call release_files as soon as you are done with it. If another agent already holds one of the files, nothing is claimed and you are told who has it.",
        params=(
            Param(name='paths', description='The file path to claim. Several may be given, separated by commas.'),
            Param(name='reason', description='What you are doing to them, in a few words. The other agents see this.'),
        ),
    ),
    Tool(
        name='release_files',
        description='Hand back files you claimed with claim_files, so another agent can work on them. Call this as soon as you have finished changing them - do not leave a claim open across the rest of the conversation.',
        params=(
            Param(name='paths', description='The file path to release. Several may be given, separated by commas.'),
        ),
    ),
    Tool(
        name='use_skill',
        description="Load the full instructions of a skill listed under AVAILABLE SKILLS. Call this BEFORE starting a task whenever the request matches a skill's description. Returns the skill's instructions plus the absolute paths of any files bundled with it.",
        aliases=('skill',),
        params=(
            Param(name='skill_name', description='The exact skill name from the AVAILABLE SKILLS list.', aliases=('name', 'skill')),
        ),
    ),
    Tool(
        name='use_mcp_server',
        description="Load the tools of an MCP server listed under MCP SERVERS. Those servers are attached and working, but only their tool names are shown - this returns the parameters for each one. Call it before using a tool from a server listed there, once per server per conversation.",
        aliases=('load_mcp_server', 'get_mcp_tool_list'),
        params=(
            Param(name='server', description='The exact server name from the MCP SERVERS list.', aliases=('name', 'server_name')),
        ),
    ),)


_BY_NAME = {}
for _tool in TOOLS:
    for _name in (_tool.name, *_tool.aliases):
        _BY_NAME[_name] = _tool


def get(name: str):
    """The tool a call names, or None. Aliases resolve to the real tool."""
    return _BY_NAME.get(name)


def names() -> list:
    return [tool.name for tool in TOOLS]


def prompt_schema(indent: int = 2, exclude: tuple = ()) -> str:
    """The AVAILABLE TOOLS array, rendered rather than transcribed.

    `exclude` drops tools from the listing, which is how a sub-agent is given a
    smaller set than the assistant it was hired by. A tool left out here is left
    out of the sub-agent's dispatch too, so the listing is the truth.
    """
    return json.dumps([tool.schema() for tool in TOOLS if tool.name not in exclude],
                      indent=indent, ensure_ascii=False)


def native_schema(exclude: tuple = ()) -> list:
    """The tools in the shape a hosted API wants for real function calling.

    Vendor-neutral: name, description, and a JSON Schema for the arguments.
    Each provider reshapes this into its own wire format - see `encode_tools`
    on the providers - so all three describe the same set of tools by
    construction.

    The wording differs from `prompt_schema` where it has to. Raw <content>
    blocks are a workaround for models that can only emit text; over a native
    interface the parameter carries the file body directly, so `write_file`
    here has a `content` parameter and does not mention blocks at all.
    """
    schemas = []
    for tool in TOOLS:
        if tool.name in exclude:
            continue
        properties, required = {}, []
        for param in tool.params:
            if param.hidden:
                continue
            properties[param.name] = {
                "type": "boolean" if isinstance(param.default, bool)
                        else "array" if isinstance(param.default, list)
                        else "string",
                "description": param.describe(native=True),
            }
            if param.required:
                required.append(param.name)
        schemas.append({
            "name": tool.name,
            "description": tool.native_description or tool.description,
            "input_schema": {"type": "object", "properties": properties,
                             "required": required},
        })
    return schemas


if __name__ == "__main__":
    print("This file can not run directly.")
