"""The Python scratch VM: what it remembers, and what it does when it dies.

`run_python` exists for the model the whole harness is built around - one too
small to hold an intermediate result in its head, and too small to escape a
snippet of code into a JSON string. So the two things worth testing hardest are
the two that make it worth having:

* the code arrives through a `<content>` block, byte for byte, exactly as
  `write_file` does - checked through the real parser, not by calling the
  handler directly;
* what one call defines is still there in the next one.

The rest is about dying honestly. A VM that is killed for running too long has
lost every variable in it, and a model told only "that failed" will go on
referring to them - so each of the three ways it can die is checked for saying
so out loud.
"""
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aetheris import paths

# Before `config` is imported: it reads the paths at import time, and the VM's
# scratch directory must not be the real ~/.aetheris/vm.
HOME = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".vm-test-home")
shutil.rmtree(HOME, ignore_errors=True)
os.environ[paths.ENV_VAR] = HOME

from aetheris import config          # noqa: E402
config.MCP_ENABLED = False
config.SAVE_CHAT_HISTORY = False
config.AUTO_ALLOW = True
config.GIT_AUTO_COMMIT = False
config.PERMISSIONS_ENABLED = False
config.NATIVE_TOOLS = False
config.VM_TIMEOUT = 4

from aetheris import llm_client, tools, vm      # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


def run(code, **arguments):
    return tools.dispatch_tool("run_python", dict(content=code, **arguments))


# ---------------------------------------------------------------------------
print("--- the code arrives as a raw block, not as an escaped string ---")
# This is the whole reason the tool takes a <content> block. The snippet below
# holds every character that breaks a JSON string: quotes of both kinds, a
# backslash, a brace, real newlines.
reply = '''<tool_call>
{"name": "run_python", "arguments": {}}
<content>
pattern = "he said \\"hi\\" and left"
print({"k": pattern}["k"])
len(pattern)
</content>
</tool_call>'''
calls = llm_client.parse_tool_calls(reply)
check("the call is read", [name for name, _ in calls] == ["run_python"], str(calls))
name, arguments = calls[0]
check("the code came through the block", "arguments" not in arguments
      and arguments.get("content", "").startswith("pattern ="),
      repr(arguments.get("content", ""))[:60])
result = tools.dispatch_tool(name, arguments)
check("and it ran with the quotes intact", 'he said "hi" and left' in result, result[:80])
check("the last expression is answered", "=> 21" in result, result[-40:])

check("a model that writes it in the JSON is understood too",
      "=> 4" in run("2 + 2"))
check("and one that calls the parameter 'code'",
      "=> 6" in tools.dispatch_tool("run_python", {"code": "2 * 3"}))

# ---------------------------------------------------------------------------
print("\n--- it remembers, which is what makes it a VM and not a one-liner ---")
first = run("import statistics\nscores = [88, 92, 79]\nstatistics.mean(scores)")
check("the first call works", first.startswith("[Success"), first[:60])
check("and says what it is holding", "kept for your next run_python call" in first
      and "scores" in first, first[-80:])
later = run("sorted(scores)[-1]")
check("a later call still has it", "=> 92" in later, later[:80])
check("and so does the import", "=> 79" in run("min(scores)"))

check("reset empties it", "NameError" in run("scores", reset=True))
# `"reset": "false"` is a string, and every non-empty string is truthy. Reading
# it with bool() would throw away the model's whole working namespace at the
# exact moment it said not to.
run("kept_after = 1")
check('but the word "false" does not',
      "=> 1" in run("kept_after", reset="false"), )
check('while the word "true" does',
      "NameError" in run("kept_after", reset="true"))

# ---------------------------------------------------------------------------
print("\n--- what it says when the code is wrong ---")
broken = run("total = 0\ntotal += missing")
check("an exception is an [Error]", broken.startswith("[Error]"), broken[:40])
check("naming the line the model wrote", 'File "<vm>", line 2' in broken, broken)
check("and none of the harness's own frames", "kernel.py" not in broken, broken)
syntax = run("if True\n    pass")
check("a syntax error is reported, not raised", "SyntaxError" in syntax, syntax[:80])
check("and nothing ran", "Traceback" not in syntax, syntax[:80])

printed_then_failed = run("print('got this far')\nraise ValueError('nope')")
check("output printed before a failure survives it",
      "got this far" in printed_then_failed and "ValueError: nope" in printed_then_failed,
      printed_then_failed[:120])

eof = run("input()")
check("input() with nothing to read says what to do instead",
      "<stdin> block" in eof, eof[-90:])

empty = run("")
check("a call with no code is refused rather than run",
      empty.startswith("[Error]") and "<content> block" in empty, empty[:80])

