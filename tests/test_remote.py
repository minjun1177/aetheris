"""One door into a running session, and everything that must not get through it.

`/remote` is the only feature here that opens a port, and what is behind that
port is a shell. So the checks that matter most are the refusals: that a
request without the token is answered with nothing, that a wrong token is not
almost-right, that a `Host` naming somebody else's domain - which is what a
rebound DNS name looks like on the wire - is turned away before the token is
even considered, and that closing it really closes it.

The rest is about the two ways this could quietly lose or leak something: a
transcript that carries a `.env` value off this machine because the person ran
`!cat .env`, and a question answered by a phone that was still showing the
last one. Both are tested against the real server, over real HTTP, on a port
the operating system picks.

A second process is not needed here: the server genuinely runs in a thread of
its own, so a request from this one is already a request from somewhere else.
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from simple_harness import paths

# Before `config` is imported: it resolves the state paths at import time, and
# none of this may touch the real ~/.localchat.
HOME = tempfile.mkdtemp(prefix="remote-home-")
os.environ[paths.ENV_VAR] = HOME

from simple_harness import config          # noqa: E402
config.MCP_ENABLED = False
config.SAVE_CHAT_HISTORY = False

from simple_harness import remote          # noqa: E402
from simple_harness import vault           # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


def request(path, token="", method="GET", payload=None, host=None, session=None):
    """One HTTP call. Returns (status, body) - a refusal is an answer too."""
    url = f"http://127.0.0.1:{remote.status()['port']}{path}"
    if token:
        url += ("&" if "?" in url else "?") + "k=" + token
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if host:
        req.add_header("Host", host)
    if session:
        req.add_header("X-Remote-Session", session)
    try:
        with urllib.request.urlopen(req, timeout=10) as answer:
            return answer.status, answer.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def socket_for_a_free_port() -> int:
    """A port nothing is listening on, so moving the door has somewhere to go."""
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    port = holder.getsockname()[1]
    holder.close()
    return port


def state(token, since=0):
    code, body = request(f"/state?since={since}", token)
    return code, json.loads(body) if code == 200 else {}


def state_with(session):
    """The transcript, asked for the way a paired browser asks for it."""
    return request("/state", TOKEN, session=session)


# ---------------------------------------------------------------------------
print("--- the door opens, and says where ---")

config.REMOTE_PORT = 0          # let the OS pick, so a busy 8765 is not a failure
config.REMOTE_LINES = 50
opened = remote.start()
check("it starts", remote.running() and opened["port"] > 0, str(opened["port"]))
check("bound to loopback unless asked otherwise", opened["host"] == "127.0.0.1",
      opened["host"])
TOKEN = opened["token"]
# Opening the door starts mirroring this terminal, which here is the test's own
# `[ok]` lines - they would then *be* the transcript every check below reads.
# So the tee comes straight back off, and is put on again on purpose further
# down, where it is what is being tested.
remote._drop_mirror()
check("the token is long enough to be worth having", len(TOKEN) >= 16, TOKEN[:4] + "…")
check("and the URL carries it", opened["urls"] and TOKEN in opened["urls"][0])
check("no setting holds the token",
      not hasattr(config, "REMOTE_TOKEN")
      and TOKEN not in [str(value) for value in config.settable().values()],
      "a saved token would outlive the session that made it")

# ---------------------------------------------------------------------------
print("\n--- and refuses everything that is not the person holding the link ---")

code, _ = request("/")
check("no token, no page", code == 401, str(code))
code, _ = request("/state")
check("no token, no transcript", code == 401, str(code))
code, _ = request("/say", token="", method="POST", payload={"text": "hello"})
check("no token, nothing typed", code == 401, str(code))
code, _ = request("/", token=TOKEN[:-1] + ("a" if TOKEN[-1] != "a" else "b"))
check("a token that is nearly right is wrong", code == 401, str(code))
code, _ = request("/", token=TOKEN, host="evil.example.com")
check("a Host this machine was never called is refused", code == 403, str(code))
check("...before the token is even looked at",
      request("/", token="", host="evil.example.com")[0] == 403)
code, _ = request("/nothing/here", token=TOKEN)
check("an unknown path is a 404, not a file", code == 404, str(code))

code, body = request("/", token=TOKEN)
check("with the token, the page is served", code == 200 and "<!doctype html>" in body)
check("and the page carries no placeholder", "__TOKEN__" not in body)
check("the page fetches nothing from the internet",
      "http://" not in body.replace("http://127.0.0.1", "") and "https://" not in body)

# ---------------------------------------------------------------------------
print("\n--- the transcript is what the terminal printed ---")

remote.publish("plain line\n")
remote.publish("\x1b[33mcoloured\x1b[0m line\n")
remote.publish("\x1b[2K\x1b[3Aerased and moved\n")
remote.publish("spinner one\rspinner two\n")
remote.publish("half a line, still")
code, body = state(TOKEN)
lines = [text for _, text in body["lines"]]
check("a line arrives", "plain line" in lines, str(lines))
check("its colour comes with it - the browser is what paints the transcript",
      "\x1b[33mcoloured\x1b[0m line" in lines, str(lines))
check("but nothing that moves a cursor or erases does",
      "erased and moved" in lines
      and not any("\x1b[2K" in text or "\x1b[3A" in text for text in lines),
      str([t for t in lines if "erased" in t]))
check("an overwritten line arrives as what was left",
      "spinner two" in lines and "spinner one" not in lines, str(lines))
check("a line still being printed is the tail, not a line",
      body["tail"] == "half a line, still" and
      "half a line, still" not in lines, body["tail"])

# The spinner is one line, rewritten twenty times a second and never ended
# with a newline: every frame it has ever drawn sits in that buffer unless the
# `\r` is honoured there too. It reached the phone as a paragraph of them.
remote.publish("\n")
for frame in ("\u00b7", "\u2722", "*", "\u2736"):
    remote.publish(f"\r  {frame} thinking\u2026  ")
code, body = state(TOKEN)
check("a spinner is the frame it is on, not every frame it has been",
      body["tail"].strip() == "\u2736 thinking\u2026", repr(body["tail"]))
remote.publish("\r\x1b[K")
code, body = state(TOKEN)
check("and it leaves nothing behind when it stops", body["tail"] == "",
      repr(body["tail"]))
remote.publish("half a line, still")
remote.publish("\n")            # finish it, so the ring is left tidy

seq = body["seq"]
remote.publish("after the cursor\n")
code, body = state(TOKEN, since=seq)
check("a cursor only gets what is new",
      [t for _, t in body["lines"]] == ["half a line, still", "after the cursor"],
      str(body["lines"]))

secret_home = os.path.join(HOME, "project")
os.makedirs(secret_home, exist_ok=True)
with open(os.path.join(secret_home, ".env"), "w", encoding="utf-8") as f:
    f.write("API_KEY=sk-not-a-real-key-000\n")
here = os.getcwd()
os.chdir(secret_home)          # `vault` reads the .env of wherever it is asked
try:
    check("the .env is one vault can see", "API_KEY" in vault.names(),
          str(vault.names()))
    remote.publish("the key is sk-not-a-real-key-000 apparently\n")
finally:
    os.chdir(here)
code, body = state(TOKEN)
leaked = [t for _, t in body["lines"] if "sk-not-a-real-key-000" in t]
check("a .env value on the terminal is not on the wire", not leaked, str(leaked))

for i in range(config.REMOTE_LINES + 20):
    remote.publish(f"line {i}\n")
code, body = state(TOKEN)
check("the transcript is a ring, not a leak",
      len(remote.transcript()) <= config.REMOTE_LINES,
      f"{len(remote.transcript())} kept of {config.REMOTE_LINES}")

# ---------------------------------------------------------------------------
print("\n--- a line typed there is a line typed here ---")

code, _ = request("/say", TOKEN, "POST", {"text": "  what does this repo do?  "})
check("it is accepted", code == 200, str(code))
check("and queued", remote.queued() == 1, str(remote.queued()))
check("the prompt gets it, stripped", remote.take_line() == "what does this repo do?")
check("and only once", remote.take_line() == "")
code, _ = request("/say", TOKEN, "POST", {"text": "   "})
check("an empty line is refused rather than queued",
      code == 400 and remote.queued() == 0, str(code))

remote.set_driver("remote")
check("a remote line marks the turn as remotely driven", remote.driven())
remote.set_driver("terminal")
check("and a typed one takes it back", not remote.driven())

# ---------------------------------------------------------------------------
print("\n--- a question goes where the driver is ---")

remote.set_driver("terminal")
check("nothing is asked of a remote that is not driving",
      remote.ask("Approval", [("path", "x.py")], [("y", "Allow")], timeout=1) == "")

remote.set_driver("remote")
answers = []


def ask_in_the_background(timeout=10):
    answers.clear()
    thread = threading.Thread(
        target=lambda: answers.append(
            remote.ask("Run Command - approval required",
                       [("command", "rm -rf build/")],
                       [("y", "Allow"), ("n", "Deny")], timeout=timeout)),
        daemon=True)
    thread.start()
    for _ in range(100):                     # wait for it to be the pending one
        if remote.pending():
            return thread
        time.sleep(0.05)
    return thread


thread = ask_in_the_background()
code, body = state(TOKEN)
question = body["question"]
check("the question reaches the remote",
      question and question["title"].startswith("Run Command"), str(question))
check("with what is being approved",
      question and ["command", "rm -rf build/"] in question["details"], str(question))
check("and a question is plain text - the page draws it, so colour is noise",
      question and not any("\x1b" in str(part) for row in question["details"] for part in row)
      and not any("\x1b" in str(part) for row in question["choices"] for part in row),
      str(question))
check("and no answer field to read ahead",
      question and "answer" not in question, str(question))

code, _ = request("/answer", TOKEN, "POST", {"id": "not-this-one", "value": "y"})
check("a stale question cannot be answered", code == 409, str(code))
code, _ = request("/answer", TOKEN, "POST", {"id": question["id"], "value": "y"})
thread.join(timeout=5)
check("the real one can", code == 200 and answers == ["y"], str(answers))
check("and is gone once answered", remote.pending() is None)

thread = ask_in_the_background(timeout=1)
thread.join(timeout=5)
check("a question nobody answers expires", answers == [""], str(answers))
check("...which every caller reads as no", not answers[0])
check("and leaves nothing behind", remote.pending() is None)
remote.set_driver("terminal")

# ---------------------------------------------------------------------------
print("\n--- and the questions that reach it are the real ones ---")

# Not a stand-in for the approval prompt: the approval prompt itself, and the
# plan tool, called the way the tool loop calls them. What is being checked is
# that they actually go through `ask_the_driver` rather than to `input()` -
# which is the difference between a remote control and a remote screen.
import asyncio                                                      # noqa: E402
from simple_harness import app                                      # noqa: E402
from simple_harness import tools                                    # noqa: E402
from simple_harness import tui                                      # noqa: E402

config.AUTO_ALLOW = False
config.PERMISSIONS_ENABLED = False
remote.set_driver("remote")


def answered_with(value, call):
    """Run `call` in a thread, answer its question with `value`, return both."""
    out = []
    thread = threading.Thread(target=lambda: out.append(call()), daemon=True)
    thread.start()
    for _ in range(200):
        if remote.pending():
            break
        time.sleep(0.05)
    asked = remote.pending()
    if asked:
        request("/answer", TOKEN, "POST", {"id": asked["id"], "value": value})
    thread.join(timeout=10)
    return asked, (out[0] if out else None)


asked, got = answered_with("y", lambda: tui._approval_prompt(
    "Run Command", [("command", "rm -rf build/")], rule="run_cmd(rm *)"))
check("the approval prompt is asked on the remote",
      asked and asked["title"].startswith("Run Command"), str(asked))
check("with every answer the terminal offers, including 'always allow'",
      asked and [value for value, _ in asked["choices"]] == ["y", "n", "a"],
      str(asked["choices"]) if asked else "")
check("and Allow there means allowed here", got is True, str(got))

asked, got = answered_with("n", lambda: tui._approval_prompt(
    "Delete File", [("path", "x.py")], rule="delete_file(x.py)"))
check("Deny there means refused here", got is False, str(got))

asked, got = answered_with("2", lambda: tools.handle_submit_plan_for_approval(
    "read app.py", "add a flag", "run the tests"))
check("a plan is put to the remote too",
      asked and asked["title"].startswith("Plan approval"), str(asked))
check("and rejecting it there rejects it here",
      isinstance(got, str) and "Rejected" in got, str(got)[:60])

asked, got = answered_with("2", lambda: tools._ask_one(
    "Which database?", ["postgres", "sqlite"], 1, 1))
check("get_input's own question goes there as its options",
      asked and [label for _, label in asked["choices"]] == ["postgres", "sqlite"],
      str(asked["choices"]) if asked else "")
check("and the option chosen is the one that comes back", got == "sqlite", str(got))

asked, got = answered_with("mysql, actually", lambda: tools._ask_one(
    "Which database?", ["postgres", "sqlite"], 1, 1))
check("words typed there instead of a number are the answer",
      got == "mysql, actually", str(got))

remote.set_driver("remote")
check("a phone may not close the session it is driving",
      app._refused_from_remote("/exit") and app._refused_from_remote("/quit"))
check("...but anything else it types is its own business",
      not app._refused_from_remote("/clear"))
remote.set_driver("terminal")
check("and at the keyboard /exit is /exit", not app._refused_from_remote("/exit"))

request("/say", TOKEN, "POST", {"text": "carry on then"})
line = asyncio.run(app._read_line(None))
check("a queued line is what the prompt returns", line == "carry on then", line)
check("and taking it marks the turn as remotely driven", remote.driven())
remote.set_driver("terminal")
remote._drop_mirror()           # `_read_line` puts the tee back on; off again

# ---------------------------------------------------------------------------
print("\n--- a phone that asks once is answered when there is something ---")

started = time.time()
code, body = state(TOKEN)                    # no wait: answers immediately
check("without wait=1 it answers at once", time.time() - started < 2)

held = []


def long_poll(since):
    code, raw = request(f"/state?since={since}&wait=1", TOKEN)
    held.append((time.time(), json.loads(raw)))


seq = body["seq"]
poller = threading.Thread(target=long_poll, args=(seq,), daemon=True)
started = time.time()
poller.start()
time.sleep(0.5)
check("a poll with nothing to say waits", not held)
remote.publish("something happened\n")
poller.join(timeout=5)
check("and answers the moment something is printed",
      held and "something happened" in [t for _, t in held[0][1]["lines"]],
      str(held[0][1]["lines"]) if held else "nothing came back")
check("in about the time it took, not the full hold",
      held and held[0][0] - started < remote.HOLD_SECONDS / 2)

# ---------------------------------------------------------------------------
print("\n--- the mirror is the terminal's own output, and is put back ---")

original = sys.stdout
remote.ensure_mirror()
check("the tee is installed over stdout", sys.stdout is not original)
remote.ensure_mirror()
check("and only once, however often it is asked", sys.stdout.target is original)
print("printed through the tee")
check("what is printed lands in the transcript",
      "printed through the tee" in [text for _, text in remote.transcript()])

# ---------------------------------------------------------------------------
print("\n--- the port is a setting, and moving it moves the door ---")

check("the port is something /set can change",
      "REMOTE_PORT" in config.settable() and "REMOTE_HOST" in config.settable())

free = socket_for_a_free_port()
was_token, was_port = remote.token(), remote.status()["port"]
config.REMOTE_PORT = free
changed = remote.reconfigure()
check("changing it while the remote is open moves it",
      changed.get("rebound") and remote.status()["port"] == free,
      f"{was_port} → {remote.status()['port']}")
check("the transcript survives the move", len(remote.transcript()) > 0)
TOKEN = remote.token()
check("the link is a new one", TOKEN != was_token)
code, _ = request("/", was_token)
check("and the old one opens nothing", code == 401, str(code))
code, _ = request("/", TOKEN)
check("while the new one does", code == 200, str(code))

config.REMOTE_LINES = 25
changed = remote.reconfigure()
check("changing how much is kept resizes it rather than moving anything",
      changed.get("resized") == 25 and len(remote.transcript()) <= 25,
      str(changed))
check("and an unrelated /set moves nothing", remote.reconfigure() == {})

# ---------------------------------------------------------------------------
print("\n--- a wrong token is counted, said out loud, and then shut out ---")

remote.take_notices()                    # start from a quiet board
config.REMOTE_MAX_BAD_TOKENS = 3
config.REMOTE_LOCKOUT = 1                # seconds, so the test can wait it out

codes = [request("/", "not-the-token")[0] for _ in range(3)]
check("each wrong token is refused", codes == [401, 401, 401], str(codes))
code, body = request("/", TOKEN)
check("and after enough of them even the right one is turned away",
      code == 429, str(code))
check("with how long to wait", "try again in" in body, body[:80])
check("the address is named as refused",
      remote.status()["refused"] == ["127.0.0.1"], str(remote.status()["refused"]))

notices = remote.take_notices()
check("the first wrong token is reported at the prompt",
      any("token that is not this one" in text for text in notices), str(notices))
check("and so is the shutting out",
      any("wrong tokens" in text and "refused" in text for text in notices),
      str(notices))
check("but not one line per attempt - a script must not fill the terminal",
      len(notices) <= 3, str(notices))

time.sleep(1.1)
code, _ = request("/", TOKEN)
check("the lockout lifts by itself", code == 200, str(code))
check("and holding the real link clears the count",
      not remote.status()["refused"], str(remote.status()["refused"]))
config.REMOTE_MAX_BAD_TOKENS = 20

remote.take_notices()
check("who opened it is known", any(row["address"] == "127.0.0.1"
                                    for row in remote.clients()),
      str(remote.clients()))

# A token with a byte of UTF-8 in it used to be the way past all of that.
# `secrets.compare_digest` will not compare two *strings* when either holds a
# character outside ASCII - it raises TypeError rather than answering False -
# so `?k=%C3%A9` left the handler through `handle_error`: the connection was
# dropped with no HTTP reply, `note_bad_token` was never reached, and the count
# that leads to the lockout never moved. An address could knock all afternoon
# and appear at the prompt only as a stream of TypeErrors.
remote.take_notices()
config.REMOTE_MAX_BAD_TOKENS = 3
config.REMOTE_LOCKOUT = 1
codes = [request("/state", quote("é"))[0], request("/state", quote("中文"))[0]]
check("a token that is not even ASCII is answered, not crashed",
      codes == [401, 401], str(codes))
check("and it is counted like any other wrong one",
      request("/state", quote("ü"))[0] == 401
      and remote.status()["refused"] == ["127.0.0.1"],
      str(remote.status()["refused"]))
notices = remote.take_notices()
check("reported as a wrong token rather than as a TypeError",
      any("token that is not this one" in text for text in notices)
      and not any("TypeError" in text for text in notices), str(notices))
time.sleep(1.1)
config.REMOTE_MAX_BAD_TOKENS = 20
check("and the real token still opens it", request("/", TOKEN)[0] == 200)
remote.take_notices()

# ---------------------------------------------------------------------------
print("\n--- and what the conversation is costing ---")

code, body = state(TOKEN)
check("nothing is claimed before anything is known", not body.get("usage"),
      str(body.get("usage")))
remote.set_usage(4321, 20000, turns=3)
code, body = state(TOKEN)
check("what the loop reports is what the page gets",
      body["usage"] == {"used": 4321, "budget": 20000, "turns": 3}, str(body["usage"]))

# ---------------------------------------------------------------------------
print("\n--- the phone is told what it may type ---")

code, body = request("/commands", TOKEN)
rows = json.loads(body)["commands"] if code == 200 else []
check("the command list is served", code == 200 and len(rows) > 20, str(len(rows)))
from simple_harness import tui                                      # noqa: E402
check("and it is the table /help renders, not a second list",
      [row["name"] for row in rows][:len(tui.COMMANDS)]
      == [name for name, _ in tui.COMMANDS],
      "a phone offered a command the terminal does not answer is a dead end")
check("every row says what it does",
      all(row["help"].strip() for row in rows))
check("it needs the token like everything else", request("/commands")[0] == 401)

# ---------------------------------------------------------------------------
print("\n--- what a browser fetches by itself is not an intruder ---")

remote.take_notices()
for path in ("/favicon.ico", "/apple-touch-icon.png", "/robots.txt"):
    code, _ = request(path)
    check(f"{path} answers 404 without a token", code == 404, str(code))
check("and none of it is reported as somebody trying a token",
      not remote.take_notices(),
      "opening the link used to report you at your own prompt, twice")
check("...nor counted towards the lockout", not remote.status()["refused"])
# From a standing start: only the *first* wrong token from an address is
# reported, so a count left over from the check above would hide this one.
remote._bad.clear()
code, _ = request("/state", "not-the-token")
check("while a real wrong token still is",
      code == 401 and len(remote.take_notices()) == 1, str(code))

print("\n--- a browser that goes away is not a stack trace ---")

import io                                                          # noqa: E402
import contextlib                                                  # noqa: E402

noise = io.StringIO()
with contextlib.redirect_stderr(noise):
    dropped = socket.create_connection(("127.0.0.1", remote.status()["port"]))
    dropped.sendall(f"GET /state?k={TOKEN}&wait=1 HTTP/1.1\r\n"
                    f"Host: 127.0.0.1\r\n\r\n".encode())
    dropped.close()
    time.sleep(0.8)
check("nothing is printed when a connection is torn down mid-request",
      noise.getvalue() == "", noise.getvalue()[:120])
check("and the remote is still answering", state(TOKEN)[0] == 200)

# ---------------------------------------------------------------------------
print("\n--- over a network the link is not enough on its own ---")

check("loopback asks for nothing more by default",
      not remote.pairing_required() and state(TOKEN)[0] == 200,
      f"REMOTE_PAIR is {config.REMOTE_PAIR!r}")

config.REMOTE_PAIR = "always"
check("...and that is a setting", remote.pairing_required())
code, body = request("/state", TOKEN)
check("the transcript is behind the second factor",
      code == 403 and "pair" in body, f"{code} {body}")
code, _ = request("/say", TOKEN, "POST", {"text": "let me in"})
check("so is typing", code == 403, str(code))
code, page = request("/", TOKEN)
check("but the page still loads - it is what asks for the code",
      code == 200 and "<!doctype html>" in page, str(code))

remote.take_notices()
code, body = request("/pair", TOKEN, "POST", {})
check("asking to pair is accepted", code == 200 and json.loads(body)["wanted"], body)
check("and the answer does not contain the code",
      not any(ch.isdigit() for ch in json.loads(body).get("code", "")), body)
notices = remote.take_notices()
check("the code is printed here instead - which is the whole point",
      any("wants to drive this session" in text and "Code:" in text for text in notices),
      str(notices))

secret = "".join(ch for ch in notices[-1].split("Code:")[1] if ch.isdigit())[:6]
check("it is six digits", len(secret) == 6 and secret.isdigit(), secret)

wrong = "".join("0" if ch != "0" else "1" for ch in secret)
code, _ = request("/pair", TOKEN, "POST", {"code": wrong})
check("a wrong code is refused", code == 403, str(code))
for _ in range(2):
    request("/pair", TOKEN, "POST", {"code": wrong})
code, _ = request("/pair", TOKEN, "POST", {"code": secret})
check("and three wrong ones kill the code, right answer or not",
      code == 403, str(code))
notices = remote.take_notices()
check("which is said at the prompt too",
      any("wrong" in text for text in notices), str(notices))

request("/pair", TOKEN, "POST", {})                    # a fresh code
secret = "".join(ch for ch in remote.take_notices()[-1].split("Code:")[1]
                 if ch.isdigit())[:6]
code, body = request("/pair", TOKEN, "POST", {"code": secret})
SESSION = json.loads(body).get("session", "")
check("the code off the terminal is what opens it", code == 200 and len(SESSION) > 20,
      str(code))

check("the transcript opens with the session", state_with(SESSION)[0] == 200)
code, _ = request("/say", TOKEN, "POST", {"text": "now let me in"}, session=SESSION)
check("and so does typing", code == 200 and remote.take_line() == "now let me in")
check("without it, still nothing", state(TOKEN)[0] == 403)
check("and the session belongs to the address it was issued to",
      not remote.paired(SESSION, "10.9.9.9") and remote.paired(SESSION, "127.0.0.1"))
check("the pairing is listed", len(remote.sessions()) == 1, str(remote.sessions()))

check("/remote forget drops it", remote.forget_sessions() == 1)
check("...and the browser is outside again", state_with(SESSION)[0] == 403)
check("the link itself still works", request("/", TOKEN)[0] == 200)

# The same trap as the non-ASCII token, one factor along.
# `complete_pairing` keeps whatever `str.isdigit()` accepts, and that is true
# of `٣` as well as `3` - so a code of Arabic-Indic digits reached
# `compare_digest` as a non-ASCII string and raised out of the handler instead
# of spending one of the three tries it is allowed.
request("/pair", TOKEN, "POST", {})                    # a fresh code to guess at
remote.take_notices()
code, _ = request("/pair", TOKEN, "POST", {"code": "١٢٣٤٥٦"})
check("a pairing code that is not ASCII digits is refused, not crashed",
      code == 403, str(code))
check("and it spent a try like any other wrong guess",
      (remote._pairing or {}).get("tries") == 1, str(remote._pairing))
remote.forget_sessions()                               # back to no code outstanding
remote.take_notices()

config.REMOTE_PAIR = "never"
check("a door can be told not to ask", state(TOKEN)[0] == 200)
config.REMOTE_PAIR = "lan"

# ---------------------------------------------------------------------------
print("\n--- and closing it closes it ---")

port = remote.status()["port"]
remote.stop()
check("it stops", not remote.running())
check("stdout is the terminal's again", sys.stdout is original)
check("the token is gone", not remote.token())
try:
    code, _ = request("/", TOKEN)
    reachable = True
except Exception:
    reachable = False
check("nothing answers on the port any more", not reachable)

probe = socket.socket()
# The same option the server itself is opened with: what would otherwise be in
# the way is the TIME_WAIT left by the requests above, not the server.
probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    probe.bind(("127.0.0.1", port))
    freed = True
except OSError:
    freed = False
finally:
    probe.close()
check("and the port is free for the next thing", freed, f"port {port}")

check("asking a stopped remote answers nothing",
      remote.ask("gone", [], [("y", "Allow")], timeout=1) == "")
check("stopping twice is not an error", remote.stop() is None)

# ---------------------------------------------------------------------------
print("\n--- and the network case is not the local one ---")

config.REMOTE_PORT = 0
local = remote.start()
remote._drop_mirror()
local_token = local["token"]
remote.stop()
lan = remote.start("lan")
remote._drop_mirror()
check("opening it to the network binds every interface",
      lan["host"] == "0.0.0.0" and lan["scope"] == "lan", str(lan["host"]))
check("and issues a token of its own, longer than the local one",
      len(lan["token"]) > len(local_token),
      f"{len(local_token)} → {len(lan['token'])}")
check("the link it prints is one a phone can reach",
      any(not url.startswith("http://127.0.0.1") for url in lan["urls"])
      or lan["urls"] == [],           # a machine with no network of its own
      str(lan["urls"]))
check("nothing about it is reachable without that token",
      request("/", "")[0] == 401 and request("/", local_token)[0] == 401)
remote.stop()

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("remote control checks passed")
