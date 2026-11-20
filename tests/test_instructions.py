"""Instructions files: the person's own, and one per directory up to the repository root.

A terminal opened in a subdirectory used to see only that directory's
AGENTS.md, and there was no file for how the person works in every project.
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOME = tempfile.mkdtemp(prefix="instructions-home-")
os.environ["AETHERIS_HOME"] = HOME
from aetheris import systemprompt   # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


origin = os.getcwd()
repo = os.path.realpath(tempfile.mkdtemp(prefix="instructions-repo-"))
subprocess.run(["git", "init", "-q", repo], check=True)
deep = os.path.join(repo, "src", "pkg")
os.makedirs(deep)
write(os.path.join(repo, "AGENTS.md"), "ROOT RULE: tabs never")
write(os.path.join(repo, "src", "pkg", "CLAUDE.md"), "PKG RULE: keep it pure")
write(os.path.join(repo, "src", "pkg", "AGENTS.md"), "NOT READ: CLAUDE.md is not first")

print("--- a subdirectory sees the repository's file too ---")
os.chdir(deep)
found = systemprompt.context_files()
check("the root's file and the directory's are both found",
      [p for _, p in found] == [os.path.join(repo, "AGENTS.md"),
                                os.path.join(deep, "AGENTS.md")], str(found))
text = systemprompt.load_context_file()
check("the broadest comes first, the nearest last",
      0 <= text.find("ROOT RULE") < text.find("NOT READ"), text)
check("each is labelled with where it came from",
      f"PROJECT CONVENTIONS ({os.path.join('..', '..', 'AGENTS.md')})" in text, text)

print("\n--- one file per directory, in the documented order ---")
os.remove(os.path.join(deep, "AGENTS.md"))
text = systemprompt.load_context_file()
check("with AGENTS.md gone, the directory's CLAUDE.md is read", "PKG RULE" in text, text)
os.remove(os.path.join(deep, "CLAUDE.md"))
os.makedirs(os.path.join(deep, "CONVENTIONS.md"))
check("a directory named like one is not a file", "CONVENTIONS" not in "".join(
    p for _, p in systemprompt.context_files() if p.startswith(deep)))
os.rmdir(os.path.join(deep, "CONVENTIONS.md"))

print("\n--- the person's own file goes into every project ---")
write(os.path.join(HOME, "AGENTS.md"), "MY RULE: answer in Korean")
found = systemprompt.context_files()
check("it is first", found and found[0] == ("yours", os.path.join(HOME, "AGENTS.md")),
      str(found))
text = systemprompt.load_context_file()
check("and said to be the user's own", "USER'S OWN INSTRUCTIONS" in text
      and text.find("MY RULE") < text.find("ROOT RULE"), text)

print("\n--- outside a repository, only the current directory ---")
plain = os.path.realpath(tempfile.mkdtemp(prefix="instructions-plain-"))
write(os.path.join(plain, "AGENTS.md"), "PLAIN RULE")
inner = os.path.join(plain, "inner")
os.makedirs(inner)
os.chdir(inner)
check("a parent outside any repository is not read",
      "PLAIN RULE" not in systemprompt.load_context_file())
os.chdir(plain)
check("the current directory's is", "PLAIN RULE" in systemprompt.load_context_file())

print("\n--- a huge file is cut, an empty one is skipped ---")
write(os.path.join(plain, "AGENTS.md"), "x" * (systemprompt.CONTEXT_FILE_MAX_CHARS + 500))
text = systemprompt.load_context_file()
check("cut at the ceiling, and says so",
      "cut here" in text and text.count("x") <= systemprompt.CONTEXT_FILE_MAX_CHARS + 5)
write(os.path.join(plain, "AGENTS.md"), "   \n")
os.remove(os.path.join(HOME, "AGENTS.md"))
check("an empty file adds nothing", systemprompt.load_context_file() == "")

os.chdir(origin)
print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("instructions checks passed")
