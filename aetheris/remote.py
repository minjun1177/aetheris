"""This session, from the phone in your pocket.

A harness is a terminal, and a terminal is somewhere you have to be. That is
fine while the answer takes four seconds and wrong the moment it takes four
minutes: a `/deepthink` pass, a test suite the model is chasing, a sub-agent
reading half a repository. The work is happening on this machine and the
person has left the desk, so the two things they actually need - *what is it
doing* and *yes, go ahead* - are exactly the two things they cannot reach.

So the harness can hand out one door into the session it is already running.
`/remote on` opens an HTTP server on this machine, prints a URL with a token
in it, and anything that can open that URL is now at the prompt: it sees the
transcript as it is printed, types lines that arrive as if they had been typed
here, and answers the approval prompts that would otherwise sit waiting for a
keystroke from somebody who is on a train.

**It is a door into a shell.** Whoever holds the link can run what the person
at this keyboard can run. That is the feature, and it is also the whole of the
risk, so:

  * it is **off by default** and starts only on an explicit `/remote on`;
  * the token is generated per start, never saved, and never written into
    `settings.json` - there is no long-lived credential to leak. Loopback gets
    128 bits of it; `lan` gets 256, because that token is one that crosses a
    network somebody else is also on;
  * it binds **loopback** unless `/remote on lan` is typed, and that prints a
    warning naming what is now reachable;
  * where it is reachable over a network, **the link is not enough**: the
    browser is shown a box asking for six digits that were printed on *this
    terminal*, and gets a session key only when it sends them back. The token
    crosses the network to reach the phone and the terminal does not, which is
    the entire difference between a second factor and a second copy of the
    first. `REMOTE_PAIR` decides when it is asked for;
  * every request carries the token, compared with `secrets.compare_digest`;
  * wrong tokens are counted per address, and an address that has sent
    `REMOTE_MAX_BAD_TOKENS` of them is refused for `REMOTE_LOCKOUT` seconds -
    a 128-bit token is not guessable, but a door that lets somebody knock all
    afternoon without anyone hearing it is still the wrong door;
  * **the person is told who is there.** The first request from an address,
    and every attempt with a wrong token, becomes a line at the prompt. On a
    network you share, the useful question is not "could someone get in" but
    "did they", and nothing else here can answer it;
  * the `Host` header has to name this machine, which is what stops a page on
    the internet from walking into `127.0.0.1` through a rebound DNS name;
  * what is mirrored out goes through `vault.redact` first, so a `.env` value
    that is on the terminal because the person ran `!cat .env` is not also on
    the wire.

**What it is not.** This is plain HTTP. On loopback that is the whole story -
the bytes never leave the machine. Over `lan` they cross a network, and anyone
already on that network can read them: the transcript, and the token with it.
So `lan` is for a network you trust, and everything else is `ssh -L`, which is
somebody else's audited code and is why there is no tunnel and no TLS here.

**What the remote sees.** The screen, as text. Everything the harness prints
goes through `sys.stdout`, so mirroring is a tee installed on it rather than a
second rendering that would drift from the first: one transcript, two places,
and no second copy of the formatting to keep in step. ANSI is stripped, a line
being streamed is published as a `tail` before its newline arrives, and the
buffer is a ring (`REMOTE_LINES`) because a session outlives any phone screen.

**What the remote can do.** Type a line - a message or a slash command, both
go into the same loop by the same route - and answer a question. A line typed
remotely marks the turn it starts as *remotely driven*, and that is what
decides where the next approval prompt is asked: the person who started the
work is the person who gets asked to approve it. A question nobody answers
within `REMOTE_ASK_TIMEOUT` is refused, because the safe end of an unanswered
"may I delete this" is no.

Threads: the server is a `ThreadingHTTPServer` in a daemon thread, and every
piece of shared state below is behind one `Condition` - which is also how a
long poll waits without spinning, and how `ask` blocks the main thread until
an answer arrives or the deadline passes.
"""

import collections
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from aetheris import config
from aetheris import vault

FORMAT = 1

# Ceiling on one request body. A typed line is a sentence; anything of this
# size is either a mistake or somebody probing.
MAX_BODY = 64 * 1024

# How long a `/state` long poll is held open before answering with whatever it
# has. Under a mobile network's own idle timeout, and short enough that a
# phone that has gone to sleep reconnects rather than hanging.
HOLD_SECONDS = 25.0

# What a browser is allowed to call itself. Anything else is a name that
# resolved to this machine from somewhere it should not have.
_LOCAL_NAMES = {"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"}

# Paths a browser asks for on its own, with no token, because it is a browser.
_BROWSER_ASKS_ANYWAY = frozenset({
    "/favicon.ico", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png",
    "/robots.txt",
})

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
# The colour half of that: `ESC [ … m` and nothing else. It is the only escape
# the browser is allowed to see - a cursor move means nothing to a page and
# everything to whoever is reading it.
_NOT_SGR = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-ln-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

# Everything below is read and written from the server's threads and from the
# main one, so nothing here is touched outside `_wake`.
_wake = threading.Condition()

_server = None
_thread = None
_token = ""
_bound = ()                 # (host, port) actually bound, after the OS chose
_scope = ""                 # "lan" when it was opened to the network
_started = 0.0

_clients = {}               # address -> {"first", "last", "requests"}
_bad = {}                   # address -> {"count", "until"}
_notices = collections.deque(maxlen=50)      # for the prompt to print

_pairing = None             # the code a browser is being asked for, or None
_sessions = {}              # key -> {"address", "since", "last"}

_lines = collections.deque(maxlen=1)     # (seq, text); resized at start()
_seq = 0
_tail = ""                  # the line being printed, before its newline
_typed = collections.deque()             # lines from the remote, oldest first
_question = None            # the question waiting for an answer, or None
_busy = False
_usage = {}                 # what the conversation costs, for the page's strip
_driver = "terminal"        # who started the line being worked on
_last_seen = 0.0            # when a remote client last asked for state

_tee = None                 # the stdout wrapper, while one is installed


def _cfg(name, default):
    """A setting, whatever the running config says it is right now."""
    return getattr(config, name, default)


