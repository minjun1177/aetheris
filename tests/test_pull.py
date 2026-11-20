"""`/connect ollama <model>` offers to pull a model the daemon does not have.

Before, it connected and said "run: ollama pull ..." - the one step of a local
setup the harness could have done itself. The daemon is faked: what matters
is what is asked, what is shown, and that the cached answers are dropped.
"""
import contextlib
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AETHERIS_HOME"] = tempfile.mkdtemp(prefix="pull-home-")
os.environ.pop("OLLAMA_HOST", None)
from aetheris import config
config.SAVE_CHAT_HISTORY = False

from aetheris import connect, providers   # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


class FakeDaemon:
    def __init__(self):
        self.installed = {"llama3.2:latest"}
        self.pulled = []
        self.fail_with = None

    def list(self):
        return {"models": [{"model": name, "size": 0} for name in sorted(self.installed)]}

    def show(self, model):
        return {"capabilities": ["completion", "tools"] if model in self.installed else []}

    def pull(self, model, stream=False):
        self.pulled.append((model, stream))
        if self.fail_with:
            raise self.fail_with
        yield {"status": "pulling manifest"}
        yield {"status": "pulling abc", "digest": "sha256:abc", "total": 2000, "completed": 500}
        yield {"status": "pulling abc", "digest": "sha256:abc", "total": 2000, "completed": 2000}
        self.installed.add(model if ":" in model else f"{model}:latest")
        yield {"status": "success"}


daemon = FakeDaemon()
providers.ollama_client = lambda host="", timeout=None: daemon

print("--- knowing when a pull would help ---")
check("a pulled model needs no pull", not providers.ollama_needs_pull("llama3.2", ""))
check("a missing one does", providers.ollama_needs_pull("gemma4:e4b", ""))
providers._ollama_installed.clear()
real_installed = providers._installed_ollama_models
providers._installed_ollama_models = lambda host: None
check("a daemon that is off is not fixed by a pull", not providers.ollama_needs_pull("x", ""))
providers._installed_ollama_models = real_installed

print("\n--- the pull itself ---")
events = []
check("it succeeds", providers.ollama_pull("gemma4:e4b", "",
                                           lambda *e: events.append(e)) == "")
check("streamed, so progress can be shown", daemon.pulled[-1] == ("gemma4:e4b", True))
check("every event reaches the caller, with bytes",
      events[0] == ("pulling manifest", 0, 0) and ("pulling abc", 500, 2000) in events,
      str(events))
check("and the cached answers are dropped, so the model is ready at once",
      providers.ollama_model_problem("gemma4:e4b", "") == "")

daemon.fail_with = RuntimeError("pull model manifest: file does not exist")
check("a failure is returned, not raised",
      "file does not exist" in providers.ollama_pull("nosuch:1b", ""))
daemon.fail_with = None

print("\n--- /connect offers it ---")
answers = []


def answering(reply):
    def ask(prompt, title="", options=(), keyboard_only=False):
        answers.append(title)
        return reply
    return ask


connect._ask = answering("y")
out = io.StringIO()
with contextlib.redirect_stdout(out):
    connect.run("ollama qwen3:4b")
printed = out.getvalue()
check("it asks before downloading", answers == ["Pull qwen3:4b?"], str(answers))
check("pulls what was named", daemon.pulled[-1] == ("qwen3:4b", True))
check("shows the progress", "25%" in printed and "100%" in printed, printed[-300:])
check("and ends connected", "Pulled qwen3:4b" in printed and "Connected: Ollama · qwen3:4b"
      in printed, printed[-300:])

connect._ask = answering("n")
answers.clear()
before = len(daemon.pulled)
out = io.StringIO()
with contextlib.redirect_stdout(out):
    connect.run("ollama phi4:14b")
check("no means nothing is downloaded", len(daemon.pulled) == before)
check("and says how to do it by hand", "ollama pull phi4:14b" in out.getvalue(),
      out.getvalue()[-200:])

answers.clear()
with contextlib.redirect_stdout(io.StringIO()):
    connect.run("ollama llama3.2")
check("a model that is there is not asked about", answers == [], str(answers))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("pull checks passed")
