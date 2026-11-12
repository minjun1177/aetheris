# Undoing and Checking the Work

[← README](../README.md)

## Undoing AI Edits

Every file an AI tool changes is committed on its own, under a message naming
the tool that did it:

```
ai(edit_file): greet.py
ai(write_file): parser.py
```

`/undo` takes the newest one back:

```
/undo             put the last AI edit back the way it was
/autocommit       whether this is on, and the recent AI commits
/autocommit off   stop committing (edits still happen, they are just not committed)
```

An assistant that edits files is only as useful as its undo. Without one the
honest advice is "commit before you let it touch anything", which nobody
follows, and a wrong edit three tool calls ago is gone.

Two rules keep this from being a nuisance:

**It commits only what the tool named.** Whatever else you have staged or
changed is left exactly as it was - `git commit` is given those paths
explicitly rather than being allowed to sweep up your index.

**Undo refuses rather than destroying work it did not create.** It will not
touch a commit you wrote, and if a file in the AI's commit has changed since,
it stops and says which one:

```
'ai(write_file): shared.py' touched files that have since changed: shared.py.
Commit or discard those first - undoing now would take them with it.
```

Outside a git repository, and on a machine with no git installed, nothing is
committed and nothing breaks. Set `GIT_AUTO_COMMIT = False` in `config.py` to
default it off.

---

## Checking the Work

A model that has just written a file tells you it is done. It has not run
anything. On a local 4B model this is not laziness - it genuinely does not
occur to it, and telling it to check its work in the system prompt is forgotten
by the third tool call. So the file is wrong, the turn ends, and you find out
by running the program yourself, copying the traceback, and pasting it back in.

That round trip is now the harness's job.

When a turn changes a file, the harness works out what kind of project the file
belongs to, runs that project's own check once the turn's tool calls are all
done, and if it fails, puts the failure in front of the model:

```
› fix the off-by-one in slice_window

  ▸ edit_file(filepath='context.py')
  ⎇ committed 4f1c2ae   /undo to take it back
  ⟳ auto-verify: python -m pytest -x -q -l   in chat
  ✗ python -m pytest -x -q -l failed   2.4s

  ▸ read_file(filepath='context.py', start=180, end=205)
  ▸ edit_file(filepath='context.py')
  ⎇ committed 9b70dd1   /undo to take it back
  ⟳ auto-verify: python -m pytest -x -q -l   in chat
  ✓ python -m pytest -x -q -l passed   2.6s

Fixed. `slice_window` was dropping the last message when the budget landed
exactly on a turn boundary; the bound is now inclusive.
```

Reacting to a concrete traceback is the kind of work a small model is good at -
much easier than noticing unprompted that something might be wrong. This is
the feature that moves a 4B model from "usually needs checking" to "tells you
when it is wrong".

**It only runs a check the project already declares.** No check is invented:

| The project has | It runs | Not a failure |
| :--- | :--- | :--- |
| `pyproject.toml`, `pytest.ini`, `tox.ini` or `setup.cfg` | `python -m pytest -x -q -l` | exit 5 - a project with no tests yet |
| no config file, but a `test_*.py` / `*_test.py` (or `tests/test_*.py`) beside the changed file - or above it, inside a git repository | `python -m pytest -x -q -l` | exit 5 |
| `package.json` with a real `test` script | `npm test --silent` | |
| `Cargo.toml` | `cargo test --quiet` | |
| `go.mod` | `go test ./...` | |

The check is picked by the *extension of the file that changed*, not by
whichever marker turns up first, so a `.py` file and a `.ts` file in the same
repository get the right one each. A file none of them cover - a `README.md`, a
config file - runs nothing at all. Neither does a project whose runner is not
installed, or one whose `package.json` still has the placeholder `test` script
`npm init` writes.

**It runs once per turn, not once per edit.** Four files written in one reply
are one run of the suite, against the state the model meant to leave them in
rather than three states it was halfway through.

**For Python it also hands over the variables.** The pytest check runs with
`--showlocals`, so a failure arrives as the state that produced it rather than
a line number to reason backwards from:

```
>       assert lookup(records, user_id) == prefix + "c"
prefix     = 'user-'
records    = {1: {'name': 'a'}, 2: {'name': 'b'}}
user_id    = 3
E       KeyError: 3
```

Working backwards from a line number to what was in scope is the thing a small
model is worst at; reading a value off the page is the thing it is best at.
pytest cuts a long repr down itself, so this costs a few hundred characters,
not the whole budget. The other three runners have no equivalent - a Go panic
and a JS stack trace do not carry locals - so this is Python only.

**It is bounded, and it gives up.** The check gets `VERIFY_TIMEOUT` seconds
with no stdin, and only the tail of the output - which is where a failure is
written down - is shown to the model. A suite that runs past the timeout turns
itself off for the rest of the session rather than costing that after every
edit; `/autoverify on` tries it again. And when the *same* failure comes back
three times, the harness stops feeding it back:

```
Auto-verify has come back with the same failure 3 times, so it is off for the
rest of this turn. Stop editing. Tell the user which check is failing, what
you changed, and what you think is wrong - a fourth guess is worth less to
them than an honest description. They can take your changes back with /undo.
```

The count is for a model that is *stuck*, not for a turn that has several
things wrong with it. A run against `gemma4:e4b` hit three different failures -
a module that was not callable, a bug that was already in the tree, then a
brace it had just deleted - and was stopped on the third, one edit after being
handed exactly what it needed. A failure that reads differently is progress,
so the budget starts again.

That last sentence is why this ships after `/undo` (§11) rather than before it.
Every retry is its own commit, so a loop that went the wrong way is undone one
step at a time.

```
/autoverify        whether it is on, which checks exist, and any turned off here
/autoverify off    stop running anything after an edit
```

Set `AUTO_VERIFY = False` in `config.py`, or `/set AUTO_VERIFY off`, to default
it off.

**Lock the test, and "make it pass" means what it says.** Ask a model to make
a failing test pass and it will often make the *test* pass - loosening the
assertion until it is true, or deleting it. Telling it not to does not work,
for the same reason it does not work in a planning stage: a small model that is
stuck takes the opening it is given. `/tdd` closes the opening:

```
/tdd make test_window_slicing pass
```

For that one request the project's test files are refused to `edit_file`,
`write_file`, `delete_file` and `copy_file` at the dispatcher - through the
same permission rules as everything else (§13), held in memory and never
written to your `.permissions.json`. The model is told so up front, so it
spends its calls on the code rather than discovering the wall. Auto-verify gets
six tries instead of three, because staying in that loop is the whole point.

It lifts itself when the turn ends, including when the turn failed or you
interrupted it - a lock nobody remembers turning on is worse than no lock.
`/tdd` on its own arms it for your next message; `/tdd off` lifts it early.

The model is given one way out: if it concludes the test itself is wrong, it is
asked to say so and stop rather than work around it. Sometimes the test is
wrong, and six attempts at satisfying a bad test is not a better answer than
saying so.

What this cannot cover is `run_cmd` - what a shell command writes is not
knowable from the call, which is the same limit the agent channel's file claims
have.
