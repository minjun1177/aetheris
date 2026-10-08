"""The harness's own API keys: kept out of the model, out of a plaintext file
where there is a keyring, and away from other accounts.

`providers.json` held every key in plaintext, and nothing stood between it and
a model with `read_file` - or `cat`, or `env` for a key set in the shell. One
call and the key is in the conversation, at the provider, and in a session
file. The checks below are the four ways that is closed:

* whatever a tool prints, a key the harness holds is not in it - and it is
  hidden as `[hidden: ...]`, never as `{{env:...}}`, which would be filled back
  in and let the model send the key somewhere;
* `providers.json` itself is refused to every tool, ahead of the permission
  rules, by any path that resolves to it - a symlink included;
* the model's commands run without the provider keys in their environment,
  while a command the person typed with `!` keeps their whole shell;
* with a keyring, the key is not in the file at all, and the file and the
  directory it is in are owner-only either way.

The keyring is a fake `keyring` module, so these run the same on every CI
runner and never touch a real Keychain or Credential Manager.
"""
import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOME = tempfile.mkdtemp(prefix="keys-home-")
os.environ["AETHERIS_HOME"] = HOME
os.environ.pop("AETHERIS_KEYRING", None)       # CI switches it off; on here, faked
for variable in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
                 "GOOGLE_API_KEY", "ATTACCA_CREDENTIAL"):
    os.environ.pop(variable, None)


class FakeBackend:
    priority = 5
    name = "Fake Keychain"

    def __init__(self):
        self.entries = {}
        self.refuse = False


backend = FakeBackend()
fake = types.ModuleType("keyring")
fake.get_keyring = lambda: backend
fake.get_password = lambda service, user: backend.entries.get((service, user))


def _set(service, user, value):
    if backend.refuse:
        raise RuntimeError("the keyring is locked")
    backend.entries[(service, user)] = value


def _delete(service, user):
    if (service, user) not in backend.entries:
        raise RuntimeError("no such entry")
    del backend.entries[(service, user)]


fake.set_password = _set
fake.delete_password = _delete
sys.modules["keyring"] = fake

from aetheris import config          # noqa: E402
from aetheris import keystore        # noqa: E402
from aetheris import paths           # noqa: E402
from aetheris import permissions     # noqa: E402
from aetheris import providers       # noqa: E402
from aetheris import tools           # noqa: E402
from aetheris import vault           # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


def quietly(function, *arguments, **keywords):
    with contextlib.redirect_stdout(io.StringIO()):
        return function(*arguments, **keywords)


