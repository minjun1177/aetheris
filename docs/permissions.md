# Tool Permissions and Secrets

[← README](../README.md)

## Tool Permissions

Until now the only gate was the approval prompt, and `/automode on` turned it
off for everything at once - including `run_cmd` and `delete_file`. Rules give
the middle ground.

**Typing while it works.** Nothing stops you: the terminal buffers the line and the next prompt picks it up, so an answer can be written while the model is still finishing. The one thing it will not do is *answer* something - a question that appears mid-turn empties the keyboard buffer first and says so, because approving a `delete_file` with a half-typed sentence is not a thing that should be possible.

Rules are read from `./.permissions.json` and `~/.aetheris/permissions.json`;
rules from both files apply. Copy `.permissions.json.example` to start.

```json
{
  "allow": ["run_cmd(git status)", "mcp__filesystem__*"],
  "deny":  ["delete_file", "run_cmd(rm *)", "write_file(*/.env)"]
}
```

A rule is a tool name, optionally followed by a pattern in parentheses matched
against the call's main argument - the command for `run_cmd`, the path for a
file tool, the URL for a network tool. Both halves accept `*` and `?`. A pattern
with no wildcard also covers `<pattern> <anything>`, so `run_cmd(git status)`
already allows `git status --short`.

- **deny** does not run and does not ask. The tool's handler is never reached,
  and the model is told it is blocked so it stops retrying.
- **allow** runs without a prompt.
- Everything else asks, exactly as before - an empty rule set changes nothing.

### What a rule does not cover

A rule is matched against the call's argument **as text**, and it is worth being
plain about what that does and does not buy you.

**An allow rule stops at the command it names.** `run_cmd` runs its command
through a shell, so `git status && rm -rf ~` starts with the text
`run_cmd(git status)` allows. It is not allowed: an operator the rule itself
does not contain - `;` `&&` `||` `|` `` ` `` `$(` `${` `>` `<` - means the
command does more than the rule accounts for, and it falls through to the
approval prompt instead. A rule that asks for a pipeline outright, such as
`run_cmd(git log * | grep *)`, still gets one.

**A deny rule is a stop sign, not a sandbox.** It matches text, and text can be
rewritten: `run_cmd(rm *)` denies `rm -rf x` and does not recognise
`sh -c 'rm -rf x'` or `/bin/rm -rf x`. Those fall through to the approval
prompt, so the prompt is still between the model and the command - but with
`/automode on` there is no prompt, and then a deny rule is only as good as the
spelling the model happened to use. Deny is for the mistakes you expect, not for
an adversary.

**A pattern is never empty.** `write_file()` reads as "calls with no arguments"
and would have meant the opposite, so it is refused at load time and named in
`/perms`. Write the bare tool name, `write_file`, when you mean every call.

**`run_python` takes no pattern at all.** A rule matches one argument as text,
and the argument here is a program - `run_python(import *)` would mean nothing
useful and would read as though it meant something. So the only rule is the
bare `run_python`, which allows every snippet, and the prompt shows the code
before it runs. Allowing it is worth roughly what allowing `run_cmd` outright
is worth; the difference is that the prompt is showing you exactly what will
run.

At an approval prompt the choices are now `[y/n/a]`, where `a` allows this
exact call from now on and appends the rule to `.permissions.json`. `/perms`
lists the active rules, `/perms allow <rule>` and `/perms deny <rule>` add one
by hand, and `/perms reload` re-reads the files.

### Secrets in `.env`

A `.env` is the one file in a project whose *contents* are the secret, and a
value the model reads does not stay read: it goes to the provider, so a hosted
model means the key is now their problem too, and it is written into
`~/.aetheris/sessions/*.json` and stays there. One `read_file` puts a key in
two places nobody would think to check.

So the harness reads the file and the model does not. What it is handed is

```
STRIPE_KEY={{env:STRIPE_KEY}}
DATABASE_URL="{{env:DATABASE_URL}}"
APP_ENV=development
```

and when it writes that placeholder back - in a `run_cmd`, a `get_url`, a
`call_api` header, a `run_python` snippet - the harness puts the real value in
on the way to the tool. `curl -H "Authorization: Bearer {{env:STRIPE_KEY}}"`
works, and the model has still never seen the key. The approval prompt shows
the placeholder too, with a line saying which secret is filled in, so approving
is not a way to find out either.

**What counts as a secret**, exactly - because the rule is blunt on purpose
and it shows:

1. the file is `.env`, `.env.local`, `.env.development`, `.env.production`,
   `.env.test` or `.envrc`, in the working directory or at the top of the git
   tree. Anything with `example`, `sample`, `template`, `dist` or `default` in
   the name is skipped: those exist to be read;
2. the line parses as `KEY=value`, `export KEY=value` or `KEY: value`, quotes
   stripped;
3. the **value** is at least `SECRET_MIN_LENGTH` (8) characters, is not a
   number, and is not one of a short list of words that are never secrets
   (`true`, `localhost`, `production`, …).

Then that value is replaced **wherever it appears**, in any text going to the
model or out over `/remote`. Which is the whole point and also the surprise:
`PROJECT_DIR=aetheris` in your `.env` makes `aetheris` a secret, so
`!dir` comes back with `{{env:PROJECT_DIR}}` where the folder name was. Nothing
is wrong - a value that is also an ordinary word matches like an ordinary word,
and the alternative is a rule that sometimes lets a key through. When it
happens to a `!` command the harness now says so:

```
◆ PROJECT_DIR from .env is hidden in the copy the model gets. /set SECRET_REDACT off stops that.
```

The way out is to take the value out of `.env` rather than to loosen the rule:
it is not a secret, so it does not belong in the file the harness treats as
secret. `APP_ENV=development` and a port number are already left alone by (3).

**A file is never how it gets out, or how it is lost.** A placeholder is
expanded into what *runs* and never into what is *saved*, so writing
`{{env:KEY}}` to a file and reading it back gives the model the placeholder
again - and writing that placeholder over the file that holds the real value is
refused outright, because it would replace the key with its own name and
neither the model nor the person would see it happen. Writing
`.env.example` full of placeholders is fine, and is the case that rule exists
to keep working.

**What this does not cover.** A secret that is not in one of these files -
typed into the chat, printed by a command that generates it, pasted by you - is
not known and is not redacted. This closes the biggest and most routine hole;
it is not a sandbox and should not be described to anyone as one. `/set
SECRET_REDACT off` turns it off.
