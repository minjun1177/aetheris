# Remote Control

[← README](../README.md)

## Remote Control

A harness is a terminal, and a terminal is somewhere you have to be. That is
fine while the answer takes four seconds. It stops being fine the moment the
work takes four minutes - a `/deepthink` pass, a test suite the model is
chasing, a sub-agent reading half a repository - because the two things you
then need are *what is it doing* and *yes, go ahead*, and both of them are
behind a keyboard you have walked away from.

So the session can hand out one door into itself:

```
❯ /remote on

  ✓ Remote control is ON. Whoever opens the link is at this prompt, with
    everything it can do.
  │ bound to 127.0.0.1:8765 - nobody has opened it yet
  ╰─ open this, and whoever holds it is at this prompt:
     http://127.0.0.1:8765/?k=Hn4Qk0Zt7rJ2vXbA9wLpMg
```

Nobody types forty-three random characters into a phone, so `/remote qr`
draws the link as something to point a camera at - black modules on a white
ground it paints itself, so it scans whatever theme the terminal is in:

```
❯ /remote qr

    █▀▀▀▀▀█ ▀ ▀▄█▀██▄ ▀▀▄██ ▄█▄▀█ █▀▀▀▀▀█
    █ ███ █ ▄ ▄█ ▀▄▀▄▀██▀▀▄█▀▄  ▀ █ ███ █
    █ ▀▀▀ █ █ ▀▀▀█ █▀ ██▄ █▀▀▀█▀  █ ▀▀▀ █
    ▀▀▀▀▀▀▀ █▄█ ▀ █▄▀▄█ ▀▄█ ▀ ▀ ▀ ▀▀▀▀▀▀▀
    █▄▄ ▀▄▀▀█ ▀█  ▀▀▄▄▀█▄▄▀▄▀▀ █▄██▀█▀ ▄▀
      … 
```

There is no library behind that: `qr.py` is a byte-mode encoder in the
stdlib, because a wheel to draw one screen was the wrong trade. It is held to
the standard by `tests/test_qr.py`, which reads each symbol back the way a
scanner does and checks that every block still satisfies its own error
correction.

Open the link on anything with a browser and you are at the prompt. The page shows
the transcript as it is printed here, a box that types into the same loop the
keyboard types into - a message or a slash command, both - and, when something
needs approving, the approval prompt itself with its buttons.

What the terminal *draws* stays out of it: the prompt, its completion menu,
the redraw after every keystroke are that terminal's furniture, not transcript,
and mirrored they reached the phone as a bare `❯` before every line typed
there. The prompt is pointed at the real stream and the mirror sits over what
the program prints instead.

The transcript arrives with the terminal's own colours still on it and the
page paints them - including colour that was opened on one line and closed
three lines later, the way the banner does, which is carried across lines
exactly as a terminal carries it - because half of what a terminal says is *how* it says it -
a refusal in red, a tool call in grey, the answer in white - and a wall of
identical text is harder to read on a phone than on the screen it came from.
Only `ESC [ … m` ever reaches the browser; every other escape is taken out
before it is sent, and the text itself only ever goes in as `textContent`.

Above the box is a strip: the same spinner the terminal turns while it waits on
the left, and what the conversation costs on the right - tokens against the
context window, the share of it used, how many turns. That is the part of
`/usage` that fits on a phone, and the spinner is where the eye already is,
which on a phone is the box rather than the last line of the transcript.

Typing `/` there lists the slash commands with what each one does, from the
same table `/help` renders, and tapping one inserts it: a phone has not read
`/help` and cannot be expected to remember forty names. Typing `!` turns the
box amber and says **Shell - runs on that machine as you; a command, not a
message**, which is the same warning the terminal puts over its own prompt and
for the same reason - the two look identical until one of them runs.

**Every question follows whoever is driving.** A turn started from the phone has its
questions asked on the phone; a turn started here is asked here. This is the
part that makes it a remote control rather than a viewer: a run that stops at
`Allow? [y/n]` on a screen nobody is looking at has hung, and there is no way
to find that out from the train. Both are printed on the terminal either way,
so the person at the desk can read what was asked and what came back. A
question nobody answers within `REMOTE_ASK_TIMEOUT` is refused, because the
safe end of an unanswered *may I delete this* is no.