def on_disk() -> dict:
    with open(providers.CONFIG_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def fresh_state():
    """What the next start would see: the file read again, the cache gone."""
    keystore._known.clear()
    providers._state = {}
    providers._active_cache_clear()
    return providers.load_state(force=True)


KEY = "sk-ant-api03-REALKEYxyz0123456789abcdef"
ENTRY = ("aetheris", f"anthropic @ {HOME}")
POSIX = os.name != "nt"

origin = os.getcwd()
WORK = tempfile.mkdtemp(prefix="keys-work-")
os.chdir(WORK)
config.AUTO_ALLOW = True                # /automode: the refusal has to beat it
config.GIT_AUTO_COMMIT = False
config.CHANNEL_ENABLED = False
config.AUTO_VERIFY = False

try:
    print("--- with a keyring, the key is not in the file ---")
    check("a real backend counts as available", keystore.available())
    providers.connect("anthropic", model="claude-x", api_key=KEY)
    saved = on_disk()["providers"]["anthropic"]
    check("providers.json holds no key", "api_key" not in saved and KEY not in json.dumps(on_disk()),
          str(saved))
    check("only a marker saying where it is", saved.get("key_store") == "keyring")
    check("the keyring has it, under this home", backend.entries.get(ENTRY) == KEY,
          str(backend.entries))
    if POSIX:
        mode = stat.S_IMODE(os.stat(providers.CONFIG_PATH).st_mode)
        check("the file is owner-only all the same", mode == 0o600, oct(mode))
    fresh_state()
    check("the next start reads it back", providers.build("anthropic").api_key == KEY)
    check("and /connect says where keys go", "Fake Keychain" in providers.key_home())

    print("\n--- a keyring that will not take it: the file, as before ---")
    backend.refuse = True
    providers.connect("openai", model="gpt-x", api_key="sk-proj-OPENAIkey0123456789")
    written = on_disk()["providers"]["openai"]
    check("the key the keyring refused is in the file, not lost",
          written.get("api_key") == "sk-proj-OPENAIkey0123456789", str(written))
    check("with no marker pointing at a keyring that does not have it",
          "key_store" not in written)
    backend.refuse = False

    print("\n--- a plaintext key from before moves at the next start ---")
    state = on_disk()
    state["providers"]["gemini"] = {"api_key": "AIzaGEMINIkey0123456789"}
    with open(providers.CONFIG_PATH, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    fresh_state()
    quietly(providers.apply_startup)
    check("out of the file", "api_key" not in on_disk()["providers"]["gemini"])
    check("into the keyring", backend.entries.get(("aetheris", f"gemini @ {HOME}"))
          == "AIzaGEMINIkey0123456789")

    print("\n--- the keyring gone: keyless, and the marker kept for its return ---")
    os.environ["AETHERIS_KEYRING"] = "off"
    fresh_state()
    check("switched off reads nothing", providers.build("anthropic").api_key == "")
    providers.save_state()
    check("and a save does not drop the marker",
          on_disk()["providers"]["anthropic"].get("key_store") == "keyring")
    del os.environ["AETHERIS_KEYRING"]
    fresh_state()
    check("back on, back again", providers.build("anthropic").api_key == KEY)

    print("\n--- /connect forget takes it out of the keyring too ---")
    removed, _ = providers.forget_key("gemini")
    check("forgotten", removed)
    check("the keyring entry is gone", ("aetheris", f"gemini @ {HOME}") not in backend.entries)
    check("and the marker", "key_store" not in on_disk()["providers"].get("gemini", {}))

    print("\n--- a keyring that never answers: a bounded wait, then the file ---")
    # gnome-keyring on WSL: on the bus, so it looks usable, but every call waits
    # on an unlock prompt that has no screen to appear on.
    stuck = threading.Event()
    answering = (fake.get_password, fake.set_password)
    fake.get_password = lambda service, user: stuck.wait()
    fake.set_password = lambda service, user, value: stuck.wait()
    keystore.TIMEOUT = 0.3
    started = time.monotonic()
    fresh_state()
    check("a start is not held up by it", time.monotonic() - started < 2,
          f"{time.monotonic() - started:.1f}s")
    check("the key it holds is missing this run, its marker kept for later",
          providers.build("anthropic").api_key == ""
          and on_disk()["providers"]["anthropic"].get("key_store") == "keyring")
    check("and the reason is kept to be shown", "did not answer" in keystore.trouble())
    started = time.monotonic()
    providers.connect("gemini", model="gemini-x", api_key="AIzaSTUCKkey0123456789")
    check("after one wait it is not asked again", time.monotonic() - started < 0.2,
          f"{time.monotonic() - started:.2f}s")
    check("a key saved meanwhile goes to the file, not lost",
          on_disk()["providers"]["gemini"].get("api_key") == "AIzaSTUCKkey0123456789")
    check("/connect says where it went, and why", providers.CONFIG_PATH in providers.key_home()
          and "did not answer" in providers.key_home(), providers.key_home())
    stuck.set()
    fake.get_password, fake.set_password = answering
    keystore.TIMEOUT = 10.0
    keystore._usable, keystore._trouble = None, ""
    fresh_state()
    check("answering again, the keyring key is back", providers.build("anthropic").api_key == KEY)

    print("\n--- whatever a tool prints, the harness's own key is not in it ---")
    os.environ["ATTACCA_CREDENTIAL"] = "zc_ENVcredential0123456789"
    with open("notes.txt", "w", encoding="utf-8") as handle:
        handle.write(f"anthropic={KEY}\nattacca=zc_ENVcredential0123456789\n")
    shown = quietly(tools.dispatch_tool, "read_file", {"filepath": "notes.txt"})
    check("a saved key is hidden", KEY not in shown and "[hidden: anthropic key]" in shown, shown)
    check("so is one from the environment",
          "zc_ENVcredential0123456789" not in shown and "[hidden: $ATTACCA_CREDENTIAL]" in shown)
    check("and nothing hands it back: the hidden form is not a placeholder",
          vault.restore("curl -H 'x: [hidden: anthropic key]'") == "curl -H 'x: [hidden: anthropic key]'")
    config.SECRET_REDACT = False
    check("SECRET_REDACT off is about .env, not these",
          KEY not in vault.redact(f"key {KEY}"))
    config.SECRET_REDACT = True

    print("\n--- providers.json is refused to every tool, ahead of the rules ---")
    permissions.hold("allow", ["read_file", "copy_file", "write_file"], "keys-test")
    for tool, arguments in (
            ("read_file", {"filepath": providers.CONFIG_PATH}),
            ("copy_file", {"src": "notes.txt", "dst": providers.CONFIG_PATH}),
            ("write_file", {"filepath": providers.CONFIG_PATH,
                            "content": '{"providers": {"anthropic": {"base_url": "https://evil"}}}'}),
            ("mcp__fs__read", {"path": providers.CONFIG_PATH})):
        result = quietly(tools.dispatch_tool, tool, arguments)
        check(f"{tool} refused, under /automode and an allow rule",
              "was refused" in str(result) and "API keys" in str(result), str(result)[:120])
    check("the file was not changed", "evil" not in json.dumps(on_disk()))
    if POSIX:
        os.symlink(providers.CONFIG_PATH, "innocent.json")
        result = quietly(tools.dispatch_tool, "read_file", {"filepath": "innocent.json"})
        check("a symlink to it is the same file", "was refused" in str(result), str(result)[:120])
    permissions.release("keys-test")

    print("\n--- the model's commands run without the provider keys ---")
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-ENVkey0123456789xyz"
    probe = (f'"{sys.executable}" -c "import os; '
             f'print(os.environ.get(\'ANTHROPIC_API_KEY\', \'absent\'))"')
    result = quietly(tools.dispatch_tool, "run_cmd", {"command": probe})
    check("not in the environment of run_cmd", "absent" in str(result), str(result)[:200])
    config.POLICY_AUTO_ALLOW = True
    raw = quietly(tools.run_user_cmd, probe)
    config.POLICY_AUTO_ALLOW = False
    check("a `!` command keeps the person's whole shell", "sk-ant-ENVkey0123456789xyz" in raw,
          raw[:200])
    check("and it is still hidden in the copy the model gets",
          "sk-ant-ENVkey0123456789xyz" not in vault.redact(raw))
    from aetheris import vm              # noqa: E402
    reply = quietly(tools.dispatch_tool, "run_python",
                    {"content": "import os\nprint(os.environ.get('ANTHROPIC_API_KEY', 'absent'))"})
    check("nor in run_python's", "absent" in str(reply), str(reply)[:200])
    vm.shutdown() if hasattr(vm, "shutdown") else None
    os.environ.pop("ANTHROPIC_API_KEY", None)
    os.environ.pop("ATTACCA_CREDENTIAL", None)

    if POSIX:
        print("\n--- the home directory is closed to other accounts ---")
        fresh_home = os.path.join(HOME, "made-here")
        os.environ["AETHERIS_HOME"] = fresh_home
        paths.ensure_home()
        mode = stat.S_IMODE(os.stat(fresh_home).st_mode)
        check("a new one is created owner-only", mode == 0o700, oct(mode))
        open_home = os.path.join(HOME, "theirs")
        os.makedirs(open_home)
        os.chmod(open_home, 0o755)
        os.environ["AETHERIS_HOME"] = open_home
        check("one named with AETHERIS_HOME is not changed under them",
              not paths.close_home() and stat.S_IMODE(os.stat(open_home).st_mode) == 0o755)
        user = tempfile.mkdtemp(prefix="keys-user-")
        os.makedirs(os.path.join(user, ".localchat"))
        os.chmod(os.path.join(user, ".localchat"), 0o755)
        del os.environ["AETHERIS_HOME"]
        out = subprocess.run(
            [sys.executable, "-c", "from aetheris import paths; print(paths.close_home())"],
            env={**os.environ, "HOME": user}, capture_output=True, text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        mode = stat.S_IMODE(os.stat(os.path.join(user, ".localchat")).st_mode)
        check("the default one, from before the rename too, is closed",
              out.stdout.strip() == "True" and mode == 0o700, f"{oct(mode)} {out.stderr[-200:]}")
        shutil.rmtree(user, ignore_errors=True)
        os.environ["AETHERIS_HOME"] = HOME

        from aetheris import session     # noqa: E402
        config.SAVE_CHAT_HISTORY = True
        config.SESSION_DIR = os.path.join(HOME, "sessions")
        sid = session.save_session([{"role": "system", "content": "x"}], None)
        mode = stat.S_IMODE(os.stat(os.path.join(config.SESSION_DIR, f"{sid}.json")).st_mode)
        check("a saved conversation is owner-only", mode == 0o600, oct(mode))
        mode = stat.S_IMODE(os.stat(config.SESSION_DIR).st_mode)
        check("and so is the directory it is in", mode == 0o700, oct(mode))
        config.SAVE_CHAT_HISTORY = False

finally:
    os.chdir(origin)
    shutil.rmtree(WORK, ignore_errors=True)
    shutil.rmtree(HOME, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("key checks passed")
