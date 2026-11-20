"""`spawn_agent` with a worktree of its own, and on a model the assistant picks.

Driven by a scripted provider. What is checked: a sub-agent given
`isolation="worktree"` works in a checkout of its own, its edits end up on a
branch and nowhere in the main working tree, the checkout is taken away
afterwards and the branch only kept when there is something on it; one that
cannot have a worktree is not started at all; the working directory always
comes back. And `model`: `provider:model` reaches another provider for the
length of the job and nothing saved changes, an unusable one is refused
before any request, and `SUBAGENT_MODELS` is the menu the system prompt shows.
"""
import io
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AETHERIS_HOME"] = tempfile.mkdtemp(prefix="isolation-home-")
from aetheris import config          # noqa: E402
config.AUTO_ALLOW = True
config.MCP_ENABLED = False
config.CHANNEL_ENABLED = False
config.SAVE_CHAT_HISTORY = False
config.AUTO_VERIFY = False
config.SUBAGENT_MAX_TURNS = 4

from aetheris import providers, subagent, tools   # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


class Scripted(providers.Provider):
    name = "scripted"
    label = "Scripted"
    needs_key = False

    def __init__(self, replies, model="scripted-1"):
        super().__init__({"model": model})
        self.replies = list(replies)
        self.cwds = []

    def ready(self):
        return ""

    async def stream(self, messages, max_tokens=None, tools=None):
        self.cwds.append(os.getcwd())
        self.brief = messages[1]["content"] if len(messages) > 1 else ""
        reply = self.replies.pop(0) if self.replies else "done."
        yield {"text": reply}
        yield {"done": True, "prompt_tokens": 1, "completion_tokens": 1,
               "total_seconds": 0.0, "eval_seconds": 0.0}


def hire(replies, **kwargs):
    saved = providers._active
    scripted = Scripted(replies)
    providers._active = scripted
    out, real = io.StringIO(), sys.stdout
    sys.stdout = out
    try:
        report = subagent.run(kwargs.pop("task", "do the job"), **kwargs)
    finally:
        sys.stdout = real
        providers._active = saved
    return report, scripted, out.getvalue()


def same(a, b):
    """Two spellings of one directory - a runner's temp path is a symlink or 8.3 name."""
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def git(*args, cwd):
    return subprocess.run(("git",) + args, cwd=cwd, capture_output=True, text=True)


WRITE = ('<tool_call>\n{"name": "write_file", "arguments": {"filepath": "made.txt"}}\n'
         '<content>\nfrom the sub-agent\n</content>\n</tool_call>')

repo = tempfile.mkdtemp(prefix="isolation-repo-")
git("init", "-q", cwd=repo)
git("config", "user.email", "t@example.com", cwd=repo)
git("config", "user.name", "t", cwd=repo)
open(os.path.join(repo, "a.txt"), "w").write("a\n")
git("add", ".", cwd=repo)
git("commit", "-qm", "init", cwd=repo)
os.chdir(repo)
before_head = git("rev-parse", "HEAD", cwd=repo).stdout.strip()

print("--- a sub-agent in a worktree of its own ---")
report, scripted, _ = hire([WRITE, "Wrote made.txt."], isolation="worktree")
branch = re.search(r"branch (aetheris/agent-[\w.-]+)", report)
check("it worked somewhere else", scripted.cwds and not any(same(c, repo) for c in scripted.cwds),
      str(scripted.cwds))
check("and was told where, and that uncommitted work is not there",
      "worktree of your own" in scripted.brief and "not here" in scripted.brief)
check("the working directory came back", same(os.getcwd(), repo), os.getcwd())
check("nothing it wrote is in the main working tree",
      not os.path.exists(os.path.join(repo, "made.txt")))
check("the main branch did not move",
      git("rev-parse", "HEAD", cwd=repo).stdout.strip() == before_head)
check("the report names the branch its changes are on", branch is not None, report)
if branch:
    shown = git("show", f"{branch.group(1)}:made.txt", cwd=repo)
    check("and the change is on it", shown.returncode == 0
          and "from the sub-agent" in shown.stdout, shown.stderr)
check("with how to see, take or drop it",
      "git diff" in report and "git merge" in report and "git branch -D" in report)
check("the checkout itself is gone",
      git("worktree", "list", cwd=repo).stdout.count("\n") == 1,
      git("worktree", "list", cwd=repo).stdout)