def _same_secret(offered, expected: str) -> bool:
    """A constant-time comparison that cannot be made to raise instead.

    `secrets.compare_digest` refuses two *strings* when either holds a
    character outside ASCII - it raises TypeError rather than answering False.
    Everything compared here arrives off the wire: a token out of the query
    string, percent-decoded as UTF-8, and a pairing code whose digits are
    filtered with `str.isdigit()`, which is true of `٣` as well as `3`. So one
    byte of UTF-8 in `?k=` took the handler out through `handle_error` - the
    connection dropped with no HTTP reply at all, and, worse, `note_bad_token`
    was never reached: an address could knock all afternoon without ever
    being counted towards `REMOTE_MAX_BAD_TOKENS` or reported at the prompt,
    which is the one warning this module promises on a shared network.

    Both sides are compared as UTF-8 bytes, which `compare_digest` takes
    without complaint and which cannot compare equal unless the strings do.
    """
    try:
        return secrets.compare_digest(str(offered).encode("utf-8", "surrogatepass"),
                                      expected.encode("utf-8", "surrogatepass"))
    except (TypeError, ValueError, UnicodeError):
        return False


def running() -> bool:
    return _server is not None


def token() -> str:
    return _token


# ---------------------------------------------------------------------------
# the transcript, mirrored off the terminal
# ---------------------------------------------------------------------------

class _Tee:
    """`sys.stdout`, with everything written to it also published.

    A wrapper rather than a `print` of our own: the harness prints from thirty
    places in six modules, and a second path would be a second thing to keep
    in step. Nothing here may raise - this *is* `print` for the rest of the
    program, and a remote that has gone wrong must not be what stops the
    terminal working.
    """

    def __init__(self, target):
        self.target = target

    def write(self, text):
        written = self.target.write(text)
        try:
            publish(text)
        except Exception:
            pass
        return written

    def flush(self):
        try:
            self.target.flush()
        except Exception:
            pass

    def isatty(self):
        try:
            return self.target.isatty()
        except Exception:
            return False

    def fileno(self):
        return self.target.fileno()

    def __getattr__(self, name):
        return getattr(self.target, name)


def ensure_mirror() -> None:
    """Make sure the tee is the outermost thing on `sys.stdout`.

    Called at the top of every prompt rather than once at start, because
    `prompt_toolkit`'s `patch_stdout` replaces `sys.stdout` for as long as a
    prompt is open and puts the original back afterwards. A tee installed
    *while* a prompt was open would be the thing thrown away, and the mirror
    would stop with nothing to say it had.
    """
    global _tee
    if not running() or isinstance(sys.stdout, _Tee):
        return
    _tee = _Tee(sys.stdout)
    sys.stdout = _tee


def unmirrored_stdout():
    """`sys.stdout` with the mirror taken off the front of it.

    What prompt_toolkit draws - the prompt, its completion menu, the redraw
    after every keystroke - is the terminal's own furniture, not transcript.
    Mirrored, it reached the phone as a bare `\u276f` before every line typed
    there. So the prompt is pointed at the real stream and the tee is put back
    on *inside* `patch_stdout`, where it catches what the program prints and
    not what the renderer draws.
    """
    out = sys.stdout
    while isinstance(out, _Tee):
        out = out.target
    return out


def _drop_mirror() -> None:
    global _tee
    if isinstance(sys.stdout, _Tee):
        sys.stdout = sys.stdout.target
    _tee = None


def publish(text: str) -> None:
    """Add what was just printed to the transcript the remote reads.

    Whole lines land in the ring; a line still being streamed is kept as the
    `tail`, so a phone shows an answer arriving rather than a blank screen
    until the paragraph ends. A `\\r` means the terminal was about to overwrite
    what it had - a spinner, a progress line - so only what follows the last
    one survives.
    """
    global _seq, _tail
    if not text:
        return
    with _wake:
        buffer = _tail + text
        parts = buffer.split("\n")
        # The same rule for the line still being written as for the ones that
        # are finished: a `\r` means the terminal was about to draw over what
        # it had. Without it the spinner - which is one line, rewritten twenty
        # times a second and never ended with a newline - reached the phone as
        # every frame it had ever drawn, laid end to end.
        _tail = parts.pop().rsplit("\r", 1)[-1]
        for line in parts:
            line = line.rsplit("\r", 1)[-1]
            _seq += 1
            _lines.append((_seq, _clean(line)))
        if parts or _tail:
            _wake.notify_all()


def _clean(line: str) -> str:
    """One line as the remote should see it: colour kept, nothing else.

    The transcript is the terminal's own output, and half of what the terminal
    says is *how* it says it - a refusal in red, a tool call in grey, the
    model's answer in white. Stripping that left the phone with a wall of
    identical text that is genuinely harder to read than the screen it came
    from. So the colour survives to the browser, which paints it, and every
    other escape - the cursor moves, the erases - is taken out here, where it
    cannot mean anything to a page.
    """
    line = _NOT_SGR.sub("", line).replace("\t", "    ").rstrip()
    try:
        return vault.redact(line)
    except Exception:
        return line


def _plain(text: str) -> str:
    """The same, with the colour taken out too: for a question's own words."""
    return _ANSI.sub("", _clean(text))


def transcript(since: int = 0) -> list:
    """The lines numbered above `since`, oldest first."""
    with _wake:
        return [[seq, text] for seq, text in _lines if seq > since]


# ---------------------------------------------------------------------------
# lines typed on the remote
# ---------------------------------------------------------------------------

def take_line() -> str:
    """The oldest line the remote typed, or "" when there is none.

    Taken once: the caller is the prompt, and a line handed to it twice would
    be a message sent twice.
    """
    with _wake:
        line = _typed.popleft() if _typed else ""
        if line:
            _wake.notify_all()
    return line


def queued() -> int:
    with _wake:
        return len(_typed)


def set_busy(flag: bool) -> None:
    """Whether the harness is working on something, for the remote's badge."""
    global _busy
    with _wake:
        if _busy != bool(flag):
            _busy = bool(flag)
            _wake.notify_all()


def set_usage(used: int, budget: int, turns: int = 0) -> None:
    """What this conversation is costing, for the strip above the phone's box.

    Pushed in rather than worked out here: the conversation lives in `app`, and
    a module that opens a socket has no business reaching into it. Numbers
    only - the page draws them.
    """
    global _usage
    with _wake:
        _usage = {"used": int(used), "budget": int(budget), "turns": int(turns)}
        _wake.notify_all()


def set_driver(who: str) -> None:
    """Who started the line being worked on - `remote` or `terminal`."""
    global _driver
    with _wake:
        _driver = "remote" if who == "remote" else "terminal"


def driven() -> bool:
    """Is a remotely typed line what the harness is working on?"""
    with _wake:
        return _driver == "remote" and _server is not None


