"""The text protocol's repair engine: what it reads, and what it refuses.

A model with no tool-calling template of its own is the whole audience for this
code, and it will not write `<tool_call>` reliably. Measured against the three
models Ollama reports as having no tool support - gemma3:1b, 4b and 12b - the
harness ran **no tool at all**: the models wrapped a perfectly formed call in a
markdown fence, or nested it under a `tool_call` key, or named it `tool_name`.
`parse_tool_calls` found no literal tag, returned nothing, and the turn ended
with no tool run, no error, and nothing said. Silent failure is the one outcome
the text protocol exists to prevent.

The other half of this file is the refusals. Repair must never invent an
argument (ARCHITECTURE 5.6), and reading a fence must never turn an ordinary
code block in an answer into a tool call.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aetheris import config
config.MCP_ENABLED = False
config.SAVE_CHAT_HISTORY = False

from aetheris import llm_client

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


def parse(text):
    return llm_client.parse_tool_calls(text, quiet=True)


def one(text):
    calls = parse(text)
    return calls[0] if len(calls) == 1 else (None, None)


# ---------------------------------------------------------------------------
print("--- the tag, which is what the prompt asks for ---")
check("a closed block is read",
      one('<tool_call>{"name": "read_file", "arguments": {"filepath": "a.py"}}</tool_call>')
      == ("read_file", {"filepath": "a.py"}))
check("a raw block beats the JSON",
      one('<tool_call>{"name": "write_file", "arguments": {"filepath": "g.py"}}\n'
          '<content>\nx = 1\n</content></tool_call>')
      == ("write_file", {"filepath": "g.py", "content": "x = 1"}))
check("parameters flattened beside the name are still found",
      one('<tool_call>{"name": "read_file", "filepath": "a.py"}</tool_call>')
      == ("read_file", {"filepath": "a.py"}))
check("two calls in one reply both survive",
      len(parse('<tool_call>{"name": "git_status", "arguments": {}}</tool_call>'
                '<tool_call>{"name": "git_diff", "arguments": {}}</tool_call>')) == 2)

# ---------------------------------------------------------------------------
print("\n--- a call wrapped in a markdown fence (gemma3) ---")
check("```tool_call is read, and the raw block outside it comes along",
      one('```tool_call\n{"name": "write_file", "arguments": {"filepath": "hello.py"}}\n```\n'
          "<content>\nprint('hi')\n</content>\n")
      == ("write_file", {"filepath": "hello.py", "content": "print('hi')"}))
check("```tool_code is read too",
      one('```tool_code\n{"name": "read_file", "arguments": {"filepath": "a.py"}}\n```')
      == ("read_file", {"filepath": "a.py"}))
check("so is a bare ```json fence, when it decodes to a real tool",
      one('Here you go.\n```json\n{"name": "list_dir", "arguments": {"dirpath": "."}}\n```')
      == ("list_dir", {"dirpath": "."}))

print("\n--- a call nested one level too deep ---")
check("a `tool_call` envelope is unwrapped",
      one('```tool_code\n{"tool_call": {"name": "write_file",'
          ' "arguments": {"filepath": "hello.py"}}}\n```')
      == ("write_file", {"filepath": "hello.py"}))
check("so is a `function_call` envelope inside the tag",
      one('<tool_call>{"function_call": {"name": "git_status", "arguments": {}}}</tool_call>')
      == ("git_status", {}))

print("\n--- the name under a key of another name ---")
check("`tool_name` carries the name",
      one('```tool_code\n{"tool_name": "write_file",'
          ' "arguments": {"filepath": "hello.py"}}\n```')
      == ("write_file", {"filepath": "hello.py"}))
check("`function_name` does too",
      one('<tool_call>{"function_name": "git_status", "arguments": {}}</tool_call>')
      == ("git_status", {}))
check("and the key it came from does not become an argument",
      one('<tool_call>{"tool_name": "read_file", "filepath": "a.py"}</tool_call>')
      == ("read_file", {"filepath": "a.py"}))

# ---------------------------------------------------------------------------
print("\n--- what must not be read as a tool call ---")
check("an ordinary answer is left alone",
      parse("안녕하세요. 무엇을 도와드릴까요?") == [])
check("a python code block in an answer stays a code block",
      parse('이렇게 쓰세요:\n```python\nprint("hi")\n```') == [])
check("a json code block that is not a call stays a code block",
      parse('설정 예시:\n```json\n{"model": "gemma4:e4b", "num_ctx": 65536}\n```') == [])
check("a fence naming a tool that does not exist is not run",
      parse('```tool_call\n{"name": "launch_missiles", "arguments": {}}\n```') == [])
check("a single-key dict that is not an envelope is not flattened",
      parse('```json\n{"config": {"model": "x"}}\n```') == [])
check("an envelope whose inner dict has no name is left alone",
      parse('<tool_call>{"tool_call": {"tool": {"name": "git_status"}}}</tool_call>') == [])
check("arguments sitting beside an envelope key mean it is not an envelope",
      parse('<tool_call>{"tool": {"name": "read_file"}, "filepath": "a.py"}</tool_call>') == [])

print("\n--- a reply that stopped early invents nothing (5.6) ---")
check("a call cut off mid-string is refused, not completed",
      parse('<tool_call>{"name": "write_file", "arguments": {"filepath": "real.py", "content": "def ') == [])
check("a raw block left open is refused",
      parse('<tool_call>{"name": "write_file", "arguments": {"filepath": "real.py"}}\n<content>\nhalf') == [])

print("\n--- the tag wins over a fence, so a correct call is never re-read ---")
both = ('<tool_call>{"name": "git_status", "arguments": {}}</tool_call>\n'
        '```tool_call\n{"name": "delete_file", "arguments": {"filepath": "x"}}\n```')
check("only the tagged call is run", parse(both) == [("git_status", {})])

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("tool parsing checks passed")
