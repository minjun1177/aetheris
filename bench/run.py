"""Measure a model on the harness's own tasks: `python bench/run.py --model gemma4:e4b`.

README's model table rests on two kinds of task: five tool-calling ones - one
call, using the result, exact arguments, two calls in order, and correctly
calling nothing - and two edits: creating a file, and fixing a function halfway
down a 5,700-character file. They used to be run by hand, a few times each,
which is enough to tell "works" from "does not" and not enough to tell whether
a change to the harness helped. This runs each of them N times through
`aetheris -p --json --auto-allow`, grades every run from what actually happened
- the files on disk, the tool calls made, the answer given - and saves the
lot as JSON, so a later run can be held against it with `--compare`.

Each run gets a fresh, empty working directory and a throwaway harness home,
so runs cannot see each other's files, sessions or memories. The home holds
only what is needed: which model to use, the terms already agreed (running this
is agreeing), and the settings that keep a run self-contained - no session
history, no agent channel, no MCP servers.

With `--seed S`, run i is sent seed S + i: repeatable, and still N different
samples rather than one sample N times.

Ollama only, unless `--home` points at an existing harness home whose connected
provider should be measured instead - a hosted key lives in the keyring under
that home's path, so a copy of the home would not find it.
"""
import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "bench", "fixtures")
RESULTS = os.path.join(ROOT, "bench", "results")


# ---------------------------------------------------------------------------
# the tasks
# ---------------------------------------------------------------------------
#
# Each one: what to put in the working directory, what to ask, and how to tell
# from the report and the directory afterwards whether it was done. A grader
# returns (passed, why); `why` is what a person reading a failure needs first.

def _calls(report, name=None, outcome=None):
    return [call for call in report.get("tool_calls") or []
            if (name is None or call.get("name") == name)
            and (outcome is None or call.get("outcome") == outcome)]


