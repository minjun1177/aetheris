# Setup

[← README](../README.md)

## Setup

### Prerequisites
- **Python**: Version 3.10 or higher
- **Ollama**: Installed and running locally (default endpoint: `http://localhost:11434`).
  Only needed for the local case - `/connect` reaches Anthropic, OpenAI and
  Gemini without it.

### What the model has to be able to do

The harness is built so a small model is *usable*, not so any model is. The
thing that decides it is not size but whether the model can be made to emit a
tool call at all - and having done the work to make 4B models usable, here is
where that actually lands.

| | Runs | Notes |
| :--- | :--- | :--- |
| **Recommended** | `gemma4:e4b`, or any model Ollama reports `tools` for at 8B+ | Native tool calling, ~5-9GB. Reliable in testing |
| **Workable** | A 12B-class model without `tools`, e.g. `gemma3:12b` | Text protocol. **11/15** tool calls landed in testing |
| **The floor** | A 4B-class model, e.g. `gemma3:4b` | Roughly a coin flip. Fine for one-shot edits, not for `/deepthink` |
| **Below that** | 1-3B | Not recommended. Expect it to describe a tool call rather than make one |

Across five tool-calling tasks - one call, using the result, exact arguments, two
calls in order, and correctly calling nothing - `gemma4:e4b` passed all five.

Two things matter more than the parameter count:

**Whether Ollama reports `tools` for it.** That is per model, not per family -
`gemma4:e4b` has it and `gemma3:12b` does not. A model that has it goes through
the real function-calling interface, gets a 12KB smaller prompt, and is
markedly more reliable. `/connect status` shows which protocol is in use.

**Context window.** `NUM_CTX` defaults to 65536. A model that cannot hold that
will have its conversation compressed early and often; 32k is workable, below
16k is not really. Models differ, so `/context <tokens>` gives the one in use a
window of its own - e.g. `/context 16k` for a small model on a small GPU - and
a hosted model that reports its window gets that one without being told.

Measured on this project's own test tasks - creating a file, and fixing a
function halfway down a 5,700-character file - on the machine it was written
on. Sample sizes are small (6-15 runs); treat them as the difference between
"works" and "does not", not as a benchmark.

Nothing here applies to `/connect anthropic|openai|gemini`. Those all support
native tool calling, and the floor is whatever that provider's smallest model
is.

### Installation Steps

1. **Install it**:
   ```bash
   pip install aetheris
   ```
   That puts the `aetheris` command on your PATH; run it in any
   directory you want to work in. `python -m aetheris` does the same
   thing if you would rather not rely on the PATH.

   To work on the harness itself, install the checkout instead, so an edit
   takes effect without reinstalling:
   ```bash
   git clone https://github.com/minjun1177/aetheris
   cd aetheris
   pip install -e .
   ```
   Or, to run it straight from the checkout without installing:
   ```bash
   pip install -r requirements.txt
   ```
   `get_code_skeleton` and `query_ast_node` need Tree-sitter, which is ten
   grammar wheels for two tools and so is opt-in: `pip install "aetheris[ast]"`
   (or `pip install -e ".[ast]"` from a checkout). Everything else runs
   without it.

   Attacca speaks a websocket protocol that needs two more packages, so it is
   opt-in the same way: `pip install "aetheris[attacca]"`. `/connect attacca`
   says so, before any pairing code is shown, if they are missing.

   `pip install "aetheris[keyring]"` keeps API keys in the OS keyring instead
   of a file - see *Where keys are kept*.

2. **Pull an Ollama Model**:
   ```bash
   ollama pull gemma4:e4b
   ```

3. **Launch it**:
   ```bash
   aetheris            # if you installed it
   python -m aetheris   # if you did not
   ```

   To carry on where you left off instead of starting fresh:
   ```bash
   aetheris -c                     # the newest session worked on in this directory
   aetheris --resume <id or title>  # a particular one, by either name
   ```
   `-resume` and `-continue` are accepted too. Both stop with an error rather
   than opening a blank session when there is nothing to resume, and `--resume`
   lists the candidates instead of choosing when a name matches more than one.
   `/sessions` inside a session lists them and opens the one you pick.

4. **Or answer one prompt and exit** - for a pipe, a script or CI:
   ```bash
   aetheris -p "summarise CHANGELOG.md"           # the answer, on stdout
   git diff | aetheris -p "review this"            # piped input is appended to the prompt
   aetheris -p "fix the failing test" --auto-allow --json
   ```
   The work is drawn on stderr; stdout carries the answer alone, or with
   `--json` one object: `text`, `tool_calls` (name, arguments, `ok` / `error` /
   `refused`, seconds), `tokens`, `seconds`, `model`, `session` and `error`.
   Without `--auto-allow` a tool that needs approval is refused unless there is
   a terminal to ask at. The session is saved, so `aetheris -c` carries it on;
   `-c` and `--resume` combine with `-p` too. Exit status 1 is a failure, 2 a
   usage error.

### Running the tests

No framework - each file is a script that prints `[ok]` / `[FAIL]` and exits
non-zero on failure. They need no Ollama daemon and no network:

```bash
for t in tests/*.py; do python "$t" || echo "FAILED: $t"; done
```

Without a clone, the source release of the version you installed carries the
same suite and every file it reads. It runs that version's source, not the
installed files, against the dependencies and the platform you actually have:

```bash
# 1. Fetch the source release of the installed version, and unpack it
pip download "aetheris==$(aetheris --version | cut -d' ' -f2)" --no-binary :all: --no-deps -d src
tar xzf src/aetheris-*.tar.gz -C src && cd src/aetheris-*/
# 2. Run it
for t in tests/*.py; do python "$t" || echo "FAILED: $t"; done
```

### Measuring a model

`bench/run.py` runs the tasks the table above was measured on - five tool
calls and two edits - through `aetheris -p --json --auto-allow`, N times each,
grades every run from the files on disk and the tool calls made, and saves the
result as JSON under `bench/results/`:

```bash
python bench/run.py --model gemma4:e4b -n 5 --seed 1     # 7 tasks x 5 runs
python bench/run.py --list                                # what the tasks are
python bench/run.py -n 5 --seed 1 --compare bench/results/<earlier>.json
```

Each run gets an empty working directory and a throwaway harness home, so runs
cannot see each other. With `--seed S`, run i is sent seed S + i: repeatable,
and still N different samples.

`tests/test_platform.py` is the one worth running on any new machine, and
especially on Windows: it checks what that machine can tell you about a command
waiting for input. `tests/test_vm.py` starts and kills real Python processes,
so it is the slowest of them - about ten seconds, most of it waiting out a
deliberate `VM_TIMEOUT`.

`tests/test_zyris.py` and `tests/test_attacca.py` need the `attacca` extra
and say `skipped` without it. They still need no network: Attacca is played
by a server on a loopback port.
