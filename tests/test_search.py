"""`search_in_file` searches the project, not everything under the directory.

It used to open every file below the working directory. A search for a
function name spent its hundred results on `dist/`, a lock file and a minified
bundle, and a small model reads the first page of results as the answer. So
what is checked is what it leaves out - ignored files, binaries - and the two
ways to narrow or widen it: a glob, and lines of context. ripgrep and the
Python fallback must say the same thing, so both are run where ripgrep exists
(on PATH, or named by AETHERIS_TEST_RG).
"""
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("AETHERIS_HOME", tempfile.mkdtemp(prefix="search-home-"))

from aetheris import config          # noqa: E402
config.MCP_ENABLED = False
config.SAVE_CHAT_HISTORY = False
from aetheris import tools           # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


def write(root, name, text, mode="w"):
    path = os.path.join(root, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as handle:
        handle.write(text)


root = tempfile.mkdtemp(prefix="search-")
subprocess.run(["git", "init", "-q"], cwd=root)
write(root, ".gitignore", "dist/\n*.log\n")
write(root, "src/app.py", "import os\n\ndef find_me():\n    return 1\n\n\n# find_me again\n")
write(root, "src/util.ts", "export function find_me() {}\n")
write(root, "docs/notes.md", "line 1\nline 2\nfind_me in prose\nline 4\nline 5\n")
write(root, "dist/bundle.js", "function find_me(){}" * 3 + "\n")
write(root, "debug.log", "find_me was called\n")
write(root, "blob.bin", b"\x00\x01find_me\x00", mode="wb")
write(root, "untracked_new.py", "find_me = 2\n")      # untracked but not ignored
subprocess.run(["git", "add", ".gitignore", "src", "docs"], cwd=root)

here = os.getcwd()
os.chdir(root)

backends = [("python", lambda: "")]
rg = os.environ.get("AETHERIS_TEST_RG") or shutil.which("rg")
if rg:
    backends.append(("ripgrep", lambda: rg))
else:
    print("  [skip] ripgrep is not installed; only the fallback is checked")

results = {}
real = tools._ripgrep
try:
    for label, which in backends:
        tools._ripgrep = which
        print(f"\n--- {label} ---")
        found = tools.handle_search_in_file("find_me")
        results[label] = found
        check("the project's files are searched", "src/app.py:3:" in found
              and "src/util.ts:1:" in found and "docs/notes.md:3:" in found, found)
        check("an untracked file that is not ignored is too", "untracked_new.py:1:" in found)
        check("what .gitignore excludes is not", "dist/" not in found and "debug.log" not in found)
        check("and neither is a binary", "blob.bin" not in found)

        found = tools.handle_search_in_file("find_me", glob="*.py")
        check("a glob narrows it to matching file names", "src/app.py" in found
              and "util.ts" not in found and "notes.md" not in found, found)
        found = tools.handle_search_in_file("find_me", glob="src/*")
        check("or to a path", "src/app.py" in found and "src/util.ts" in found
              and "notes.md" not in found and "untracked_new" not in found, found)

        found = tools.handle_search_in_file("find_me", glob="docs/*", context=2)
        check("context shows the lines around a match",
              "docs/notes.md-1- line 1" in found and "docs/notes.md:3: find_me in prose" in found
              and "docs/notes.md-5- line 5" in found, found)
        found = tools.handle_search_in_file("find_me", glob="*.py", context=3)
        check("two matches in one stretch are one block, each counted once",
              found.count("src/app.py:3:") == 1 and found.count("src/app.py:7:") == 1
              and "Found 3 match" in found, found)

        found = tools.handle_search_in_file(r"def \w+_me", is_regex=True)
        check("a regex", "src/app.py:3:" in found and "util.ts" not in found, found)
        check("no match says so", "No matches" in tools.handle_search_in_file("zzz_nothing"))
finally:
    tools._ripgrep = real

if len(results) == 2:
    check("ripgrep and the fallback agree", results["python"] == results["ripgrep"],
          f"\n{results['python']}\n---\n{results['ripgrep']}")

print("\n--- limits ---")
tools._ripgrep = lambda: ""
check("a broken regex is an error, not a crash",
      tools.handle_search_in_file("(", is_regex=True).startswith("[Error]"))
check("context is a number", tools.handle_search_in_file("x", context="lots")
      .startswith("[Error]"))
write(root, "src/many.py", "hit\n" * 150)
found = tools.handle_search_in_file("hit", glob="many.py")
check("a hundred matches, then it says there are more", found.count("src/many.py:") == 100
      and "narrow it" in found, found[:120])
tools._ripgrep = real

plain = tempfile.mkdtemp(prefix="search-plain-")
os.chdir(plain)
write(plain, "main.c", "int find_me;\n")
write(plain, "build/out.c", "int find_me;\n")
write(plain, "node_modules/x/index.js", "find_me\n")
tools._ripgrep = lambda: ""
found = tools.handle_search_in_file("find_me")
tools._ripgrep = real
check("outside git, build and vendored directories are skipped by name",
      "main.c" in found and "build/" not in found and "node_modules" not in found, found)

os.chdir(here)
shutil.rmtree(root, ignore_errors=True)
shutil.rmtree(plain, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("search checks passed")
