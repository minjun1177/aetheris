"""`/connect` - point the harness at a model provider.

Interactive by default, because the useful part is picking a model from the
list the provider actually offers rather than typing an id from memory. It also
takes arguments for the times you already know what you want:

    /connect                      pick a provider, then a model
    /connect anthropic            pick a model from Anthropic
    /connect openai gpt-4o        connect straight to a model
    /connect status               what is connected, and what could be
    /connect forget anthropic     delete the API key saved for a provider
"""

import os
import textwrap

from aetheris import config
from aetheris import providers
from aetheris.config import S, _hr, tw


def _print_problem(what: str, error) -> None:
    """A failure, in full, wrapped to the terminal rather than cut off.

    Providers answer a refused request with a sentence that names the reason
    and often the page that fixes it. Cutting that to fit one line removed the
    half worth reading, which is the half at the end.
    """
    print(f"  {S.ERR}✗ {what}:{S.R}")
    width = max(30, tw() - 6)
    for paragraph in str(error).splitlines() or [""]:
        for line in textwrap.wrap(paragraph.strip(), width) or [""]:
            print(f"  {S.MUTED}  {line}{S.R}")
    print()


def _ask(prompt: str, title: str = "", options=(), keyboard_only: bool = False) -> str:
    """One answer, from whoever is driving - unless it is a secret.

    `/model` typed on a phone used to put its numbered list on the phone and
    its "Select (0~20)" on a terminal nobody was sitting at, which is the hang
    the remote exists to prevent. So this goes through the same place every
    other blocking question does.

    `keyboard_only` is the exception, and it is exactly one prompt: an API key.
    The remote is plain HTTP, so a key typed there crosses the network in the
    clear, and a provider key is not the person's to leak twice. That one is
    asked at the keyboard or not at all.
    """
    from aetheris import remote
    from aetheris.tui import ask_the_driver
    if keyboard_only and remote.driven():
        print(f"  {S.WARN}⚠ An API key is only ever typed at the keyboard.{S.MUTED} "
              f"This link is plain HTTP; a key sent over it crosses the network "
              f"in the clear.{S.R}")
        print(f"  {S.MUTED}Set {S.GRAY}/connect{S.MUTED} again from the terminal, "
              f"or put the key in the environment.{S.R}\n")
        return ""
    try:
        answer = ask_the_driver(title or "Aetheris", (), options, prompt,
                                free_text=True)
    except KeyboardInterrupt:
        print()
        return ""
    return config.safe_text((answer or "").strip())


def _choose(title: str, rows: list, formatter) -> int:
    """Show a numbered list and return the chosen index, or -1."""
    print(f"\n  {S.BOLD}{S.ACCENT}{title}{S.R}")
    for i, row in enumerate(rows, 1):
        print(f"  {S.ACCENT}{i:3}.{S.R} {formatter(row)}")
    print(f"  {S.MUTED}  0.{S.R} {S.GRAY}Cancel{S.R}\n")
    # The same list the terminal just printed, as something to tap on: a phone
    # cannot see the numbered rows above and has no business guessing them.
    raw = _ask(f"  {S.INFO}Select{S.R} {S.MUTED}(0~{len(rows)}){S.R} {S.INFO}›{S.R} ",
               title=title,
               options=[(str(i), formatter(row)) for i, row in enumerate(rows, 1)]
                       + [("0", "Cancel")])
    if not raw or raw == "0":
        return -1
    try:
        index = int(raw) - 1
    except ValueError:
        return -1
    return index if 0 <= index < len(rows) else -1


def show_status() -> None:
    print(f"\n  {S.BOLD}{S.ACCENT}Providers{S.R}")
    print(f"  {_hr(width=60)}")
    active = providers.current().name
    for name, factory in providers.PROVIDERS.items():
        provider = providers.build(name)
        marker = f" {S.OK}◀ connected{S.R}" if name == active else ""
        problem = provider.ready()
        state = f"{S.ERR}{problem}{S.R}" if problem else f"{S.OK}ready{S.R}"
        print(f"  {S.ACCENT}{provider.label}{S.R} {S.MUTED}({name}){S.R}  {state}{marker}")
        if provider.model:
            print(f"  {S.MUTED}│{S.R}  {S.GRAY}model{S.R}  {provider.model}")
        how = ("native tool calling" if provider.supports_native_tools
               and getattr(config, "NATIVE_TOOLS", True)
               else "text <tool_call> protocol")
        print(f"  {S.MUTED}│{S.R}  {S.GRAY}tools{S.R}  {S.MUTED}{how}{S.R}")
        if provider.key_env:
            source = provider.key_source or f"{S.MUTED}not set{S.R}"
            print(f"  {S.MUTED}│{S.R}  {S.GRAY}key{S.R}    {source}"
                  f"   {S.MUTED}{provider.key_help}{S.R}")
        print(f"  {S.MUTED}╰─{S.R}")
    print(f"\n  {S.GRAY}Keys are read from the environment first, then "
          f"{S.MUTED}{providers.CONFIG_PATH}{S.GRAY}.{S.R}")
    print(f"  {S.GRAY}Connect with {S.ACCENT}/connect <provider> [model]{S.GRAY}, "
          f"delete a saved key with {S.ACCENT}/connect forget <provider>{S.GRAY}.{S.R}\n")