# ---------------------------------------------------------------------------
# who is at the door
# ---------------------------------------------------------------------------

def _note(text: str) -> None:
    """Something the person should know, kept for the next free prompt.

    Never printed from here. A handler thread printing would land in the
    middle of a streaming answer, and the channel had this problem first: the
    prompt is where a message from elsewhere belongs, which is where
    `take_notices` is read.
    """
    with _wake:
        _notices.append((time.time(), text))


def take_notices() -> list:
    """Drain what has happened at the door since this was last asked."""
    with _wake:
        found = list(_notices)
        _notices.clear()
    return [text for _, text in found]


def locked_out(who: str) -> float:
    """Seconds this address is still refused for, or 0."""
    with _wake:
        record = _bad.get(who)
        if not record:
            return 0.0
        return max(0.0, record["until"] - time.time())


def note_bad_token(who: str) -> None:
    """Count a wrong token, and shut the address out if there are enough.

    The first one is said out loud, because on a network somebody else is on,
    one wrong token is the only warning there is going to be. The ones after
    it are not: a locked-out script knocking four times a second must not be
    able to fill the terminal with its own noise.
    """
    limit = max(1, int(_cfg("REMOTE_MAX_BAD_TOKENS", 20)))
    for_how_long = max(1.0, float(_cfg("REMOTE_LOCKOUT", 300)))
    with _wake:
        record = _bad.setdefault(who, {"count": 0, "until": 0.0})
        record["count"] += 1
        count = record["count"]
        if count >= limit:
            record["until"] = time.time() + for_how_long
            record["count"] = 0
    if count == 1:
        _note(f"{who} tried the remote with a token that is not this one.")
    elif count >= limit:
        _note(f"{who} has tried {limit} wrong tokens - refused for "
              f"{int(for_how_long)}s. Somebody is guessing; /remote off closes it.")


def note_client(who: str) -> None:
    """A request that got past the token. The first from an address is news."""
    global _last_seen
    with _wake:
        _last_seen = time.time()
        _bad.pop(who, None)          # it is the holder of the link, fumbling
        seen = _clients.get(who)
        if seen:
            seen["last"], seen["requests"] = _last_seen, seen["requests"] + 1
            return
        _clients[who] = {"first": _last_seen, "last": _last_seen, "requests": 1}
    _note(f"{who} opened the remote link.")


def clients() -> list:
    """Who has been through the door, newest first."""
    with _wake:
        return sorted(({"address": who, **rest} for who, rest in _clients.items()),
                      key=lambda row: row["last"], reverse=True)


# ---------------------------------------------------------------------------
# pairing: the second thing a browser has to have, and where it comes from
# ---------------------------------------------------------------------------

# How long a code is worth typing, and how many guesses it survives. Six digits
# is a million, and three guesses inside two minutes is not a way in.
PAIR_SECONDS = 120
PAIR_TRIES = 3


def pairing_required() -> bool:
    """Is the token alone enough to drive this session?

    Over `lan` it is not, and that is the whole of this feature. The link has
    to cross a network to reach the phone - read aloud, photographed, sitting
    in somebody's browser history - and the one thing an attacker who has the
    link still cannot do is read the terminal it came from. So the second
    factor is a code that only appears *there*: holding the link gets you a
    box asking for six digits, and nothing else.
    """
    mode = str(_cfg("REMOTE_PAIR", "lan")).strip().lower()
    if mode in ("always", "on", "true", "yes"):
        return True
    if mode in ("never", "off", "false", "no"):
        return False
    return _scope == "lan"


def begin_pairing(who: str) -> dict:
    """Put a code on the terminal for the browser at `who` to be told.

    One at a time: a second browser asking while a code is outstanding gets
    the same code rather than replacing it, because two codes on screen with
    nothing to say which is which is how a person ends up typing the
    attacker's one.
    """
    global _pairing
    with _wake:
        live = (_pairing and _pairing["until"] > time.time())
        if live and _pairing["address"] == who:
            return {"wanted": True, "seconds": int(_pairing["until"] - time.time())}
        if live:
            return {"wanted": True, "busy": True,
                    "seconds": int(_pairing["until"] - time.time())}
        code = f"{secrets.randbelow(1000000):06d}"
        _pairing = {"code": code, "address": who, "tries": 0,
                    "until": time.time() + PAIR_SECONDS}
        _wake.notify_all()
    _note(f"{who} wants to drive this session. Code: {code[:3]} {code[3:]} "
          f"- type it there within {PAIR_SECONDS // 60} minutes. "
          f"If this is not you, /remote off.")
    return {"wanted": True, "seconds": PAIR_SECONDS}


def complete_pairing(who: str, offered: str) -> str:
    """The code, checked. Returns a session key, or "" for anything else."""
    global _pairing
    offered = "".join(ch for ch in str(offered) if ch.isdigit())
    with _wake:
        if not _pairing or _pairing["until"] <= time.time():
            _pairing = None
            return ""
        if _pairing["address"] != who:
            return ""
        if not _same_secret(offered, _pairing["code"]):
            _pairing["tries"] += 1
            spent = _pairing["tries"]
            if spent >= PAIR_TRIES:
                _pairing = None
            _wake.notify_all()
            if spent >= PAIR_TRIES:
                _note(f"{who} got the pairing code wrong {PAIR_TRIES} times. "
                      f"The code is dead; they can ask for another.")
            return ""
        key = secrets.token_urlsafe(24)
        _sessions[key] = {"address": who, "since": time.time(), "last": time.time()}
        _pairing = None
        _wake.notify_all()
    _note(f"{who} paired and is now at this prompt. /remote forget drops it.")
    return key


def paired(key: str, who: str) -> bool:
    """Is this a session key that was issued, to this address?"""
    if not key:
        return False
    with _wake:
        session = _sessions.get(key)
        if not session or session["address"] != who:
            return False
        session["last"] = time.time()
        return True


def sessions() -> list:
    """The browsers that have paired, newest first."""
    with _wake:
        return sorted(({"address": s["address"], "since": s["since"], "last": s["last"]}
                       for s in _sessions.values()),
                      key=lambda row: row["last"], reverse=True)


def forget_sessions() -> int:
    """Drop every paired browser. They can pair again; the link still works."""
    global _pairing
    with _wake:
        count = len(_sessions)
        _sessions.clear()
        _pairing = None
        _wake.notify_all()
    return count


# ---------------------------------------------------------------------------
# questions, and where they are asked
# ---------------------------------------------------------------------------