def _read(work, name):
    try:
        with open(os.path.join(work, name), encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def _run_python(work, code, timeout=20):
    """Run `code` in `work` in a separate interpreter. (ok, output).

    Isolated (-I), so nothing from this environment's PYTHONPATH stands in for
    the file the model wrote - which also takes the working directory off the
    path, so it is put back explicitly.
    """
    code = "import sys; sys.path.insert(0, '.')\n" + code
    try:
        done = subprocess.run([sys.executable, "-I", "-c", code], cwd=work,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "timed out"
    return done.returncode == 0, (done.stdout + done.stderr).strip()[-300:]


def setup_listing(work):
    for name in ("alpha.txt", "beta.txt"):
        with open(os.path.join(work, name), "w", encoding="utf-8") as handle:
            handle.write("x\n")


def grade_listing(report, work):
    if not [c for c in _calls(report, outcome="ok")]:
        return False, "no tool call succeeded"
    text = report.get("text", "")
    missing = [n for n in ("alpha.txt", "beta.txt") if n not in text]
    return (not missing), (f"answer does not name {', '.join(missing)}" if missing else "")


def setup_secret(work):
    with open(os.path.join(work, "secret.txt"), "w", encoding="utf-8") as handle:
        handle.write("The code is 48213.\n")


def grade_secret(report, work):
    if not _calls(report, "read_file", "ok") and not _calls(report, outcome="ok"):
        return False, "the file was never read"
    return ("48213" in report.get("text", "")), "answer does not give the number"


def grade_exact(report, work):
    body = _read(work, "greeting.txt")
    if body is None:
        return False, "greeting.txt was not written"
    return (body.strip() == "Hello, Aetheris!"), f"content was {body[:60]!r}"


def setup_two_calls(work):
    with open(os.path.join(work, "name.txt"), "w", encoding="utf-8") as handle:
        handle.write("Mirae\n")


def grade_two_calls(report, work):
    body = _read(work, "hello.txt")
    if body is None:
        return False, "hello.txt was not written"
    if "Hello, Mirae" not in body:
        return False, f"content was {body[:60]!r}"
    names = [call.get("name") for call in report.get("tool_calls") or []]
    reads = [i for i, n in enumerate(names) if n == "read_file"]
    writes = [i for i, n in enumerate(names) if n in ("write_file", "edit_file")]
    if reads and writes and reads[0] > writes[-1]:
        return False, "wrote before it read"
    return True, ""


def grade_no_call(report, work):
    calls = report.get("tool_calls") or []
    if calls:
        return False, f"called {', '.join(c.get('name', '?') for c in calls)}"
    return ("51" in report.get("text", "")), "answer is not 51"


def grade_create(report, work):
    if _read(work, "fizzbuzz.py") is None:
        return False, "fizzbuzz.py was not written"
    ok, output = _run_python(work, (
        "from fizzbuzz import fizzbuzz\n"
        "assert [fizzbuzz(n) for n in (1, 3, 5, 15, 7, 30)] == "
        "['1', 'Fizz', 'Buzz', 'FizzBuzz', '7', 'FizzBuzz'], "
        "[fizzbuzz(n) for n in (1, 3, 5, 15, 7, 30)]\n"))
    return ok, output


def setup_fix(work):
    shutil.copy(os.path.join(FIXTURES, "stats.py"), os.path.join(work, "stats.py"))


def grade_fix(report, work):
    body = _read(work, "stats.py")
    if body is None:
        return False, "stats.py is gone"
    # Every function is still there: a "fix" that rewrote the file down to the
    # one function asked about is a different failure, and a common one.
    with open(os.path.join(FIXTURES, "stats.py"), encoding="utf-8") as handle:
        wanted = [line.split("(")[0] for line in handle if line.startswith("def ")]
    lost = [name[4:] for name in wanted if name + "(" not in body]
    if lost:
        return False, f"lost {', '.join(lost)}"
    ok, output = _run_python(work, (
        "from stats import median, mean, percentile, describe\n"
        "assert median([1, 2, 3, 4]) == 2.5, median([1, 2, 3, 4])\n"
        "assert median([3, 1, 2]) == 2, median([3, 1, 2])\n"
        "assert median([5, 1]) == 3, median([5, 1])\n"
        "assert mean([1, 2, 3]) == 2\n"
        "assert percentile([1, 2, 3, 4], 0.5) == 2\n"
        "assert describe([1, 2, 3, 4])['median'] == 2.5\n"))
    return ok, output


TASKS = [
    {"name": "one_call", "kind": "tool",
     "setup": setup_listing, "grade": grade_listing,
     "prompt": "Which files are in the directory {dir}? Look, then list their names."},
    {"name": "use_result", "kind": "tool",
     "setup": setup_secret, "grade": grade_secret,
     "prompt": "Read {dir}/secret.txt and tell me the number it contains."},
    {"name": "exact_args", "kind": "tool",
     "setup": None, "grade": grade_exact,
     "prompt": "Create the file {dir}/greeting.txt. Its whole content must be exactly: "
               "Hello, Aetheris!"},
    {"name": "two_calls", "kind": "tool",
     "setup": setup_two_calls, "grade": grade_two_calls,
     "prompt": "Read the name in {dir}/name.txt, then create {dir}/hello.txt containing "
               "'Hello, ' followed by that name."},
    {"name": "no_call", "kind": "tool",
     "setup": None, "grade": grade_no_call,
     "prompt": "What is 17 times 3? Answer directly, without using any tool."},
    {"name": "create_file", "kind": "edit",
     "setup": None, "grade": grade_create,
     "prompt": "Create {dir}/fizzbuzz.py with a function fizzbuzz(n) that returns 'Fizz' "
               "when n is divisible by 3, 'Buzz' when divisible by 5, 'FizzBuzz' when "
               "divisible by both, and otherwise str(n)."},
    {"name": "fix_function", "kind": "edit",
     "setup": setup_fix, "grade": grade_fix,
     "prompt": "In {dir}/stats.py the function median returns the wrong value for a list "
               "with an even number of values. Fix it, and change nothing else."},
]


# ---------------------------------------------------------------------------
# running
# ---------------------------------------------------------------------------

def make_home(model: str, seed, temperature) -> str:
    """A harness home that holds only what one run needs."""
    home = tempfile.mkdtemp(prefix="bench-home-")
    settings = {"SAVE_CHAT_HISTORY": False, "CHANNEL_ENABLED": False,
                "MCP_ENABLED": False, "AUTO_TITLE": False}
    if seed is not None:
        settings["OLLAMA_SEED"] = seed
    if temperature is not None:
        settings["OLLAMA_TEMPERATURE"] = temperature
    with open(os.path.join(home, "settings.json"), "w", encoding="utf-8") as handle:
        json.dump(settings, handle)
    with open(os.path.join(home, "providers.json"), "w", encoding="utf-8") as handle:
        json.dump({"active": "ollama", "providers": {"ollama": {"model": model}}}, handle)
    return home


def run_once(task: dict, run: int, args) -> dict:
    work = tempfile.mkdtemp(prefix=f"bench-{task['name']}-")
    seed = None if args.seed is None else args.seed + run
    home = args.home or make_home(args.model, seed, args.temperature)
    if task["setup"]:
        task["setup"](work)
    prompt = task["prompt"].format(dir=work.replace("\\", "/"))
    environment = dict(os.environ, AETHERIS_HOME=home, AETHERIS_ACCEPT_TERMS="1",
                       PYTHONPATH=ROOT, PYTHONIOENCODING="utf-8")
    started = time.time()
    record = {"task": task["name"], "kind": task["kind"], "run": run, "seed": seed}
    try:
        done = subprocess.run([sys.executable, "-m", "aetheris", "-p", prompt, "--json",
                               "--auto-allow"], cwd=work, env=environment,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=args.timeout)
        lines = [line for line in done.stdout.splitlines() if line.strip()]
        report = json.loads(lines[-1]) if lines else {}
        error = report.get("error") or ("" if lines else done.stderr.strip()[-300:])
    except subprocess.TimeoutExpired:
        report, error = {}, f"timed out after {args.timeout}s"
    except ValueError:
        report, error = {}, "stdout was not JSON"
    if error:
        passed, why = False, error
    else:
        try:
            passed, why = task["grade"](report, work)
        except Exception as problem:                    # a grader bug is a result too
            passed, why = False, f"grader raised {type(problem).__name__}: {problem}"
    record.update({
        "passed": bool(passed), "why": "" if passed else why,
        "seconds": round(time.time() - started, 2),
        "tokens": (report.get("tokens") or {}).get("total", 0),
        "requests": (report.get("tokens") or {}).get("requests", 0),
        "tool_calls": [call.get("name") for call in report.get("tool_calls") or []],
        "text": (report.get("text") or "")[:400],
    })
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
        if not args.home:
            shutil.rmtree(home, ignore_errors=True)
    else:
        record["workdir"] = work
    return record


def summarise(runs: list) -> dict:
    """Per task: passes out of runs, and the median seconds and tokens."""
    tasks = {}
    for record in runs:
        tasks.setdefault(record["task"], []).append(record)
    summary = {}
    for name, records in tasks.items():
        summary[name] = {
            "passed": sum(r["passed"] for r in records), "runs": len(records),
            "median_seconds": round(statistics.median(r["seconds"] for r in records), 2),
            "median_tokens": int(statistics.median(r["tokens"] for r in records)),
        }
    summary["all"] = {
        "passed": sum(r["passed"] for r in runs), "runs": len(runs),
        "median_seconds": round(statistics.median(r["seconds"] for r in runs), 2) if runs else 0,
        "median_tokens": int(statistics.median(r["tokens"] for r in runs)) if runs else 0,
    }
    return summary


def compare(before: dict, after: dict) -> str:
    """A table of what changed between two result files."""
    rows = [f"{'task':<14} {'before':>9} {'after':>9}  {'Δ pass':>7}  "
            f"{'sec':>13}  {'tokens':>15}"]
    old, new = before.get("summary", {}), after.get("summary", {})
    for name in [n for n in new if n != "all"] + ["all"]:
        a, b = old.get(name), new.get(name)
        if not b:
            continue
        if not a:
            rows.append(f"{name:<14} {'-':>9} {b['passed']:>4}/{b['runs']:<4}")
            continue
        rate = lambda s: s["passed"] / s["runs"] if s["runs"] else 0  # noqa: E731
        delta = (rate(b) - rate(a)) * 100
        rows.append(f"{name:<14} {a['passed']:>4}/{a['runs']:<4} {b['passed']:>4}/{b['runs']:<4} "
                    f"{delta:>+6.0f}%  {a['median_seconds']:>6}→{b['median_seconds']:<6} "
                    f"{a['median_tokens']:>7}→{b['median_tokens']:<7}")
    return "\n".join(rows)


def _git_head() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True).stdout.strip()
    except OSError:
        return ""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the harness's own tasks against a model.")
    parser.add_argument("--model", default="gemma4:e4b", help="the Ollama model (gemma4:e4b)")
    parser.add_argument("-n", "--runs", type=int, default=3, help="runs per task (3)")
    parser.add_argument("--tasks", default="", help="comma-separated task names; all by default")
    parser.add_argument("--seed", type=int, default=None,
                        help="run i is sent seed SEED + i, for repeatable runs")
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--timeout", type=int, default=300, help="seconds per run (300)")
    parser.add_argument("--home", default="", help="an existing harness home to run under, "
                        "instead of a throwaway one - for measuring a hosted provider")
    parser.add_argument("--label", default="", help="a note saved with the results")
    parser.add_argument("--out", default="", help="where to save the JSON")
    parser.add_argument("--compare", default="", help="an earlier result file to compare with")
    parser.add_argument("--keep", action="store_true", help="keep each run's working directory")
    parser.add_argument("--list", action="store_true", help="list the tasks and stop")
    args = parser.parse_args(argv)

    if args.list:
        for task in TASKS:
            print(f"{task['name']:<14} {task['kind']:<5} {task['prompt']}")
        return 0
    chosen = [t for t in TASKS if not args.tasks or t["name"] in args.tasks.split(",")]
    if not chosen:
        print(f"no task matches {args.tasks!r}; --list shows them", file=sys.stderr)
        return 2

    runs = []
    total = len(chosen) * args.runs
    for run in range(args.runs):
        for task in chosen:
            record = run_once(task, run, args)
            runs.append(record)
            mark = "pass" if record["passed"] else "FAIL"
            print(f"[{len(runs):>3}/{total}] {mark} {task['name']:<14} run {run + 1}  "
                  f"{record['seconds']:>6.1f}s  {record['tokens']:>6} tok  "
                  f"{','.join(record['tool_calls']) or '-'}"
                  + (f"  ({record['why'][:100]})" if not record["passed"] else ""),
                  flush=True)

    result = {
        "meta": {"model": args.model if not args.home else f"(home {args.home})",
                 "runs_per_task": args.runs, "seed": args.seed,
                 "temperature": args.temperature, "commit": _git_head(),
                 "date": time.strftime("%Y-%m-%d %H:%M:%S"), "label": args.label},
        "summary": summarise(runs),
        "runs": runs,
    }
    out = args.out or os.path.join(
        RESULTS, f"{time.strftime('%Y%m%d-%H%M%S')}-{args.model.replace(':', '_').replace('/', '_')}.json")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)

    print()
    for name, row in result["summary"].items():
        print(f"{name:<14} {row['passed']:>3}/{row['runs']:<3}  median "
              f"{row['median_seconds']}s, {row['median_tokens']} tokens")
    print(f"\nsaved {out}")
    if args.compare:
        with open(args.compare, encoding="utf-8") as handle:
            print("\n" + compare(json.load(handle), result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
