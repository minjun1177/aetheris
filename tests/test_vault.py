"""`.env` values the model is never told, and the harness pastes in itself.

A `.env` is the one file whose contents *are* the secret, and a value the model
reads does not stay read: it goes to the provider, and it is written into
`~/.aetheris/sessions/*.json` and stays there. One `read_file` and a key is in
two places nobody would think to check.

So the model is shown `STRIPE_KEY={{env:STRIPE_KEY}}` and, when it writes that
placeholder into something that *runs*, the harness puts the real value in on
the way to the tool. The checks below are the ones that decide whether that is
a feature or a leak with extra steps:

* the value never reaches the model, by any of the four routes it could take;
* the placeholder does reach the shell as the real key, so the model can use a
  secret it has never seen;
* and it is never written *into* a file, in either direction - not expanded on
  the way in, and not allowed to overwrite the value it names.

The last one is the one worth staring at. `{{env:X}}` expanded into a file body
would be a copy-out: write it, read it back, and the model has the key. The
same placeholder written over `.env` would be a copy-*away*: the key replaced
by its own name, by a model that cannot see either and would never know.
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOME = tempfile.mkdtemp(prefix="vault-home-")
os.environ["AETHERIS_HOME"] = HOME

from aetheris import config          # noqa: E402
from aetheris import tools           # noqa: E402
from aetheris import vault           # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


def quietly(function, *arguments):
    """Run a tool without its approval box and progress lines on screen."""
    with contextlib.redirect_stdout(io.StringIO()):
        return function(*arguments)


KEY = "sk_live_51H8xQzAbCdEfGhIjKlMnOp"
URL = "postgres://admin:hunter2xyz@db.internal:5432/app"

origin = os.getcwd()
WORK = tempfile.mkdtemp(prefix="vault-work-")
os.chdir(WORK)

config.AUTO_ALLOW = True                # no terminal here to approve anything
config.PERMISSIONS_ENABLED = False
config.GIT_AUTO_COMMIT = False
config.CHANNEL_ENABLED = False
config.AUTO_VERIFY = False

try:
    with open(".env", "w", encoding="utf-8") as handle:
        handle.write("# the real thing\n"
                     f"STRIPE_KEY={KEY}\n"
                     f'DATABASE_URL="{URL}"\n'
                     "APP_ENV=development\n"      # a word, not a secret
                     "PORT=8080\n"                # a number, not a secret
                     "SHORT=abc\n")               # too short to hide safely
    vault._cache.clear()

    # ----------------------------------------------------------------------
    print("--- what counts as a secret ---")
    known = vault.known()
    check("a long opaque value does", known.get("STRIPE_KEY") == KEY)
    check("so does a URL with a password in it", known.get("DATABASE_URL") == URL)
    check("a plain word does not", "APP_ENV" not in known, str(sorted(known)))
    check("nor does a number", "PORT" not in known)
    # The dangerous one: hiding `abc` would rewrite every "abc" in every result.
    check("nor does anything too short to hide safely", "SHORT" not in known)
    check("comments are not settings", len(known) == 2, str(sorted(known)))

    # ----------------------------------------------------------------------
    print("\n--- the value does not reach the model ---")
    read = quietly(tools.dispatch_tool, "read_file", {"filepath": ".env"})
    check("read_file gives the name, not the value",
          KEY not in read and "{{env:STRIPE_KEY}}" in read, read[:70])
    check("and the same for every secret in the file",
          URL not in read and "{{env:DATABASE_URL}}" in read)
    check("what is not a secret is left exactly as it is",
          "APP_ENV=development" in read and "PORT=8080" in read)

    # The shell on the other side of `run_cmd` is `cmd.exe` on Windows, which
    # has none of `cat`, `grep` or `test`. Spelled the Unix way these checks do
    # not fail there - they pass on the error message, which proves nothing.
    # A CI runner hides that too: Git for Windows leaves the lot on PATH.
    WINDOWS = os.name == "nt"

    printed = quietly(tools.dispatch_tool, "run_cmd",
                      {"command": "type .env" if WINDOWS else "cat .env"})
    check("a command that prints it is redacted too", KEY not in printed, printed[:60])

    listed = quietly(tools.dispatch_tool, "run_cmd",
                     {"command": "findstr sk_live_ .env" if WINDOWS
                      else "grep -o 'sk_live_[A-Za-z0-9]*' .env"})
    check("and so is a command that goes looking for it", KEY not in listed)

    # `@.env` reaches read_file directly rather than through dispatch_tool.
    from aetheris import mentions          # noqa: E402
    attached, notes, _pictures = mentions.expand("what is in @.env")
    check("an @ attachment is redacted as well", KEY not in attached, str(notes))

    # ----------------------------------------------------------------------
    print("\n--- and the harness pastes it in where it runs ---")
    # Proof the *real* key reached the shell, without printing it: the command
    # only succeeds if the value is there to be matched.
    ran = quietly(tools.dispatch_tool, "run_cmd",
                  {"command": 'if "{{env:STRIPE_KEY}}"=="' + KEY + '" '
                              'echo THE-REAL-VALUE-ARRIVED' if WINDOWS
                   else 'test "{{env:STRIPE_KEY}}" = "' + KEY + '" '
                        '&& echo THE-REAL-VALUE-ARRIVED'})
    check("a placeholder reaches the shell as the value",
          "THE-REAL-VALUE-ARRIVED" in ran, ran[:60])
    check("and the result still comes back redacted", KEY not in ran)

    check("the tool is handed the value", vault.fill_in(
        "run_cmd", {"command": "curl {{env:STRIPE_KEY}}"})["command"].endswith(KEY))
    check("but the call the conversation keeps is not changed",
          "{{env:" in vault.fill_in("write_file",
                                    {"content": "{{env:STRIPE_KEY}}"})["content"])
    check("a tool that is not on the list gets the placeholder",
          vault.fill_in("mcp__x__y", {"command": "{{env:STRIPE_KEY}}"})["command"]
          == "{{env:STRIPE_KEY}}")
    check("an unknown name is left alone rather than emptied",
          vault.restore("{{env:NOT_A_KEY}}") == "{{env:NOT_A_KEY}}")

    # ----------------------------------------------------------------------
    print("\n--- a file is never how it gets out, or lost ---")
    written = quietly(tools.dispatch_tool, "write_file",
                      {"filepath": "copy.txt", "content": "{{env:STRIPE_KEY}}"})
    check("a placeholder written to a file stays a placeholder",
          written.startswith("[Success]")
          and open("copy.txt", encoding="utf-8").read().strip() == "{{env:STRIPE_KEY}}")
    check("so reading it back gives the model nothing new",
          KEY not in quietly(tools.dispatch_tool, "read_file", {"filepath": "copy.txt"}))

    refused = quietly(tools.dispatch_tool, "write_file",
                      {"filepath": ".env", "content": "STRIPE_KEY={{env:STRIPE_KEY}}\n"})
    check("writing the placeholder over the real value is refused",
          refused.startswith(config.TOOL_ERROR_PREFIX), refused[:70])
    check("and it says which secret it was protecting", "STRIPE_KEY" in refused)
    check("the file is untouched", KEY in open(".env", encoding="utf-8").read())

    example = quietly(tools.dispatch_tool, "write_file",
                      {"filepath": ".env.example", "content": "STRIPE_KEY={{env:STRIPE_KEY}}\n"})
    check("but a file that holds no secret takes one happily",
          example.startswith("[Success]"), example[:60])
    check("an example file is not read as a source of secrets",
          all(not p.endswith(".example") for p in vault.files()), str(vault.files()))

    # ----------------------------------------------------------------------
    print("\n--- editing a line the model only saw redacted ---")
    # Quoting back exactly what it was shown must be understood. Otherwise the
    # model is told "line 1 is not what you say it is", shown the same redacted
    # line again, and has nowhere left to go.
    with open("app.conf", "w", encoding="utf-8") as handle:
        handle.write(f"token = {KEY}\nname = app\n")
    rows = quietly(tools.dispatch_tool, "read_file", {"filepath": "app.conf"})
    first = rows.splitlines()[0]
    check("the row it is given names the secret", "{{env:STRIPE_KEY}}" in first, first)
    edited = quietly(tools.dispatch_tool, "edit_file",
                     {"filepath": "app.conf", "old_content": first,
                      "new_content": "token = read-from-env"})
    check("quoting that row back is understood",
          edited.startswith("[Success]"), edited.splitlines()[0][:80])
    check("and the edit landed",
          "token = read-from-env" in open("app.conf", encoding="utf-8").read())

    # ----------------------------------------------------------------------
    print("\n--- what the model is told, and the way out ---")
    section = vault.prompt_section()
    check("the prompt names the secrets it has", "STRIPE_KEY" in section)
    check("and never their values", KEY not in section and URL not in section)
    check("it says the file is not broken", "not a broken file" in section)

    config.SECRET_REDACT = False
    try:
        vault._cache.clear()
        check("turning it off gives the old behaviour back",
              vault.redact(f"key={KEY}") == f"key={KEY}")
        check("and says nothing in the prompt", vault.prompt_section() == "")
    finally:
        config.SECRET_REDACT = True
        vault._cache.clear()

    # A project with no .env is the ordinary case and must cost nothing.
    os.remove(".env")
    vault._cache.clear()
    check("with no .env there is nothing to hide", vault.known() == {})
    check("and text is handed back unchanged",
          vault.redact("nothing to do here") == "nothing to do here")

finally:
    os.chdir(origin)
    shutil.rmtree(WORK, ignore_errors=True)
    shutil.rmtree(HOME, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("vault checks passed")