# ---------------------------------------------------------------------------
print("\n--- stdin feeds input(), so a prompt can be tried in one call ---")
answered = run("n = int(input('n? '))\nprint(n * 3)", stdin="14\n")
check("the answer is read", "42" in answered, answered[:100])
check("and the prompt it printed is shown", "n?" in answered, answered[:100])

# ---------------------------------------------------------------------------
print("\n--- every way it can die says the variables are gone ---")
run("survivor = 1")
began = time.time()
timed_out = run("print('before the loop', flush=True)\nwhile True:\n    pass")
took = time.time() - began
check("an endless loop is stopped", timed_out.startswith("[Error]"), timed_out[:60])
check(f"at about VM_TIMEOUT ({config.VM_TIMEOUT}s)",
      config.VM_TIMEOUT <= took < config.VM_TIMEOUT + 5, f"{took:.1f}s")
check("and the model is told its variables went with it",
      "variable" in timed_out and "gone" in timed_out, timed_out[:160])
check("what it printed first is kept", "before the loop" in timed_out, timed_out)
check("the next call works again", "=> 7" in run("7"))

crashed = run("import os\nos._exit(3)")
check("a process that kills itself is reported", crashed.startswith("[Error]"), crashed[:60])
check("with the same warning about the namespace",
      "gone" in crashed, crashed[:160])
check("and it comes back", "=> 8" in run("8"))

# ---------------------------------------------------------------------------
print("\n--- it is a scratchpad, not the project ---")
here = os.getcwd()
# Printed and compared as a path rather than matched inside the result text:
# the result carries `repr(os.getcwd())`, and on Windows a repr doubles every
# backslash, so the directory the VM is genuinely running in never matched the
# one this asked about. Through `realpath` and `normcase`, the way
# `session.latest_in_dir` compares two spellings of one directory.
answer = run("import os\nprint(os.path.realpath(os.getcwd()))")
wanted = os.path.normcase(os.path.realpath(vm.scratch_dir()))
check("the code runs in the VM's own directory",
      any(os.path.normcase(line.strip()) == wanted for line in answer.splitlines()),
      answer.strip()[:120])
run("open('scribble.txt', 'w').write('x')")
check("so a file it writes lands there",
      os.path.isfile(os.path.join(vm.scratch_dir(), "scribble.txt")))
check("and not in the project", not os.path.isfile(os.path.join(here, "scribble.txt")))
check("the project is still importable, so a real function can be tried",
      "'.aetheris'" in run("import aetheris.paths as p\np.DIR_NAME"))
# ...and importing it leaves nothing behind. A __pycache__ the model never
# asked for turning up in `git status` is a small thing that costs a real
# conversation to explain.
check("but nothing it imports writes a __pycache__ into the project",
      "=> True" in run("import sys\nsys.dont_write_bytecode"))

# ---------------------------------------------------------------------------
print("\n--- the ceilings ---")
long_output = run("for i in range(20000):\n    print('line', i)")
check("a flood of output is trimmed", "characters trimmed" in long_output,
      f"{len(long_output)} chars")
check("but both ends of it are kept",
      "line 0" in long_output and "line 19999" in long_output)

# `VM_MEMORY_MB` is a real ceiling on Linux and a request everywhere else.
# Darwin accepts `setrlimit(RLIMIT_AS)` and does not enforce it, so the same
# allocation there runs until the wall clock stops it - which is why the
# wall-clock kill is the guarantee and the rlimit is the optimisation. What is
# asserted is what each platform actually provides; claiming the cap holds
# everywhere is the kind of unearned promise this harness exists to avoid.
hungry = run(f"bytearray({config.VM_MEMORY_MB * 4} * 1024 * 1024)")
if sys.platform.startswith("linux"):
    check("an allocation past VM_MEMORY_MB is a MemoryError",
          "MemoryError" in hungry, hungry[:120])
else:
    check("a runaway allocation is stopped one way or another",
          hungry.startswith("[Error]"), hungry[:120])
    print(f"  [note] {sys.platform} does not enforce RLIMIT_AS; the "
          f"{config.VM_TIMEOUT}s wall clock is what stopped it")
check("and the VM answers again afterwards", "=> 9" in run("9"))

# ---------------------------------------------------------------------------
print("\n--- and it counts as changing the world ---")
# Whatever the code does is not knowable from the call, exactly as for run_cmd,
# so a stage that must not change anything cannot allow it.
check("run_python is in _CHANGES_THINGS", "run_python" in tools._CHANGES_THINGS)
config.DEEPTHINK_READONLY = True
refused = run("1 + 1")
config.DEEPTHINK_READONLY = False
check("so a read-only deepthink stage refuses it",
      refused.startswith("[System]"), refused[:60])
check("it claims no files, having no path to claim",
      "run_python" not in tools._WRITES_FILES)

vm.shutdown()
shutil.rmtree(HOME, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("vm checks passed")