def ask(title: str, details, choices, free_text: bool = False,
        timeout: float = 0.0) -> str:
    """Put a question to the remote and wait. "" means nobody answered.

    The empty string is deliberately the same answer as a timeout and as a
    remote that has closed its browser, because every caller treats "no answer"
    as "no": an approval prompt refuses, a plan is not approved. A question
    that expires leaves nothing behind - the next one is asked cleanly.
    """
    global _question
    if not running():
        return ""
    deadline = time.time() + (timeout or float(_cfg("REMOTE_ASK_TIMEOUT", 300)))
    ticket = secrets.token_urlsafe(8)
    with _wake:
        _question = {
            "id": ticket,
            "title": title,
            "details": [[_plain(str(label)), _plain(str(value))] for label, value in details],
            "choices": [[str(value), _plain(str(label))] for value, label in choices],
            "free_text": bool(free_text),
            "asked": time.time(),
            "answer": None,
        }
        _wake.notify_all()
        while _question is not None and _question["id"] == ticket:
            if _question["answer"] is not None:
                answer = _question["answer"]
                _question = None
                _wake.notify_all()
                return answer
            if time.time() >= deadline:
                _question = None
                _wake.notify_all()
                return ""
            # Short waits rather than one long one: on the main thread this is
            # what keeps Ctrl+C able to interrupt a question nobody is going
            # to answer.
            _wake.wait(0.25)
    return ""


def answer(ticket: str, value: str) -> bool:
    """Answer the pending question. False when it is not the one being asked.

    The id is checked rather than assumed: a phone that was showing yesterday's
    question and pressed Allow must not approve today's `rm -rf`.
    """
    with _wake:
        if not _question or _question["id"] != ticket:
            return False
        _question["answer"] = str(value)
        _wake.notify_all()
        return True


def pending() -> dict | None:
    with _wake:
        if not _question:
            return None
        shown = {k: v for k, v in _question.items() if k != "answer"}
        return shown


# ---------------------------------------------------------------------------
# the server
# ---------------------------------------------------------------------------

def _lan_address() -> str:
    """The address this machine has on its own network, or "".

    A UDP socket that is never sent on: connecting one only asks the routing
    table which interface would be used, which is the question being asked.
    """
    probe = None
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.settimeout(0.2)
        probe.connect(("192.0.2.1", 9))          # TEST-NET-1: routed nowhere
        return probe.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return ""
    finally:
        if probe is not None:
            try:
                probe.close()
            except Exception:
                pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        """A dropped connection is not news, and never a stack trace.

        `socketserver` prints the whole traceback to stderr when a handler
        raises, which lands in the middle of the conversation. A phone that
        locks its screen, a tab that closes, a long poll the OS tears down -
        Windows raises `ConnectionAbortedError` for all three - are the normal
        end of a request here, not a fault. Anything that is *not* a socket
        giving way is worth one line at the prompt, because a handler that
        keeps failing is a remote that has quietly stopped working.
        """
        kind, problem, _ = sys.exc_info()
        if kind is None or issubclass(kind, OSError):
            return
        try:
            _note(f"the remote hit {kind.__name__}: {problem}")
        except Exception:
            pass


def start(scope: str = "") -> dict:
    """Open the door. Returns what to tell the person; raises on failure.

    `scope` of `lan` binds every interface; anything else binds loopback, and
    loopback is the default because the failure mode of getting this wrong is
    a shell on somebody else's network.
    """
    global _server, _thread, _token, _bound, _scope, _started, _lines, _seq
    if running():
        return status()

    host = "0.0.0.0" if scope == "lan" else str(_cfg("REMOTE_HOST", "127.0.0.1"))
    first = int(_cfg("REMOTE_PORT", 8765))
    with _wake:
        _lines = collections.deque(_lines, maxlen=max(1, int(_cfg("REMOTE_LINES", 500))))
        _seq = _lines[-1][0] if _lines else 0

    server, problem = None, None
    # One port, then the next few: 8765 being busy is routinely this harness's
    # own second window, and refusing over that would be an error about
    # nothing the person did wrong.
    for port in ([first] if first == 0 else range(first, first + 20)):
        try:
            server = _Server((host, port), _Handler)
            break
        except OSError as e:
            problem = e
    if server is None:
        raise RuntimeError(f"could not open a port on {host}: {problem}")

    # Twice the token for the network case. 128 bits is already not guessable,
    # and the extra is not really about guessing: a token that leaves this
    # machine is a token that can be written down, shoulder-read off a screen
    # or left in somebody's browser history, and there is no reason to be
    # stingy with the one that does.
    _token = secrets.token_urlsafe(32 if scope == "lan" else 16)
    _bound = (host, server.server_address[1])
    _scope = "lan" if scope == "lan" else ""
    _started = time.time()
    _server = server
    _thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2},
                               name="aetheris-remote", daemon=True)
    _thread.start()
    ensure_mirror()
    return status()


def reconfigure() -> dict:
    """Make a setting changed since `start()` true of the door that is open.

    A setting that is changed while the thing it configures is running should
    either take effect or say it has not, and `/set REMOTE_PORT 9000` at a
    prompt with a remote already open is exactly somebody saying "move it".
    So it moves - which means a new token and a dead link, and the caller is
    told so it can print the new one.

    Returns what changed: `{"rebound": (host, port)}` when the address moved,
    `{"resized": n}` when only the transcript did, `{}` when nothing did.
    """
    if not running():
        return {}
    wanted = ("0.0.0.0" if _scope == "lan" else str(_cfg("REMOTE_HOST", "127.0.0.1")),
              int(_cfg("REMOTE_PORT", 8765)))
    # Port 0 means "any", and the OS already answered it: re-reading that as a
    # change would move the door on every unrelated `/set`.
    moved = wanted[0] != _bound[0] or (wanted[1] and wanted[1] != _bound[1])
    if moved:
        scope = _scope
        stop()
        start(scope)
        return {"rebound": _bound}

    kept = max(1, int(_cfg("REMOTE_LINES", 500)))
    if kept != _lines.maxlen:
        _resize(kept)
        return {"resized": kept}
    return {}


def _resize(kept: int) -> None:
    global _lines
    with _wake:
        _lines = collections.deque(_lines, maxlen=kept)


