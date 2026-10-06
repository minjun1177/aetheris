"""What a tool result is allowed to say about itself, and what stays off stderr.

Two ways a working tool used to look like a broken one:

* The footer under a result searched the whole body for `[Error]`. A page
  `get_url` fetched that discusses an error, a file whose source raises one, a
  `grep` that matched the word - all came back complete and were labelled a
  failure. The markers are anchors, and this checks they are read as anchors.
* Beautiful Soup writes a paragraph to stderr when a page turns out to be XML,
  or when markup is short enough to look like a filename - which a search
  snippet often is. It is printed from the fetch threads, mid-search, and reads
  like the tool failed while the content comes back fine.
"""
import io
import os
import sys
import warnings
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aetheris import paths

HOME = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".reporting-test-home")
os.environ[paths.ENV_VAR] = HOME

from aetheris import config          # noqa: E402
config.MCP_ENABLED = False
config.SAVE_CHAT_HISTORY = False
from aetheris import tui             # noqa: E402
from aetheris import tools           # noqa: E402
from aetheris import llm_client      # noqa: E402
from aetheris import websearch       # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


def footer(result):
    """The word `_fmt_tool_result` puts under a result."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        tui._fmt_tool_result("some_tool", result)
    text = buf.getvalue()
    for word in ("error", "refused", "done"):
        if f"╰─ {word}" in text:
            return word
    return "?"


print("--- the markers are one string, in one place ---")
check("tools writes the error marker config names",
      tools._ERROR_PREFIX == config.TOOL_ERROR_PREFIX, tools._ERROR_PREFIX)
check("llm_client counts the refusal marker config names",
      llm_client.REFUSAL_PREFIX == config.TOOL_REFUSAL_PREFIX, llm_client.REFUSAL_PREFIX)

print("\n--- a failure still reads as one ---")
check("a plain error", footer("[Error] Cannot fetch URL: timed out") == "error")
check("an error with output after it",
      footer("[Error] Command failed (exit code 1).\nmake: *** [all] Error 2") == "error")
check("a named error, after tools._name_the_failure has rewritten it",
      footer(tools._name_the_failure("read_file", {"filepath": "x.py"},
                                     "[Error] No such file")) == "error")

print("\n--- a refusal reads as a refusal, whichever kind ---")
for refusal in ("[System] User denied file write.",
                "[System] 'write_file' is blocked by your permission rules (rule: r).",
                "[System] 'edit_file' is not available during this stage."):
    check(f"{refusal[:38]}…", footer(refusal) == "refused")

print("\n--- a result that merely mentions one does not ---")
# Each of these is a real thing a tool hands back: a fetched page, a source
# file, command output, a directory listing.
mentions = {
    "a page get_url fetched": "Troubleshooting\nThe server replies [Error] when the token expires.",
    "a source file read_file read": "def f():\n    return \"[Error] not found\"\n",
    "output from a grep run_cmd ran": "tools.py:302:    return f\"[Error] Cannot fetch URL: {e}\"",
    "a directory listing": "notes.txt (File)\n[Error] samples/ (Dir)",
    "a search result quoting one": "1. Fixing [Error] 0x80070005\n   https://example.com/a",
    "text that mentions a denial": "The docs say [System] User denied appears in the log.",
}
for label, body in mentions.items():
    check(label, footer(body) == "done", footer(body))

print("\n--- a marker only counts at the front ---")
check("the error marker indented is not an error",
      footer("  [Error] Cannot fetch URL: timed out") == "done")
check("and llm_client agrees about refusals",
      not "The log said [System] once".startswith(llm_client.REFUSAL_PREFIX))

print("\n--- long results are judged whole, not by their preview ---")
# The body shown is capped at 600 characters; the verdict must not be.
check("an error past the preview cap is still an error",
      footer("[Error] " + "x" * 2000) == "error")
check("a mention past the preview cap is still not one",
      footer("y" * 2000 + "[Error] mentioned here") == "done")

print("\n--- Beautiful Soup keeps its guesses off stderr ---")
cases = {
    "an RSS feed": '<?xml version="1.0"?><rss version="2.0"><channel>'
                   '<title>News</title></channel></rss>',
    "a sitemap": '<?xml version="1.0" encoding="UTF-8"?><urlset>'
                 '<url><loc>https://example.com/</loc></url></urlset>',
    "a snippet that looks like a filename": "report.txt",
    "a snippet that is just a URL": "https://example.com/some/path",
    "an empty body": "",
}
for label, markup in cases.items():
    err = io.StringIO()
    with redirect_stderr(err):
        with warnings.catch_warnings():
            warnings.simplefilter("always")     # the loudest setting there is
            text = websearch.strip_html(markup)
    check(f"{label} prints nothing", err.getvalue() == "", repr(err.getvalue()[:120]))
    if markup:
        check(f"{label} still returns its text", bool(text), repr(text[:40]))

print("\n--- and the search client is built without a rename notice ---")
err = io.StringIO()
with redirect_stderr(err):
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        before = list(warnings.filters)
        try:
            client = websearch._ddgs_client()
        except ImportError:
            client = None
            print("  [skip] neither ddgs nor duckduckgo_search is installed")
        after = list(warnings.filters)
if client is not None:
    check("building it prints nothing", err.getvalue() == "", repr(err.getvalue()[:120]))
    check("and it leaves the warning filters as it found them", before == after)
    check("and it is usable", hasattr(client, "text"))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("tool reporting checks passed")
