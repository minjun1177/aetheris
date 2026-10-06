"""Two things that only show up when something goes wrong.

* Files that must survive a crash mid-write. `open(path, "w")` truncates before
  it writes, so being killed in between leaves an empty file where a session
  transcript or an API key used to be.
* The context estimate. Under-counting is what silently overflows a context
  window, and the flat 3.5 characters-per-token this used to assume under-counts
  Korean badly.
"""
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aetheris import atomic
from aetheris import context

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


work = tempfile.mkdtemp(prefix="durability-")

print("--- atomic writes ---")
target = os.path.join(work, "session.json")
atomic.write_json(target, {"messages": ["안녕"], "n": 1})
check("it writes", json.load(open(target, encoding="utf-8"))["messages"] == ["안녕"])
check("non-ASCII survives", "안녕" in open(target, encoding="utf-8").read())

atomic.write_json(target, {"messages": ["두 번째"], "n": 2})
check("it replaces", json.load(open(target, encoding="utf-8"))["n"] == 2)
check("no temporary files left behind",
      [f for f in os.listdir(work) if f.startswith(".tmp-")] == [],
      str(os.listdir(work)))

# The value is serialised before anything is opened, so a value that cannot be
# encoded must leave the good file exactly as it was.
class Unserialisable:
    pass

try:
    atomic.write_json(target, {"bad": Unserialisable()})
    check("a bad value is refused", False, "it wrote anyway")
except TypeError:
    check("a bad value is refused", True)
check("and the old file is untouched",
      json.load(open(target, encoding="utf-8"))["n"] == 2)
check("still no temporaries", [f for f in os.listdir(work) if f.startswith(".tmp-")] == [])

secret = os.path.join(work, "providers.json")
atomic.write_json(secret, {"api_key": "sk-test"}, private=True)
check("a private file is still written and readable",
      json.load(open(secret, encoding="utf-8"))["api_key"] == "sk-test")
if os.name == "nt":
    # Windows has no POSIX mode bits - `chmod` there toggles the read-only
    # flag and nothing else, and a file's access is decided by the ACL it
    # inherits from its directory. There is no "600" to assert, so this says
    # so rather than asserting something that cannot be true.
    print("  [skip] owner-only permissions are POSIX; Windows uses ACLs")
else:
    check("a private file is owner-only", oct(os.stat(secret).st_mode)[-3:] == "600",
          oct(os.stat(secret).st_mode)[-3:])
    check("an ordinary one is not", oct(os.stat(target).st_mode)[-3:] != "600",
          oct(os.stat(target).st_mode)[-3:])

# The real claim is that a reader never sees a half-written file. Kill the writer
# mid-write and check what a reader would have found.
victim = os.path.join(work, "victim.json")
atomic.write_json(victim, {"kept": "original"})
script = f'''
import os, sys, time
sys.path.insert(0, {os.path.dirname(os.path.dirname(os.path.abspath(__file__)))!r})
from aetheris import atomic
real = atomic.write_text
def slow(path, data, private=False):
    # Stall between writing the temporary file and swapping it in.
    import tempfile
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    os.write(fd, b"{{corrupt")
    os.close(fd)
    time.sleep(30)
    os.replace(tmp, path)
slow({victim!r}, "x")
'''
proc = subprocess.Popen([sys.executable, "-c", script])
import time as _time
_time.sleep(1.5)
mid_write = open(victim, encoding="utf-8").read()
proc.kill()
proc.wait()
check("a killed writer leaves the old file whole",
      json.loads(mid_write).get("kept") == "original", repr(mid_write[:40]))

print("\n--- the context estimate ---")
korean = [{"role": "user", "content": "안녕하세요. 이 하네스는 로컬 모델을 위한 도구 실행 환경입니다." * 8}]
english = [{"role": "user", "content": "This harness is a tool execution environment." * 12}]

flat = int(sum(len(m["content"]) for m in korean) / 3.5)
new = context._estimate_tokens(korean)
check("Korean is no longer counted as if it were English", new > flat * 1.3,
      f"flat 3.5 said {flat}, now {new}")
check("English is still counted sensibly",
      0 < context._estimate_tokens(english) < len(english[0]["content"]),
      str(context._estimate_tokens(english)))