That covers the slash commands that ask something too: `/model` typed on the
phone puts its list of models on the phone, as buttons. The one exception is an
API key - `/connect` will not take one over the link, because this is plain
HTTP and a key typed there crosses the network in the clear. That prompt is
answered at the keyboard or not at all.

**What it costs to leave it open.** Nothing runs until `/remote on`, and what
that opens is a shell - the link can type `!rm -rf ~` as easily as "hello". So:

- it binds **127.0.0.1** and nothing else, unless you type `/remote on lan`,
  which opens it to the network this machine is on and says so in as many
  words;
- the token is generated when the door opens, printed once, **never saved**,
  and gone when the door closes. There is no long-lived credential here and no
  setting that holds one. Loopback gets 128 bits of it; `lan` gets 256, because
  that is the token that crosses a network somebody else is also on;
- every request carries it, compared whole rather than character by character;
- **wrong tokens are counted, and then shut out.** After
  `REMOTE_MAX_BAD_TOKENS` of them from one address, that address is refused for
  `REMOTE_LOCKOUT` seconds. A 128-bit token is not guessable; a door somebody
  can knock on all afternoon without anyone hearing it is still the wrong door;
- **over a network, the link is not enough.** `/remote on lan` opens it, but a
  browser that arrives over the network is shown a box, not the transcript:
  six digits, printed in the terminal the harness is running in, expiring in
  two minutes and surviving three wrong guesses. A phone that types them gets a
  session of its own; anything else gets the box. That is what makes it a
  second factor rather than a second copy of the first - the link crosses the
  network and can be read off a shoulder, photographed or left in a history,
  and the terminal does not. `REMOTE_PAIR` decides when it is asked for:
  `lan` (the default), `always`, or `never`; `/remote forget` drops every
  browser that has paired;
- **you are told who is there.** The first request from an address, the first
  wrong token from one, and every attempt to pair appear at your prompt the way
  another agent's message does:

  ```
  ◆ 192.168.0.14 opened the remote link.
  ◆ 192.168.0.14 wants to drive this session. Code: 418 205 - type it there
    within 2 minutes. If this is not you, /remote off.
  ◆ 192.168.0.23 tried the remote with a token that is not this one.
  ```

  On a network you share, the question worth answering is not *could* somebody
  get in but *did* they, and nothing else here can answer it. `/remote` lists
  the same thing on demand;
- a request whose `Host` is not this machine is refused before the token is
  even looked at, which is what stops a page somewhere else on the internet
  from resolving its own name to `127.0.0.1` and talking to what answers;
- what is mirrored out goes through the same redaction the model gets, so a
  `.env` value that is on this terminal because *you* ran `!cat .env` does not
  go out over the wire;
- `/remote off` closes the port, drops the token, forgets who was there and
  ends every link that was already open.

**What it is not.** This is plain HTTP. On loopback that is the whole story -
the bytes never leave the machine. Over `lan` they cross a network, and whoever
is already on that network can read them: the transcript, and the token with
it. So `lan` is for a network you actually trust, and everything else is a
tunnel you already trust - `ssh -L 8765:127.0.0.1:8765 you@machine`. There is
deliberately no TLS and no account to sign in to here: a self-signed
certificate teaches you to click through the warning, and the thing on the
other side of this door is a shell.

**Moving it.** `REMOTE_PORT` and `REMOTE_HOST` are ordinary settings, so
`/set REMOTE_PORT 9000` works - and if a remote is open when you type it, it
moves there rather than waiting for a restart. That means a new token and a new
link, printed on the spot; the old link stops opening anything. If the port is
busy, the next nineteen are tried before it gives up.

**What it does not do.** It does not run a second session; there is one
conversation and the remote is another way into it. A line typed there arrives
at the prompt, so it waits for the current turn exactly as a typed line would,
and the transcript it can scroll back through is the last `REMOTE_LINES` lines
rather than the whole conversation - `/export` is still how a transcript
leaves this machine. Without `prompt_toolkit` installed, a remote line lands at
the next Enter here instead of interrupting the prompt.

`/exit` is the one command the link will not run: closing the session from a
phone leaves the phone with nothing to reconnect to, and the terminal with a
prompt nobody asked to leave. It says so and waits for the keyboard.

`REMOTE_ENABLED = True` in your settings opens the door at every start, with a
new token each time.
