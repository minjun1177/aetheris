"""The benchmark's graders, without a model.

A grader that passes a wrong answer makes every number the benchmark prints a
lie, and nobody would notice: the runs look fine. So each one is held to a
right answer and to the wrong answers a small model actually gives - the file
rewritten down to the one function asked about, the call made when none was
wanted, the write before the read - and the fixture is checked to still have
the bug the edit task asks to fix.
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("bench_run", os.path.join(ROOT, "bench", "run.py"))
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


def task(name):
    return next(t for t in bench.TASKS if t["name"] == name)


def workdir(name):
    work = tempfile.mkdtemp(prefix="bench-test-")
    if task(name)["setup"]:
        task(name)["setup"](work)
    return work


def write(work, name, text):
    with open(os.path.join(work, name), "w", encoding="utf-8") as handle:
        handle.write(text)


def call(name, outcome="ok"):
    return {"name": name, "arguments": {}, "outcome": outcome, "seconds": 0.1}


print("--- the five tool-calling tasks ---")
check("there are five tool tasks and two edits",
      [t["kind"] for t in bench.TASKS].count("tool") == 5
      and [t["kind"] for t in bench.TASKS].count("edit") == 2)

w = workdir("one_call")
check("one_call: a listing that names both files passes",
      bench.grade_listing({"tool_calls": [call("list_dir")],
                           "text": "alpha.txt and beta.txt"}, w)[0])
check("but naming them without looking does not",
      not bench.grade_listing({"tool_calls": [], "text": "alpha.txt beta.txt"}, w)[0])

w = workdir("use_result")
check("use_result: the number after a read passes",
      bench.grade_secret({"tool_calls": [call("read_file")], "text": "It is 48213."}, w)[0])
check("a guess does not", not bench.grade_secret(
    {"tool_calls": [call("read_file")], "text": "It is 4821."}, w)[0])

w = workdir("exact_args")
write(w, "greeting.txt", "Hello, Aetheris!\n")
check("exact_args: the exact content passes (a final newline is fine)",
      bench.grade_exact({}, w)[0])
write(w, "greeting.txt", "Hello Aetheris!")
check("a missing comma does not", not bench.grade_exact({}, w)[0])

w = workdir("two_calls")
write(w, "hello.txt", "Hello, Mirae\n")
check("two_calls: read then write passes", bench.grade_two_calls(
    {"tool_calls": [call("read_file"), call("write_file")]}, w)[0])
check("write then read does not", not bench.grade_two_calls(
    {"tool_calls": [call("write_file"), call("read_file")]}, w)[0])

check("no_call: answering with no call passes",
      bench.grade_no_call({"tool_calls": [], "text": "51"}, None)[0])
check("calling a tool anyway does not", not bench.grade_no_call(
    {"tool_calls": [call("run_python")], "text": "51"}, None)[0])

print("\n--- the two edits ---")
w = workdir("create_file")
write(w, "fizzbuzz.py", "def fizzbuzz(n):\n    return 'FizzBuzz' if n % 15 == 0 else "
      "'Fizz' if n % 3 == 0 else 'Buzz' if n % 5 == 0 else str(n)\n")
check("create_file: a working fizzbuzz passes", bench.grade_create({}, w)[0])
write(w, "fizzbuzz.py", "def fizzbuzz(n):\n    return 'Fizz' if n % 3 == 0 else str(n)\n")
check("one that forgets Buzz does not", not bench.grade_create({}, w)[0])

w = workdir("fix_function")
passed, why = bench.grade_fix({}, w)
check("fix_function: the fixture still has the bug", not passed, why)
with open(os.path.join(w, "stats.py"), encoding="utf-8") as handle:
    source = handle.read()
size = len(source)
check("and is the size the docs say", 5000 <= size <= 6500, str(size))
fixed = source.replace("    return ordered[middle]\n\n\ndef standard_deviation",
                       "    return (ordered[middle - 1] + ordered[middle]) / 2\n\n\n"
                       "def standard_deviation")
check("(the fix applies to it)", fixed != source)
write(w, "stats.py", fixed)
check("the real fix passes", bench.grade_fix({}, w)[0], bench.grade_fix({}, w)[1])
write(w, "stats.py", "def median(values):\n    s = sorted(values)\n    m = len(s) // 2\n"
      "    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2\n")
passed, why = bench.grade_fix({}, w)
check("a file rewritten down to median does not", not passed and "lost" in why, why)

print("\n--- the record ---")
runs = [
    {"task": "a", "passed": True, "seconds": 1.0, "tokens": 100},
    {"task": "a", "passed": False, "seconds": 3.0, "tokens": 300},
    {"task": "b", "passed": True, "seconds": 2.0, "tokens": 200},
]
summary = bench.summarise(runs)
check("passes are counted per task", summary["a"]["passed"] == 1 and summary["a"]["runs"] == 2)
check("and over all of them", summary["all"] == {"passed": 2, "runs": 3,
                                                 "median_seconds": 2.0, "median_tokens": 200},
      str(summary["all"]))
later = bench.summarise([dict(r, passed=True) for r in runs])
table = bench.compare({"summary": summary}, {"summary": later})
check("--compare shows before, after and the change",
      "1/2" in table and "2/2" in table and "+50%" in table, "\n" + table)

home = bench.make_home("some:model", 7, 0.2)
try:
    with open(os.path.join(home, "settings.json"), encoding="utf-8") as handle:
        settings = json.load(handle)
    with open(os.path.join(home, "providers.json"), encoding="utf-8") as handle:
        state = json.load(handle)
finally:
    shutil.rmtree(home, ignore_errors=True)
check("a run's home keeps nothing behind and talks to nobody",
      settings.get("SAVE_CHAT_HISTORY") is False and settings.get("CHANNEL_ENABLED") is False
      and settings.get("MCP_ENABLED") is False, str(settings))
check("and carries the seed, the temperature and the model",
      settings.get("OLLAMA_SEED") == 7 and settings.get("OLLAMA_TEMPERATURE") == 0.2
      and state["providers"]["ollama"]["model"] == "some:model", str(state))


print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("bench checks passed")