def stop() -> None:
    """Close the door, and forget the token that opened it."""
    global _server, _thread, _token, _bound, _scope, _question, _last_seen, _pairing
    server, _server = _server, None
    _drop_mirror()
    with _wake:
        _question = None
        _typed.clear()
        _wake.notify_all()
    if server is not None:
        try:
            # From this thread, never from a handler's: `shutdown` waits for
            # the serving loop to notice, and the serving loop is what a
            # handler is running inside.
            server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass
    if _thread is not None:
        _thread.join(timeout=2.0)
    _thread = None
    _token = ""
    _bound = ()
    _scope = ""
    # Who was at the door is forgotten with the door. The addresses were only
    # ever here so the person could be told about them, and a list of who
    # visited outliving the thing they visited is a record nobody asked for.
    # `_last_seen` goes with them, or the next door would open claiming it had
    # just been read by somebody who was at the last one.
    with _wake:
        _clients.clear()
        _bad.clear()
        _sessions.clear()
        _pairing = None
        _usage.clear()
        _last_seen = 0.0


def urls() -> list:
    """Every address this server can be reached at, token included."""
    if not running():
        return []
    host, port = _bound
    names = ["127.0.0.1"] if host in ("0.0.0.0", "127.0.0.1", "") else [host]
    if host == "0.0.0.0":
        lan = _lan_address()
        if lan and lan not in names:
            names.append(lan)
    return [f"http://{name}:{port}/?k={_token}" for name in names]


def status() -> dict:
    """What `/remote` prints, and what the tests read."""
    with _wake:
        return {
            "running": _server is not None,
            "host": _bound[0] if _bound else "",
            "port": _bound[1] if _bound else 0,
            "scope": _scope or "local",
            "token": _token,
            "urls": urls(),
            "clients": len(_clients),
            "pairing": pairing_required(),
            "paired": len(_sessions),
            "refused": sorted(who for who, record in _bad.items()
                              if record["until"] > time.time()),
            "lines": len(_lines),
            "queued": len(_typed),
            "driver": _driver,
            "asking": bool(_question),
            "seen": _last_seen,
            "since": _started,
        }


# ---------------------------------------------------------------------------
# the requests
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    server_version = "aetheris"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        """Nothing. The terminal belongs to the conversation, not to a log."""

    # -- what every request has to get past ---------------------------------

    def _path_and_query(self):
        path, _, query = self.path.partition("?")
        fields = {}
        for pair in query.split("&"):
            if not pair:
                continue
            key, _, value = pair.partition("=")
            fields[key] = _unquote(value)
        return path, fields

    def _authorised(self, fields) -> bool:
        offered = fields.get("k", "")
        header = self.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            offered = header[7:].strip()
        return bool(_token) and _same_secret(offered, _token)

    def _host_is_this_machine(self) -> bool:
        """Refuse a `Host` this server was not asked for.

        A token in the URL stops somebody guessing their way in. It does not
        stop a page the person has open from resolving its own name to
        127.0.0.1 and talking to whatever answers - and this answers. What a
        rebound name cannot do is put a plausible `Host` on the request.
        """
        raw = self.headers.get("Host", "")
        name = raw.rsplit(":", 1)[0] if raw.count(":") == 1 else raw
        name = name.strip("[]").lower() or "localhost"
        if name in _LOCAL_NAMES:
            return True
        if _bound and name == str(_bound[0]).lower():
            return True
        # An address is a name nobody had to resolve, so a bare IPv4 that is
        # this machine's own is allowed - that is how a phone reaches it.
        return bool(re.fullmatch(r"[0-9a-f.:]+", name))

    def _reply(self, code: int, body: bytes, kind: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # Nothing here is meant to be reachable from another page's script.
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8", "replace")
        self._reply(code, body, "application/json; charset=utf-8")

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > MAX_BODY:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8", "replace"))
        except Exception:
            return {}

    # -- the four things it answers -----------------------------------------

    def _who(self) -> str:
        return self.client_address[0] if self.client_address else "?"

    def _refusal(self, path, fields, needs_pairing: bool = True):
        """Everything a request has to get past, in the order it has to.

        The `Host` first, because a request from a page that resolved its own
        name here should not even get to spend a guess. Then the lockout, so
        an address that is guessing cannot keep guessing. Then the token. Then
        - where pairing is required - the session key that only a browser
        which was told the code off the terminal can have.

        Returns `(code, message)` or `None`.
        """
        if not self._host_is_this_machine():
            return 403, "wrong host"
        if path in _BROWSER_ASKS_ANYWAY:
            # A browser fetches these by itself, without the token, the moment
            # the page loads. Counting them as somebody trying a wrong token
            # meant opening the link reported *you* at your own prompt as an
            # intruder - twice, once for the tab and once for the icon.
            return 404, "no such thing here"
        who = self._who()
        waiting = locked_out(who)
        if waiting:
            return 429, f"too many wrong tokens - try again in {int(waiting) + 1}s"
        if not self._authorised(fields):
            note_bad_token(who)
            return 401, "a token is needed"
        note_client(who)
        if needs_pairing and pairing_required():
            key = self.headers.get("X-Remote-Session", "") or fields.get("s", "")
            if not paired(key.strip(), who):
                return 403, "pair"
        return None

    def do_GET(self):                                    # noqa: N802
        path, fields = self._path_and_query()
        # The page itself needs no pairing: it is the thing that asks for the
        # code, and a browser that cannot load it cannot be told what to type.
        # It carries nothing about this session - the transcript is behind
        # `/state`, which does need one.
        refused = self._refusal(path, fields,
                                needs_pairing=path not in ("/", "/index.html"))
        if refused:
            return self._json(refused[0], {"error": refused[1]})

        if path in ("/", "/index.html"):
            page = PAGE.replace("__TOKEN__", _token)
            return self._reply(200, page.encode("utf-8"), "text/html; charset=utf-8")

        if path == "/commands":
            # The same table `/help` renders, so the phone cannot be offered a
            # command the terminal does not answer - and cannot miss one it
            # does. Asked once by the page and kept; it does not change while
            # a session is running.
            try:
                from aetheris import tui
                rows = [{"name": name, "help": description}
                        for name, description in tui.COMMANDS]
                rows += [{"name": name, "help": description}
                         for name, description in tui.PREFIXES]
            except Exception:
                rows = []
            return self._json(200, {"commands": rows})

        if path == "/state":
            try:
                since = int(fields.get("since", "0"))
            except ValueError:
                since = 0
            if fields.get("wait") == "1":
                self._hold(since)
            return self._json(200, _state(since))

        return self._json(404, {"error": "no such thing here"})

    def do_POST(self):                                   # noqa: N802
        path, fields = self._path_and_query()
        refused = self._refusal(path, fields, needs_pairing=path != "/pair")
        if refused:
            return self._json(refused[0], {"error": refused[1]})
        payload = self._body()

        if path == "/pair":
            if not pairing_required():
                return self._json(200, {"ok": True, "session": ""})
            offered = str(payload.get("code", "")).strip()
            if not offered:
                return self._json(200, begin_pairing(self._who()))
            key = complete_pairing(self._who(), offered)
            return self._json(200 if key else 403,
                              {"ok": bool(key), "session": key,
                               "error": "" if key else "that is not the code"})

        if path == "/say":
            text = str(payload.get("text", "")).strip()
            if not text:
                return self._json(400, {"error": "nothing to say"})
            text = config.safe_text(text)[:MAX_BODY]
            with _wake:
                _typed.append(text)
                _wake.notify_all()
            return self._json(200, {"ok": True, "queued": queued()})

        if path == "/answer":
            took = answer(str(payload.get("id", "")), str(payload.get("value", "")))
            return self._json(200 if took else 409,
                              {"ok": took,
                               "error": "" if took else "that question is gone"})

        return self._json(404, {"error": "no such thing here"})

    def _hold(self, since: int) -> None:
        """Wait for something to be worth answering with, or time out.

        A phone asking four times a second is a phone with no battery, and a
        phone asking every thirty is a phone that shows an approval prompt
        half a minute late. So it asks once and this holds the answer.
        """
        deadline = time.time() + HOLD_SECONDS
        with _wake:
            # The question is held by id rather than by whether there is one:
            # one question answered and the next asked between two polls is
            # the case a boolean cannot see, and it is the case where being
            # late matters most.
            was_busy, was_usage = _busy, dict(_usage)
            was_asking = _question["id"] if _question else ""
            while (_seq <= since and _busy == was_busy and _server is not None
                   and _usage == was_usage
                   and (_question["id"] if _question else "") == was_asking):
                if time.time() >= deadline:
                    return
                _wake.wait(0.5)


def _state(since: int) -> dict:
    """Everything the page draws itself from, in one answer."""
    with _wake:
        lines = [[seq, text] for seq, text in _lines if seq > since]
        question = None
        if _question:
            question = {k: v for k, v in _question.items() if k != "answer"}
        return {
            "format": FORMAT,
            "seq": _seq,
            "lines": lines,
            "tail": _clean(_tail),
            "busy": _busy,
            "usage": dict(_usage),
            "driver": _driver,
            "queued": len(_typed),
            "question": question,
            "title": getattr(config, "SESSION_TITLE", "") or "",
            "model": getattr(config, "MODEL", ""),
            "cwd": os.getcwd(),
        }


def _unquote(text: str) -> str:
    """Percent-decoding, without dragging `urllib` in for one field."""
    from urllib.parse import unquote_plus
    try:
        return unquote_plus(text)
    except Exception:
        return text


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------

# One file, no network: a phone on a LAN with no route out still has to be
# able to draw this, so there is nothing to fetch and nothing to fail.
PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="referrer" content="no-referrer">
<title>Aetheris</title>
<style>
:root { color-scheme: dark; --bg:#141414; --panel:#1c1c1c; --line:#2c2c2c;
        --text:#e8e4dc; --muted:#8b8b8b; --accent:#d3a04a; --ok:#98c379;
        --warn:#e5b567; --err:#e06c75; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:14px/1.5
       ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
       display:flex; flex-direction:column; height:100dvh; }
header { display:flex; gap:8px; align-items:center; padding:10px 14px;
         border-bottom:1px solid var(--line); background:var(--panel); }
header b { color:var(--accent); font-weight:600; }
header span { color:var(--muted); font-size:12px; overflow:hidden;
              text-overflow:ellipsis; white-space:nowrap; }
#dot { width:8px; height:8px; border-radius:50%; background:var(--muted);
       flex:none; }
