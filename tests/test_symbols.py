"""`find_symbol`: definitions across the project from a Tree-sitter symbol table.

What `search_in_file` cannot do: tell a definition from a call, a comment or a
string, and say which class a method belongs to. The table is cached per file
and must notice a file that changed.
"""
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AETHERIS_HOME"] = tempfile.mkdtemp(prefix="symbols-home-")
from aetheris import config   # noqa: E402
from aetheris import symbols, tools, toolspec   # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


def write(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


check("find_symbol is a tool", toolspec.get("find_symbol") is not None)
check("and is read-only", "find_symbol" not in tools._CHANGES_THINGS)

if not config.TREE_SITTER_AVAILABLE:
    result = tools.handle_find_symbol("x")
    check("without Tree-sitter it says how to get it", "aetheris[ast]" in result, result)
    print("\n(Tree-sitter is not installed; the index itself was not exercised.)")
else:
    origin = os.getcwd()
    work = tempfile.mkdtemp(prefix="symbols-work-")
    os.chdir(work)
    subprocess.run(["git", "init", "-q"], check=True)
    write("pkg/rules.py", (
        "class Loader:\n"
        "    def load_rules(self, force: bool = False) -> dict:\n"
        "        return {}\n"
        "\n"
        "def load_rules(path):\n"
        "    # load_rules is mentioned in a comment\n"
        "    text = 'load_rules in a string'\n"
        "    return path\n"))
    write("pkg/use.py", "from pkg.rules import load_rules\n\nload_rules('x')\n")
    write("web/app.js", "function loadRules(a, b) { return a; }\nloadRules(1, 2);\n")
    write("ignored/gen.py", "def load_rules():\n    pass\n")
    write(".gitignore", "ignored/\n")

    print("--- definitions, not mentions ---")
    found, cut = symbols.find("load_rules")
    places = sorted((d["file"], d["line"]) for d in found)
    check("both definitions are found, each where it starts",
          places == [("pkg/rules.py", 2), ("pkg/rules.py", 5)], str(places))
    check("a file .gitignore excludes is not indexed",
          not any(d["file"].startswith("ignored/") for d in found))
    method = next(d for d in found if d["line"] == 2)
    check("a method says which class it is in", method["container"] == "Loader", str(method))
    check("with its signature", method["signature"] == "load_rules(self, force: bool = False) -> dict",
          method["signature"])
    check("and its line range", method["end_line"] == 3, str(method))
    check("the whole project was indexed", not cut)

    print("\n--- the tool's answer ---")
    result = tools.handle_find_symbol("load_rules", False)
    check("it reports both", result.startswith("[Found] 2 definition(s)")
          and "pkg/rules.py:2-3" in result and "(in Loader)" in result, result)
    check("and no references unless asked", "[References]" not in result)
    result = tools.handle_find_symbol("load_rules", "true")
    check("references are the uses, not the definitions",
          "pkg/use.py:1:" in result and "pkg/use.py:3:" in result
          and "pkg/rules.py:5:" not in result.split("[References]")[1], result)
    check("and not a comment or a string",
          "comment" not in result and "in a string" not in result, result)
    result = tools.handle_find_symbol("loadRules", True)
    check("other languages are indexed too", "web/app.js:1-1" in result
          and "web/app.js:2:" in result, result)
    result = tools.handle_find_symbol("load_*")
    check("a glob matches by pattern", "pkg/rules.py:5" in result, result)
    result = tools.handle_find_symbol("loadrule")
    check("a near miss offers names containing it", "Nothing is named exactly" in result
          and "load_rules" in result, result)
    result = tools.handle_find_symbol("nowhere_at_all")
    check("nothing found says what to do instead", "search_in_file" in result, result)
    check("an empty name is an error", tools.handle_find_symbol("").startswith("[Error]"))

    print("\n--- a symlink out of the project is not read ---")
    outside = tempfile.mkdtemp(prefix="symbols-outside-")
    write(os.path.join(outside, "secret.py"), "def load_rules():\n    SECRET_VALUE = 1\n")
    try:
        os.symlink(os.path.join(outside, "secret.py"), os.path.join(work, "pkg", "leak.py"))
        check("its definitions are not indexed",
              not any(d["file"] == "pkg/leak.py" for d in symbols.find("load_rules")[0]))
        check("and its lines are never quoted",
              "SECRET_VALUE" not in tools.handle_find_symbol("load_rules", True))
        os.remove(os.path.join(work, "pkg", "leak.py"))
    except (OSError, NotImplementedError):
        pass

    print("\n--- a large project is answered in time, and the next question goes further ---")
    symbols.forget()
    saved_budget = symbols.INDEX_SECONDS
    symbols.INDEX_SECONDS = 0.0
    found, note = symbols.find("load_rules")
    check("past the budget it stops and says so", "not indexed" in note, note)
    check("the tool passes that on", "Incomplete" in tools.handle_find_symbol("load_rules"))
    symbols.INDEX_SECONDS = saved_budget
    found, note = symbols.find("load_rules")
    check("with time to spare it finishes", note == "" and found, note)

    print("\n--- the table follows the files ---")
    time.sleep(0.01)
    write("pkg/rules.py", "def renamed():\n    pass\n")
    found, _ = symbols.find("load_rules")
    check("a changed file is parsed again",
          [d["file"] for d in found if d["file"] == "pkg/rules.py"] == [], str(found))
    check("and its new definitions are there", symbols.find("renamed")[0])
    os.chdir(origin)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("symbol checks passed")
