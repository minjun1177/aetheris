# Aetheris

[![CI](https://github.com/minjun1177/aetheris/actions/workflows/ci.yml/badge.svg)](https://github.com/minjun1177/aetheris/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![PyPI](https://img.shields.io/pypi/v/aetheris.svg)](https://pypi.org/project/aetheris/)

A terminal AI assistant that works on your machine: it reads and writes files, runs
commands and answers their prompts, searches the web, and can **undo** what it did.
It is built for **small local models first** (Ollama), and talks to Anthropic, OpenAI,
Gemini and Attacca with the same tools and the same approval prompts.

```text
 you ──► prompt box / aetheris -p ──► model (Ollama · Anthropic · OpenAI · Gemini)
                                          │ tool call (native, or <tool_call> text + JSON repair)
                                          ▼
              permissions ─► approval ─► tool ─► git commit per edit ─► auto-verify
                                          │                                 │
                                          └──────── result back to the model ◄┘
```

---

## Quickstart

| Requirement | Version | Notes |
| :--- | :--- | :--- |
| **Python** | 3.10+ | |
| **Ollama** | any recent | Only for local models. `/connect` reaches hosted providers without it |
| **A model** | `gemma4:e4b` recommended | Any model Ollama reports `tools` for, 8B+. See [model notes](docs/setup.md) |
| **git** | optional | Enables a commit per AI edit and `/undo` |

```bash
# 1. Install
pip install aetheris
# 2. Pull a local model
ollama pull gemma4:e4b
# 3. Start it in the directory you want it to work in
aetheris
```

Inside, these six cover most sessions - `/help` shows them, `/help all` shows the rest:

| Command | What it does |
| :--- | :--- |
| `/connect` | Pick a provider and model (Ollama, Anthropic, OpenAI, Gemini, Attacca) |
| `/sessions` | Open a saved conversation |
| `/set NAME value` | Change a setting, e.g. `/set DEEPTHINK on`, `/set AUTO_ALLOW off` |
| `/undo` | Take back the last file change the AI committed |
| `/clear` | Start a fresh conversation |
| `/exit` | Leave |

**Important:** it runs commands and edits files as you, asking first unless you allow it.

---

## Installation / Usage

### Option A - from PyPI

```bash
pip install aetheris                 # the `aetheris` command
pip install "aetheris[ast]"          # + Tree-sitter, for get_code_skeleton / query_ast_node
pip install "aetheris[attacca]"      # + the packages /connect attacca needs
pip install "aetheris[keyring]"      # + keep API keys in the OS keyring
```

### Option B - from a checkout

```bash
# 1. Clone the project
git clone https://github.com/minjun1177/aetheris && cd aetheris
# 2. Install it editable, so a change takes effect without reinstalling
pip install -e .
pip install -r requirements.txt   # ...or only the dependencies; then run: python -m aetheris
```

### Carrying on, and running without the prompt box

```bash
aetheris -c                                    # the newest session worked on in this directory
aetheris --resume <id or title>                # a particular one
aetheris -p "summarise CHANGELOG.md"           # one answer on stdout, then exit
git diff | aetheris -p "review this" --json    # piped input + JSON: text, tool calls, tokens
aetheris -p "fix the test" --auto-allow        # tools run without asking: scripts and CI
```

### Using the parts

It is also a typed library: `aetheris.MCPServer` (MCP without an SDK) and `aetheris.start_shell`
(a command you can answer while it runs). Only `aetheris.__all__` is promised - [docs/library.md](docs/library.md).

### Documentation

| Topic | |
| :--- | :--- |
| [A real turn](docs/demo.md) · [Features](docs/features.md) | `gemma4:e4b` fixing a failing test, unedited; everything it does, in one list |
| [Setup and models](docs/setup.md) | Which models work, tests, the benchmark |
| [Tools](docs/tools.md) | The model's tools, the call format, hashline editing |
| [Providers](docs/providers.md) | Ollama, Anthropic, OpenAI, Gemini, Attacca, keys, reasoning models |
| [Running commands](docs/running-commands.md) | `run_cmd`, live prompts, the Python VM |
| [Undo and verify](docs/undo-and-verify.md) | A commit per edit, `/undo`, auto-verify, `/tdd` |
| [Permissions and secrets](docs/permissions.md) | Allow/deny rules, approvals, `.env` redaction |
| [Deepthink](docs/deepthink.md) | Plan, check, build, review, verify |
| [Skills and MCP](docs/skills-and-mcp.md) | `SKILL.md` packs and MCP servers |
| [Web search](docs/web-search.md) | How results are found and ranked |
| [Agent channel](docs/agents.md) · [Remote control](docs/remote.md) | Several agents in one project; driving a session from a phone |
| [Configuration](docs/configuration.md) · [Slash commands](docs/slash-commands.md) | Every setting and every command |
| [Design](docs/design.md) | Architecture overview, tests, how it differs from other harnesses |

---

## Configuration / Environment Variables

`/set` changes a setting and saves it to `~/.aetheris/settings.json`; all of them: [docs/configuration.md](docs/configuration.md).

| Variable | Default | Description |
| :--- | :--- | :--- |
| `AETHERIS_HOME` | `~/.aetheris` | Where sessions, settings, memory and keys live |
| `AETHERIS_ACCEPT_TERMS` | unset | `1` accepts the first-run terms where no terminal can ask |
| `OLLAMA_HOST` | `http://localhost:11434` | The Ollama daemon (also `/set OLLAMA_HOST`) |
| `ANTHROPIC_API_KEY` · `OPENAI_API_KEY` · `GEMINI_API_KEY` | unset | Hosted keys; `/connect` can save one instead |
| `AETHERIS_KEYRING` | on | `off` keeps saved keys in `providers.json` (0600) |

---

## Project Structure

```text
aetheris/
├── aetheris/            # the package: app.py, llm_client.py, tools.py, toolspec.py, ...
├── bench/               # run.py - measure a model on the documented tasks
├── docs/                # the user documentation
├── skills/              # bundled SKILL.md packs
├── tests/               # one script per area; each prints [ok]/[FAIL]
├── ARCHITECTURE.md      # for changing the code: how it fits, what not to break
├── CHANGELOG.md         # what changed, and why
└── pyproject.toml
```

---

## Compatibility / License

| | |
| :--- | :--- |
| **Python** | 3.10 - 3.14 |
| **OS** | **Linux**, **macOS**, **Windows** (waiting-for-input detection is best on Linux) |
| **Stable surface** | Slash commands, `/set` names, tool names, the `~/.aetheris` layout and `aetheris.__all__` - see `CHANGELOG.md` |
| **License** | Apache-2.0 - see [LICENSE](LICENSE). Provided **"AS IS"**; the first run says what it can do to your machine and asks once |