#dot.busy { background:var(--warn); animation:pulse 1s infinite; }
#dot.live { background:var(--ok); }
@keyframes pulse { 50% { opacity:.25; } }
#log { flex:1; overflow-y:auto; padding:12px 14px 4px; white-space:pre-wrap;
       overflow-wrap:anywhere; }
#log .t { color:var(--muted); }
#ask { margin:10px 14px; padding:12px; border:1px solid var(--warn);
       border-radius:8px; background:#1f1a12; display:none; }
#ask h3 { margin:0 0 8px; font-size:13px; color:var(--warn); }
#ask dl { margin:0 0 10px; font-size:12px; }
#ask dt { color:var(--muted); }
#ask dd { margin:0 0 6px; overflow-wrap:anywhere; }
#ask .row { display:flex; gap:8px; flex-wrap:wrap; }
button { font:inherit; padding:9px 14px; border-radius:6px; cursor:pointer;
         border:1px solid var(--line); background:#242424; color:var(--text); }
button.go { border-color:var(--ok); color:var(--ok); }
button.no { border-color:var(--err); color:var(--err); }
form { display:flex; gap:8px; padding:10px 14px calc(10px + env(safe-area-inset-bottom));
       border-top:1px solid var(--line); background:var(--panel); }
#menu { display:none; max-height:45vh; overflow-y:auto; border-top:1px solid var(--line);
        background:var(--panel); }
#menu button { display:block; width:100%; text-align:left; border:0; border-radius:0;
               background:none; padding:9px 14px; border-bottom:1px solid #222; }
#menu button:active { background:#242424; }
#menu b { color:var(--accent); font-weight:600; }
#menu span { color:var(--muted); display:block; font-size:12px; margin-top:2px; }
#shell { display:none; padding:8px 14px; background:#231d10; color:var(--warn);
         border-top:1px solid var(--warn); font-size:12px; }
#cost { display:flex; align-items:center; gap:8px; padding:4px 14px 0;
        font-size:11px; color:var(--muted); }
#cost .grow { flex:1; }
#spin { color:var(--accent); min-width:1.2em; }
#spin b { color:var(--muted); font-weight:400; margin-left:4px; }
#cost .bar { width:72px; flex:none; height:3px; border-radius:2px;
             background:#2a2a2a; overflow:hidden; }