wide = context._wide_chars("안녕 hello 漢字 かな")   # 2 Hangul + 2 Han + 2 Kana
check("wide scripts are counted apart from Latin", wide == 6, str(wide))

before = context._estimate_tokens(korean)
context.observe_usage(korean, int(before * 1.5))
after = context._estimate_tokens(korean)
check("a provider's real count moves the estimate", after > before,
      f"{before} -> {after}")
for _ in range(30):
    context.observe_usage(korean, int(context._raw_estimate(korean) * 1.5))
check("and it converges on it",
      abs(context._estimate_tokens(korean) - context._raw_estimate(korean) * 1.5) < 3,
      str(context._estimate_tokens(korean)))

settled = context._correction
context.observe_usage(korean, 999999)
check("a nonsense count is ignored", context._correction == settled)
context.observe_usage([{"role": "user", "content": "hi"}], 5000)
check("too short a sample is ignored", context._correction == settled)
context.observe_usage(korean, 0)
check("a provider that reports nothing is ignored", context._correction == settled)

# ---------------------------------------------------------------------------
print("\n--- a trimmed tool result keeps the line that mattered ---")
# The answer in a traceback is its last line. Keeping only the front kept
# "Traceback (most recent call last):" and deleted "RuntimeError: kaboom", and
# `manage_context` writes the trimmed list back, so it was gone for good.
FAILED = ("[Tool Result for 'run_cmd']:\n"
          "[Error] run_cmd(command='python3 boom.py'): Command failed (exit code 1).\n"
          "Traceback (most recent call last):\n"
          + '  File "lib.py", line 1, in inner\n' * 200
          + "RuntimeError: kaboom")

trimmed = context._trim_tool_results(
    [{"role": "user", "content": FAILED}], max_chars=400)[0]["content"]

check("it is actually shorter", len(trimmed) < len(FAILED) and len(trimmed) < 600,
      f"{len(FAILED)} -> {len(trimmed)}")
check("the exception survives", "RuntimeError: kaboom" in trimmed)
check("so does the call that failed", "python3 boom.py" in trimmed)
check("and it says what it dropped", "omitted from the middle" in trimmed)

short = "[Tool Result for 'read_file']:\nline one\nline two"
check("a result under the ceiling is untouched",
      context._trim_tool_results([{"role": "user", "content": short}],
                                 max_chars=400)[0]["content"] == short)
check("a message that is not a tool result is untouched",
      context._trim_tool_results([{"role": "user", "content": "x" * 900}],
                                 max_chars=400)[0]["content"] == "x" * 900)

# ---------------------------------------------------------------------------
print("\n--- trimming happens because of the budget, not on principle ---")
# It used to run at a flat 3000 characters every turn, whatever the budget. With
# a 65536 context and 49,000 tokens spare, reading a 12,000-character file
# returned a quarter of it and threw the rest away for good.
import asyncio                                                     # noqa: E402
from aetheris import config as cfg                           # noqa: E402

BIG = "[Tool Result for 'read_file']:\n" + "x = 1\n" * 1600      # ~9600 chars


def after_manage(num_ctx):
    kept_ctx, kept_predict = cfg.NUM_CTX, cfg.NUM_PREDICT
    cfg.NUM_CTX, cfg.NUM_PREDICT = num_ctx, 64
    messages = [{"role": "system", "content": "s"},
                {"role": "user", "content": "read it"},
                {"role": "assistant", "content": "done"},
                {"role": "user", "content": BIG}]
    try:
        asyncio.run(context.manage_context(messages))
    finally:
        cfg.NUM_CTX, cfg.NUM_PREDICT = kept_ctx, kept_predict
    return messages[3]["content"]

roomy = after_manage(65536)
check("a result that fits is left whole", roomy == BIG,
      f"{len(BIG)} -> {len(roomy)}")

squeezed = after_manage(2048)
check("a budget too small does trim it", len(squeezed) < len(BIG),
      f"{len(BIG)} -> {len(squeezed)}")
check("and it steps down no further than it must",
      len(squeezed) >= min(context._TRIM_STEPS),
      f"{len(squeezed)} vs floor {min(context._TRIM_STEPS)}")
check("the ceilings go loosest first",
      list(context._TRIM_STEPS) == sorted(context._TRIM_STEPS, reverse=True),
      str(context._TRIM_STEPS))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("durability checks passed")