def forget(name: str) -> None:
    """`/connect forget <provider>` - take a saved API key back out."""
    if not name:
        saved = [n for n in providers.PROVIDERS
                 if providers.settings_for(n).get("api_key")]
        if not saved:
            print(f"  {S.GRAY}No API key is saved for any provider.{S.R}\n")
            return
        print(f"  {S.ERR}✗ Usage: /connect forget <provider>{S.R}")
        print(f"  {S.GRAY}Keys are saved for: {', '.join(saved)}{S.R}\n")
        return

    name = name.lower()
    removed, detail = providers.forget_key(name)
    if not removed:
        if detail:
            print(f"  {S.ERR}✗ {detail}{S.R}\n")
        else:
            print(f"  {S.GRAY}No API key was saved for {name}.{S.R}\n")
        return

    provider = providers.build(name)
    print(f"  {S.OK}✓ Deleted the API key saved for {provider.label}.{S.R} "
          f"{S.MUTED}({detail}){S.R}")
    if provider.key_source:
        # An environment variable still supplies one, and unsetting it is not
        # something this program can do for them. Better said than implied.
        print(f"  {S.WARN}⚠ {provider.label} still has a key from "
              f"{provider.key_source}{S.MUTED} - unset it in your shell to "
              f"remove that one.{S.R}")
    elif providers.current().name == name:
        print(f"  {S.WARN}⚠ That is the provider you are connected to, so it "
              f"cannot answer until you {S.ACCENT}/connect{S.WARN} again.{S.R}")
    print()


def _pick_provider() -> str:
    rows = [providers.build(name) for name in providers.PROVIDERS]
    active = providers.current().name

    def render(provider):
        mark = f" {S.OK}◀ connected{S.R}" if provider.name == active else ""
        problem = provider.ready()
        note = f"{S.MUTED}{problem}{S.R}" if problem else f"{S.GRAY}{provider.model}{S.R}"
        return f"{S.WHITE}{provider.label}{S.R}  {note}{mark}"

    index = _choose("Connect to", rows, render)
    return rows[index].name if index >= 0 else ""


def _ensure_key(name: str) -> bool:
    """Ask for an API key if the provider needs one and has none."""
    provider = providers.build(name)
    if not provider.needs_key or provider.api_key:
        return True

    print(f"\n  {S.WARN}{provider.label} needs an API key.{S.R}")
    print(f"  {S.MUTED}{provider.key_help}{S.R}")
    print(f"  {S.MUTED}Set {' or '.join(provider.key_env)} in the environment to keep it "
          f"out of a file, or paste it here to save it to{S.R}")
    print(f"  {S.MUTED}{providers.CONFIG_PATH} (owner-only).{S.R}\n")
    key = _ask(f"  {S.INFO}API key{S.R} {S.MUTED}(blank to cancel){S.R} {S.INFO}›{S.R} ",
               title=f"{provider.label} API key", keyboard_only=True)
    if not key:
        print(f"  {S.GRAY}Cancelled.{S.R}\n")
        return False
    providers.settings_for(name)["api_key"] = key
    return True


def _pick_model(name: str) -> str:
    provider = providers.build(name)
    print(f"  {S.MUTED}⟳ asking {provider.label} what it offers…{S.R}", end="", flush=True)
    try:
        models = provider.list_models()
    except Exception as error:
        # Wrapped, not cut. This is where a wrong API key is normally met, and
        # the sentence that says which key and how to fix it is longer than any
        # single line - truncating it left the one useful half off the end.
        print("\r\033[K", end="")
        _print_problem("Could not list models", error)
        return _ask(f"  {S.INFO}Model id{S.R} {S.MUTED}(blank to cancel){S.R} {S.INFO}›{S.R} ",
                    title=f"Which {provider.label} model?")
    print("\r\033[K", end="")

    if not models:
        return _ask(f"  {S.INFO}Model id{S.R} {S.MUTED}(blank to cancel){S.R} {S.INFO}›{S.R} ",
                    title=f"Which {provider.label} model?")

    def render(entry):
        detail = f"  {S.GRAY}{entry['detail']}{S.R}" if entry.get("detail") else ""
        return f"{S.WHITE}{entry['name']}{S.R}{detail}"

    index = _choose(f"{provider.label} models", models, render)
    return models[index]["name"] if index >= 0 else ""


def run(argument: str) -> None:
    """Handle `/connect ...`. Everything it prints, it prints itself."""
    parts = argument.split()
    if parts and parts[0].lower() in ("status", "list", "-l"):
        show_status()
        return
    if parts and parts[0].lower() in ("forget", "logout"):
        forget(parts[1] if len(parts) > 1 else "")
        return

    name = parts[0].lower() if parts else ""
    model = parts[1] if len(parts) > 1 else ""

    if name and name not in providers.PROVIDERS:
        print(f"  {S.ERR}✗ Unknown provider '{name}'. "
              f"Choose from: {', '.join(providers.PROVIDERS)}{S.R}\n")
        return

    if not name:
        name = _pick_provider()
        if not name:
            print(f"  {S.GRAY}Cancelled.{S.R}\n")
            return

    if not _ensure_key(name):
        return

    if not model:
        model = _pick_model(name)
        if not model:
            print(f"  {S.GRAY}Cancelled.{S.R}\n")
            return

    provider, problem = providers.connect(name, model=model)
    if problem:
        print(f"  {S.WARN}⚠ Connected to {provider.label}, but {problem}.{S.R}\n")
        return
    where = f" {S.MUTED}(key {provider.key_source}){S.R}" if provider.key_source else ""
    print(f"  {S.OK}✓ Connected: {provider.label} · {provider.model}{S.R}{where}\n")


if __name__ == "__main__":
    print("This file can not run directly.")