#cost .bar i { display:block; height:100%; width:0; background:var(--ok); }
#cost.warm .bar i { background:var(--warn); }
#cost.full .bar i { background:var(--err); }
body.shell input { color:var(--warn); border-color:var(--warn); }
input { flex:1; min-width:0; font:inherit; padding:11px 12px; border-radius:6px;
        border:1px solid var(--line); background:#111; color:var(--text); }
input:focus { outline:1px solid var(--accent); }
#pair { position:fixed; inset:0; background:var(--bg); display:none;
        flex-direction:column; justify-content:center; align-items:center;
        gap:14px; padding:24px; text-align:center; z-index:10; }
#pair h2 { margin:0; font-size:16px; color:var(--accent); }
#pair p { margin:0; color:var(--muted); max-width:28em; line-height:1.6; }
#pair input { width:10ch; text-align:center; font-size:22px; letter-spacing:4px; }
#pair .row { display:flex; gap:8px; align-items:center; }
#pairnote { color:var(--err); min-height:1.2em; }
</style></head><body>
<header><i id="dot"></i><b>Aetheris</b><span id="where">connecting…</span></header>
<div id="log"></div>
<div id="pair">
  <h2>Enter the code on the terminal</h2>
  <p>This link reached you over a network. To drive the session you also need
     the six digits the harness just printed in the terminal it is running in -
     which is the part nobody else on the network can see.</p>
  <div class="row">
    <input id="code" inputmode="numeric" autocomplete="one-time-code"
           maxlength="7" placeholder="000000">
    <button id="pairgo">Pair</button>
  </div>
  <div id="pairnote"></div>
</div>
<div id="ask"><h3></h3><dl></dl><div class="row"></div></div>
<div id="menu"></div>
<div id="shell"><b>Shell</b> - runs on that machine as you; not sent to the model</div>
<div id="cost" hidden><span id="spin"></span><span class="grow"></span>
  <span id="costtext"></span><span class="bar"><i></i></span></div>
<form id="say"><input id="text" placeholder="message, or /command"
  autocomplete="off" autocapitalize="off" autocorrect="off"><button>Send</button></form>
<script>
const KEY = "__TOKEN__";
const log = document.getElementById("log"), ask = document.getElementById("ask");
const dot = document.getElementById("dot"), where = document.getElementById("where");
const pair = document.getElementById("pair"), pairnote = document.getElementById("pairnote");
let seq = 0, tailNode = null, asking = "", pairing = false;

// The session the harness issued after the code was typed. Per browser, and
// only ever in this browser: the terminal is the other half of it.
function session() { try { return localStorage.getItem("session") || ""; } catch (e) { return ""; } }
function remember(key) { try { localStorage.setItem("session", key); } catch (e) {} }

async function call(path, body) {
  const options = { headers: { "X-Remote-Session": session() } };
  if (body !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const answer = await fetch(path + (path.includes("?") ? "&" : "?") + "k=" + KEY, options);
  if (answer.status === 403) {
    const said = await answer.clone().json().catch(() => ({}));
    if (said.error === "pair") { askToPair(); throw new Error("pair"); }
  }
  return answer;
}

async function askToPair() {
  if (pairing) return;
  pairing = true;
  pair.style.display = "flex";
  await fetch("/pair?k=" + KEY, { method: "POST",
    headers: { "Content-Type": "application/json" }, body: "{}" });
  document.getElementById("code").focus();
}

document.getElementById("pairgo").onclick = async () => {
  const box = document.getElementById("code");
  const answer = await fetch("/pair?k=" + KEY, { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code: box.value }) });
  const said = await answer.json().catch(() => ({}));
  if (said.session) {
    remember(said.session);
    pairing = false; pair.style.display = "none"; pairnote.textContent = "";
    box.value = "";
    poll();
  } else {
    pairnote.textContent = said.error || "that is not the code";
    box.value = "";
    box.focus();
  }
};
document.getElementById("code").addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("pairgo").click();
});

function atBottom() { return log.scrollHeight - log.scrollTop - log.clientHeight < 60; }

// The transcript arrives with the terminal's own colours still on it, as SGR
// escapes. Painting them is what makes it readable: a refusal in red and a
// tool call in grey say as much as the words do. Only `ESC [ … m` reaches the
// browser - everything else was taken out before it was sent - and the text
// itself only ever goes in as textContent, so nothing here can be markup.
const BASIC = ["#1c1c1c", "#e06c75", "#98c379", "#e5b567", "#61afef", "#c678dd",
               "#56b6c2", "#e8e4dc"];

// Colour outlives a line on a terminal: the banner opens with one escape and
// closes four lines later, and every line between them is painted by a state
// nothing on that line mentions. Publishing by the line would lose all of it -
// the logo came out white - so the state carries, exactly as it does there.
let carried = [null, false];

