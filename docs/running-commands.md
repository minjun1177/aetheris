# Running Commands

[← README](../README.md)

## Running Commands

`run_cmd` captures the command's output, so nothing the command prints reaches
the user's screen while it runs - including a prompt. An interactive program
therefore used to hang the whole app: it sat waiting on stdin with its question
captured and invisible, and there was no way to tell what it wanted or to answer
it.

The fix is not to deny it stdin. **The model answers the prompt.** The command
keeps a live pipe, and when it goes quiet its output so far is handed over with
a session id - see below. A command that never finishes is stopped at
`config.CMD_TIMEOUT` (120s) along with everything it started, and whatever it
printed first is kept.

### Answering a program while it runs

`run_cmd` does not wait for the command to finish. It stays connected, draining
output as it appears, and when the command is waiting the output so far comes
back with a session id:

```
추측:

[Waiting] 'python3 game.py' is still running and has printed nothing for 0.6s,
so it is most likely waiting for input. Answer it with send_input:
    {"name": "send_input", "arguments": {"session": "s1"}}
```

The model reads the prompt, answers it with `send_input`, and gets whatever the
program prints next - one exchange per turn, until the program exits. That is
how it tests something it has just written: it plays through the program
itself. `end_process` stops one that will not exit. The first few answers can
also be sent up front in a `<stdin>` block on `run_cmd`.

**Telling "waiting" from "busy".** Going quiet proves nothing on its own - a
program that is merely computing looks identical from outside. The best signal
each platform offers is used, strongest first:

| Signal | Where | What it proves |
| :--- | :--- | :--- |
| The output ends without a newline (`추측: `) | everywhere | It is shaped like a prompt |
| `/proc/<pid>/syscall` says a thread is parked in `read()` on fd 0 | Linux | It really is waiting on stdin - and a "no" is trusted too |
| The process tree has burned no CPU | Windows, macOS | It is idle, but sleeping and waiting look the same |
| Silence lasting `CMD_WAIT_TIMEOUT` | everywhere | Nothing better was available |

On Linux the `/proc` answer is exact in both directions, so a `sleep 3` is
simply waited out. Windows and macOS have no equivalent, so idle CPU only
shortens the wait to `CMD_IDLE_GRACE` instead of ending it - a long silent
pause may be offered to the model as a prompt, and an empty `send_input` picks
the output back up when it turns out not to be one.

To see what a given machine can actually manage:

```bash
python tests/test_platform.py
```

It prints which signals are available there, then runs a prompt, a silent
sleep, a busy loop and a runaway command through `run_cmd`. On Linux it does
the whole thing twice, the second time with `/proc` switched off - which is
exactly the code Windows and macOS run, so the fallback can be checked without
leaving Linux. It exits non-zero if anything fails.

A command that never stops printing is killed at `CMD_TIMEOUT` along with
everything it started, and at most `CMD_MAX_SESSIONS` live commands are kept.

**Text that is not ASCII.** A command's output is decoded as UTF-8 first,
because UTF-8 is the only candidate that can report that it is wrong - almost
any byte is legal cp949, so guessing a code page first would silently turn good
text into mojibake. If the bytes turn out not to be UTF-8, which is what a
Python older than 3.15 printing to a pipe on Korean Windows produces, the
console code page takes over for the rest of that command - both for what it
prints and for what `send_input` sends back to it.

### The Python VM

`run_python` is a Python process kept alive beside the harness, for working
something out before committing to it. It is aimed squarely at the small local
model this harness is built around: `gemma4:e4b` cannot hold an intermediate
result in its head, cannot reliably do arithmetic in prose, and will state what
a regex matches rather than find out.

```
<tool_call>
{"name": "run_python", "arguments": {}}
<content>
import statistics
scores = [88, 92, 79, 95, 61]
print("mean", statistics.mean(scores))
sorted(scores)[-2]
</content>
</tool_call>
```

```
[Success] ran in 0.04s.

mean 83

=> 92

(kept for your next run_python call: scores, statistics)
```

Four things it does that `run_cmd python3 -c "..."` does not:

- **The code does not have to survive a JSON string.** It arrives in a
  `<content>` block, byte for byte, exactly as a file body does. Escaping a
  snippet into a shell argument inside a JSON value is two levels of quoting,
  and it is the single thing a 4B model gets wrong most often.
- **It remembers.** One process holds its globals across calls, so the model
  can compute, look at the answer, and compute again. The result names what it
  just defined, so the model knows what it still has. `reset` empties it.
- **The last expression is answered.** `2 ** 10` on its own prints nothing
  under `python -c`; here it comes back as `=> 1024`. A model reaches for a
  calculator far more readily when the calculator answers.
- **It is a scratchpad, not the project.** The process runs in
  `~/.aetheris/vm/session-<pid>`, a directory of its own for this run of the
  harness, so a stray `open(..., "w")` lands there rather than in the
  repository - and never in an auto-commit. The files stay for the rest of the
  session (`/vm stop` and `/vm reset` keep them) and are deleted when it ends:
  on exit, or on `/clear`. A directory left by a harness that crashed is swept
  the next time a VM starts. Every result names the files that call wrote and
  says they are not in the project, and a `FileNotFoundError` says where the
  code looked - a model that cannot see the directory otherwise assumes it is
  the project's. The project is still on its
  `PYTHONPATH`, so a function that has just been written can be imported and
  tried; nothing is written back to it, not even a `__pycache__`.

Answers for anything the code reads with `input()` go in a `<stdin>` block, one
per line, so a prompt can be tried without the `run_cmd` / `send_input` dance.

**It is isolation from mistakes, not from a hostile program.** The code runs as
you, with your files and your network, and there is no pretence otherwise -
which is why `run_python` still goes through the approval prompt, and why it
counts as changing the world, so a read-only deepthink stage refuses it exactly
as it refuses `run_cmd`. What the separate process does buy is that a runaway
loop, a 40GB allocation or a hard crash takes down the scratch process and not
the harness.

**The wall-clock kill is the guarantee; the memory cap is an optimisation on
top of it.** On Linux the process is additionally held to `VM_MEMORY_MB` of
address space, so an over-large allocation comes back as a `MemoryError` the
model can read and the VM survives. macOS accepts that `setrlimit` call and
does not enforce it, and Windows has no `resource` module at all - on both, the
same allocation runs until `VM_TIMEOUT` stops it and the VM is restarted. The
runaway is contained everywhere; only Linux turns it into a tidy error.
`VM_FILE_MB` is enforced wherever `RLIMIT_FSIZE` is.

Any of the three ways it can die - the `VM_TIMEOUT` kill, a crash, an
`os._exit()` - loses the namespace, and the result says so in as many words.
Silence there would leave the model referring to variables that no longer
exist, and the next error would be a `NameError` explaining nothing.
