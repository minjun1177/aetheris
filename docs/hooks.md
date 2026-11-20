# Hooks

[← README](../README.md)

**Hooks** are your own commands, run at three points: before a tool call,
after one, and when a turn ends. Permission rules can only say *yes*, *no* or
*ask*. A hook can look at the call with a program you choose: it can refuse a
`git push` that fails a check, format a file as soon as it is written, or show a
desktop notification when a long turn finishes.

---

## Where they live

| File | Used |
| :--- | :--- |
| `~/.aetheris/hooks.json` | Always. These are your own commands |
| `.hooks.json` in the working directory | **Only once trusted.** A command in it runs on your machine as you, so it is shown and asked about the way a project's `.mcp.json` is. The answer is kept against the file's sha256, so a changed file is asked about again |

Hooks from both files run, user hooks first. `/hooks` lists them, and
`/hooks reload` reads both files again. `/set HOOKS_ENABLED off` stops all of
them.

---

## The file

```json
{
  "pre_tool": [
    {"match": "run_cmd(git push*)", "command": "./scripts/check-push"}
  ],
  "post_tool": [
    {"match": "edit_file", "command": "ruff format \"$AETHERIS_FILES\""},
    {"match": "write_file(*.py)", "command": "ruff check \"$AETHERIS_FILES\""}
  ],
  "turn_end": [
    {"command": "notify-send aetheris \"turn finished\""}
  ]
}
```

| Event | When | A non-zero exit |
| :--- | :--- | :--- |
| `pre_tool` | After the permission rules, before the approval prompt and the tool | **Refuses the call.** The model gets a `[System]` result with what the hook printed |
| `post_tool` | After the tool ran | Cannot take the call back, so what the hook printed is **added to the result** as `[Hook] ...` |
| `turn_end` | When a turn ends, finished, stopped or failed | Printed on screen, never sent to the model |

`match` is a permission rule, either `tool` or `tool(pattern)`, matched the same way `/perms`
matches. If it is left out, the hook applies to every tool. `turn_end` ignores it.

---

## What a hook is given

On **stdin**, one JSON object:

```json
{"event": "pre_tool", "tool": "write_file",
 "arguments": {"filepath": "src/app.py", "content": "..."},
 "files": ["src/app.py"], "cwd": "/home/me/project"}
```

`post_tool` also carries `result`, and `turn_end` carries `outcome`
(`done`, `stopped` or `error`).

| Variable | Holds |
| :--- | :--- |
| `$AETHERIS_EVENT` | `pre_tool`, `post_tool` or `turn_end` |
| `$AETHERIS_TOOL` | The tool's name; empty for `turn_end` |
| `$AETHERIS_FILES` | The files the call names, one per line |

**Important:** the hook's environment has no provider API keys in it, the same
as `run_cmd`'s. Arguments are passed as the model wrote them, so a
`{{env:NAME}}` placeholder arrives as the placeholder, not as the secret.

A hook that runs longer than `HOOK_TIMEOUT` seconds (30 by default) counts as
failed. At most `HOOK_OUTPUT_CHARS` characters of its output are kept.