function paint(target, text, keep) {
  const parts = text.split(/\\x1b\\[([0-9;]*)m/);
  let [colour, bold] = carried;
  for (let i = 0; i < parts.length; i++) {
    if (i % 2 === 1) { [colour, bold] = sgr(parts[i], colour, bold); continue; }
    if (!parts[i]) continue;
    const span = document.createElement("span");
    span.textContent = parts[i];
    if (colour) span.style.color = colour;
    if (bold) span.style.fontWeight = "600";
    target.appendChild(span);
  }
  // The tail is the line being written and is redrawn from the same state
  // every poll, so it must not advance it.
  if (keep !== false) carried = [colour, bold];
}

function sgr(codes, colour, bold) {
  const n = codes.split(";").map(x => parseInt(x || "0", 10));
  for (let i = 0; i < n.length; i++) {
    if (n[i] === 0) { colour = null; bold = false; }
    else if (n[i] === 1) bold = true;
    else if (n[i] === 22) bold = false;
    else if (n[i] === 39) colour = null;
    else if (n[i] === 38 && n[i + 1] === 2) {
      const [r, g, b] = [n[i + 2] | 0, n[i + 3] | 0, n[i + 4] | 0];
      colour = `rgb(${r & 255},${g & 255},${b & 255})`; i += 4;
    }
    else if (n[i] === 38 && n[i + 1] === 5) { colour = null; i += 2; }
    else if (n[i] >= 30 && n[i] <= 37) colour = BASIC[n[i] - 30];
    else if (n[i] >= 90 && n[i] <= 97) colour = BASIC[n[i] - 90];
  }
  return [colour, bold];
}

function add(text, cls) {
  const stick = atBottom();
  const div = document.createElement("div");
  if (cls) div.className = cls;
  if (text === "") div.textContent = "\\u00a0"; else paint(div, text);
  log.appendChild(div);
  while (log.childNodes.length > 1200) log.removeChild(log.firstChild);
  if (stick) log.scrollTop = log.scrollHeight;
}

function drawAsk(q) {
  if (!q) { ask.style.display = "none"; asking = ""; return; }
  if (q.id === asking) return;
  asking = q.id;
  ask.querySelector("h3").textContent = q.title;
  const dl = ask.querySelector("dl"); dl.innerHTML = "";
  for (const [label, value] of q.details) {
    const dt = document.createElement("dt"), dd = document.createElement("dd");
    dt.textContent = label; dd.textContent = value;
    dl.append(dt, dd);
  }
  const row = ask.querySelector(".row"); row.innerHTML = "";
  for (const [value, label] of q.choices) {
    const b = document.createElement("button");
    b.textContent = label;
    b.className = /^(y|1|a)$/.test(value) ? "go" : (/^(n|2)$/.test(value) ? "no" : "");
    b.onclick = () => reply(q.id, value);
    row.appendChild(b);
  }
  if (q.free_text) {
    const b = document.createElement("button");
    b.textContent = "Type an answer…";
    b.onclick = () => { const t = prompt(q.title); if (t) reply(q.id, t); };
    row.appendChild(b);
  }
  ask.style.display = "block";
  ask.scrollIntoView({ block: "nearest" });
}

async function reply(id, value) {
  ask.style.display = "none"; asking = "";
  await call("/answer", { id, value }).catch(() => {});
}

// What the terminal shows when a line starts with `/` or `!`, shown here for
// the same reason: on a phone there is no /help to have read, and no way to
// tell a command for that machine from a message for the model until it runs.
const menu = document.getElementById("menu"), shell = document.getElementById("shell");
let commands = null;

async function loadCommands() {
  if (commands) return commands;
  try {
    const r = await call("/commands");
    commands = (await r.json()).commands || [];
  } catch (e) { commands = []; }
  return commands;
}

function closeMenu() { menu.style.display = "none"; menu.innerHTML = ""; }

async function reflect() {
  const box = document.getElementById("text"), text = box.value;
  const isShell = text.startsWith("!");
  shell.style.display = isShell ? "block" : "none";
  document.body.classList.toggle("shell", isShell);
  if (!text.startsWith("/")) return closeMenu();

  const typed = text.split(" ")[0].toLowerCase();
  const rows = (await loadCommands())
    .filter(c => c.name.toLowerCase().startsWith(typed));
  if (!rows.length) return closeMenu();
  menu.innerHTML = "";
  for (const row of rows.slice(0, 40)) {
    const b = document.createElement("button");
    b.type = "button";
    const name = document.createElement("b"); name.textContent = row.name;
    const help = document.createElement("span"); help.textContent = row.help;
    b.append(name, help);
    b.onclick = () => {
      // Up to the first placeholder: `/load <id|title>` inserts `/load `, so
      // the part only the person knows is the part they are left typing.
      const upto = row.name.split(" ").filter(w => !w.startsWith("<") && !w.startsWith("["));
      box.value = upto.join(" ") + (upto.join(" ") === row.name ? "" : " ");
      closeMenu(); box.focus();
    };
    menu.appendChild(b);
  }
  menu.style.display = "block";
}

document.getElementById("text").addEventListener("input", reflect);

document.getElementById("say").onsubmit = async (e) => {
  e.preventDefault();
  const box = document.getElementById("text"), text = box.value.trim();
  if (!text) return;
  box.value = "";
  closeMenu();
  shell.style.display = "none";
  document.body.classList.remove("shell");
  // No echo here. The harness prints the line itself the moment it reaches the
  // prompt - `❯ /connect  (from the remote)` - and that comes back in the
  // transcript like everything else, so echoing it locally showed it twice.
  await call("/say", { text }).catch(() => {});
};

const cost = document.getElementById("cost"), costtext = document.getElementById("costtext");
const spin = document.getElementById("spin");

// The frames the terminal turns while it waits, turned here too. The mirrored
// line says `thinking…` in the transcript; this says the same thing where the
// eye already is, which on a phone is the box rather than the last line.
const FRAMES = ["\\u00b7", "\\u2722", "*", "\\u2736", "\\u273b", "\\u273d"];
let spinAt = 0, spinTimer = null;

function turn(busy) {
  if (busy && !spinTimer) {
    spinTimer = setInterval(() => {
      spinAt = (spinAt + 1) % FRAMES.length;
      spin.innerHTML = "";
      spin.append(FRAMES[spinAt]);
      const word = document.createElement("b");
      word.textContent = "working";
      spin.append(word);
    }, 260);
  } else if (!busy && spinTimer) {
    clearInterval(spinTimer); spinTimer = null; spin.textContent = "";
  }
}

function short(n) {
  return n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + "k" : String(n);
}

function drawCost(usage, busy) {
  turn(busy);
  const known = usage && usage.budget;
  cost.hidden = !known && !busy;
  if (!known) { costtext.textContent = ""; return; }
  const share = Math.min(1, usage.used / usage.budget);
  cost.className = share > 0.9 ? "full" : (share > 0.7 ? "warm" : "");
  costtext.textContent = `${short(usage.used)} / ${short(usage.budget)} context`
    + `  ${Math.round(share * 100)}%`
    + (usage.turns ? `  \\u00b7 ${usage.turns} turn${usage.turns === 1 ? "" : "s"}` : "");
  cost.querySelector(".bar i").style.width = (share * 100).toFixed(1) + "%";
}

async function poll() {
  for (;;) {
    if (pairing) return;                       // nothing to show until paired
    try {
      const r = await call("/state?since=" + seq + "&wait=1");
      if (r.status === 401) { where.textContent = "this link is no longer valid"; return; }
      const s = await r.json();
      dot.className = s.busy ? "busy" : "live";
      drawCost(s.usage, s.busy);
      where.textContent = (s.title || s.cwd) + "  ·  " + s.model;
      for (const [n, text] of s.lines) { add(text); seq = n; }
      if (tailNode) { tailNode.remove(); tailNode = null; }
      if (s.tail) {
        const stick = atBottom();
        tailNode = document.createElement("div");
        tailNode.className = "t"; paint(tailNode, s.tail, false);
        log.appendChild(tailNode);
        if (stick) log.scrollTop = log.scrollHeight;
      }
      drawAsk(s.question);
    } catch (err) {
      if (err && err.message === "pair") return;
      turn(false);
      dot.className = ""; where.textContent = "reconnecting…";
      await new Promise(r => setTimeout(r, 2000));
    }
  }
}
poll();
</script></body></html>
"""