print("\n--- one that changes nothing leaves nothing ---")
branches_before = git("branch", "--list", "aetheris/*", cwd=repo).stdout
report, _, _ = hire(["Nothing needed changing."], isolation="worktree")
check("it says so", "changed no files" in report, report)
check("and its branch is removed too",
      git("branch", "--list", "aetheris/*", cwd=repo).stdout == branches_before)

print("\n--- work left uncommitted is not lost ---")
config.GIT_AUTO_COMMIT = False
report, _, _ = hire([WRITE, "Wrote it."], isolation="worktree")
config.GIT_AUTO_COMMIT = True
branch = re.search(r"branch (aetheris/agent-[\w.-]+)", report)
check("with auto-commit off the edit is still committed to its branch",
      branch is not None and git("show", f"{branch.group(1)}:made.txt", cwd=repo).returncode == 0,
      report)

print("\n--- no worktree, no sub-agent ---")
plain = tempfile.mkdtemp(prefix="isolation-plain-")
os.chdir(plain)
report, scripted, _ = hire([WRITE, "done"], isolation="worktree")
check("outside a repository it is refused", report.startswith("[Error] No worktree"), report)
check("and nothing was started", scripted.cwds == [])
check("and nothing written here", not os.path.exists(os.path.join(plain, "made.txt")))
report, scripted, _ = hire(["done"], isolation="sandbox")
check("an isolation it does not know is refused", report.startswith("[Error] isolation"),
      report)
os.chdir(repo)

print("\n--- the model is the hiring model's choice ---")
check("an Ollama tag stays one model name", providers.split_model("qwen3:4b") == ("", "qwen3:4b"))
check("provider:model names another provider",
      providers.split_model("anthropic:claude-haiku-5-5") == ("anthropic", "claude-haiku-5-5"))
check("and an Ollama tag behind it survives",
      providers.split_model("ollama:qwen3:4b") == ("ollama", "qwen3:4b"))


class Other(Scripted):
    name = "other"
    label = "Other"
    drives_turns = False
    made = []

    def __init__(self, settings=None):
        super().__init__(["From the other provider."], (settings or {}).get("model", ""))
        self.settings = dict(settings or {})
        Other.made.append(self)


providers.PROVIDERS["other"] = Other
providers.settings_for("other")["model"] = "saved-model"
report, scripted, _ = hire(["never used"], model="other:small-1")
check("provider:model runs the job there", report == "From the other provider.", report)
check("on the model named", Other.made and Other.made[-1].model == "small-1")
check("and the assistant's own provider is back afterwards",
      providers.current() is not Other.made[-1])
check("with nothing saved changed",
      providers.settings_for("other")["model"] == "saved-model")


class Unready(Other):
    name = "unready"
    label = "Unready"

    def ready(self):
        return "no API key (UNREADY_KEY)"


providers.PROVIDERS["unready"] = Unready
report, scripted, _ = hire(["never used"], model="unready:x")
check("a provider that cannot be used is refused before any request",
      report.startswith("[Error] The sub-agent was not started") and "API key" in report,
      report)
check("and the job did not run on the assistant's model instead", scripted.cwds == [])
del providers.PROVIDERS["other"], providers.PROVIDERS["unready"]

print("\n--- the menu it chooses from ---")
config.SUBAGENT_MODELS = []
check("no list, no section - the prompt stays as it was",
      subagent.models_prompt_section() == "")
config.SUBAGENT_MODELS = ["ollama:qwen3:4b", "anthropic:claude-haiku-5-5"]
section = subagent.models_prompt_section()
check("a list is shown in its own order",
      section.index("ollama:qwen3:4b") < section.index("anthropic:claude-haiku-5-5"))
check("and is the same bytes every time", section == subagent.models_prompt_section())
config.SUBAGENT_MODELS = []
spec = {t.name: t for t in __import__("aetheris.toolspec", fromlist=["TOOLS"]).TOOLS}
check("spawn_agent says how to name another provider's model",
      "provider:model" in spec["spawn_agent"].params[2].description)
check("and offers isolation", spec["spawn_agent"].params[3].name == "isolation")
check("the handler takes both", tools.handle_spawn_agent.__code__.co_argcount == 4)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("isolation checks passed")
