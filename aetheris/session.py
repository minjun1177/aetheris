import os
import re
import json
import datetime

from aetheris import atomic
from aetheris import config
from aetheris import paths
from aetheris import providers
from aetheris.config import S, ttlp


def load_memory() -> dict:
    if os.path.exists(config.MEMORY_FILE):
        try:
            with open(config.MEMORY_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            # `memory.json` is a plain file in the user's own directory and is
            # edited by hand often enough to matter. Anything that is not a
            # table of entries reads as no memories rather than as an
            # AttributeError from inside a tool call.
            return loaded if isinstance(loaded, dict) else {}
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def _entry(memory: dict, memory_id: str) -> dict:
    """One memory as a record, whatever shape it is stored in."""
    value = memory.get(memory_id)
    return value if isinstance(value, dict) else {"content": str(value or "")}


def _important(entry: dict) -> bool:
    """Whether a record is marked to be put in front of the model at the start.

    A flag on the entry rather than a list of ids kept beside it: one memory is
    one record, and a second place for the same fact to be true in is a second
    place for it to be wrong in. Anything written before the flag existed has
    no key here and reads as not important, which is what it was.
    """
    return bool(entry.get("important"))


# What a model writes for "no". Everything else that is not empty is a yes:
# the parameter exists to mark something, so an unrecognised spelling should
# mark it rather than quietly drop it on the floor.
_NOT_IMPORTANT = ("false", "no", "n", "0", "off", "none", "null")


def _read_flag(raw):
    """The `important` argument as True, False, or None for "did not say"."""
    if isinstance(raw, bool):
        return raw
    text = str(raw if raw is not None else "").strip().lower()
    if not text:
        return None
    return text not in _NOT_IMPORTANT


def save_memory(memory: dict) -> None:
    atomic.write_json(config.MEMORY_FILE, memory)

def handle_write_memory(memory_id: str, content: str, important="") -> str:
    if not memory_id:
        return "[Error] Memory ID is required."
    memory = load_memory()
    flag = _read_flag(important)
    if flag is None:
        # Saving over a memory without mentioning `important` must not demote
        # one that was marked important before. The mark says what the memory
        # is for; a later rewrite of its text is not a decision about that.
        flag = memory_id in memory and _important(_entry(memory, memory_id))
    memory[memory_id] = {
        "content": content,
        "created_at": datetime.datetime.now().isoformat(),
        "important": flag,
    }
    save_memory(memory)
    if flag:
        return (f"[Success] Memory saved: '{memory_id}' (important - it is put "
                "in front of you at the start of every session from now on).")
    return f"[Success] Memory saved: '{memory_id}'"

def handle_get_memory_list() -> str:
    memory = load_memory()
    if not memory:
        return "[Memory] No memories stored."
    lines = []
    for i, mid in enumerate(memory, 1):
        data = _entry(memory, mid)
        created = data.get("created_at", "unknown")
        preview = str(data.get("content", ""))[:50]
        mark = " [important]" if _important(data) else ""
        lines.append(f"{i}. {mid}{mark} ({created}) - {preview}")
    return "\n".join(lines)

def handle_read_memory(memory_id: str) -> str:
    if not memory_id:
        return "[Error] Memory ID is required."
    memory = load_memory()
    if memory_id not in memory:
        return f"[Error] Memory '{memory_id}' not found."
    data = _entry(memory, memory_id)
    return (f"[Memory: {memory_id}]\nContent: {data.get('content', '')}\n"
            f"Created: {data.get('created_at', 'unknown')}\n"
            f"Important: {'yes' if _important(data) else 'no'}")

def handle_delete_memory(memory_id: str) -> str:
    if not memory_id:
        return "[Error] Memory ID is required."
    memory = load_memory()
    if memory_id not in memory:
        return f"[Error] Memory '{memory_id}' not found."
    del memory[memory_id]
    save_memory(memory)
    return f"[Success] Memory deleted: '{memory_id}'"

def handle_edit_memory(memory_id: str, new_content: str) -> str:
    if not memory_id:
        return "[Error] Memory ID is required."
    memory = load_memory()
    if memory_id not in memory:
        return f"[Error] Memory '{memory_id}' not found."
    # An entry written by an older version is a bare string, not a record, and
    # subscripting one raises rather than editing it. Rewriting it whole is the
    # same edit and brings it up to the current shape.
    entry = memory[memory_id]
    if isinstance(entry, dict):
        entry["content"] = new_content
    else:
        memory[memory_id] = {"content": new_content,
                             "created_at": datetime.datetime.now().isoformat()}
    save_memory(memory)
    return f"[Success] Memory edited: '{memory_id}'"


def important_memories() -> list[tuple[str, str]]:
    """Every memory marked important, as (id, content), in the order stored."""
    memory = load_memory()
    pairs = []
    for memory_id in memory:
        data = _entry(memory, memory_id)
        if _important(data):
            pairs.append((memory_id, str(data.get("content", ""))))
    return pairs


def memory_prompt_section() -> str:
    """The important memories, written out for the system prompt.

    A memory is only as useful as the chance the model goes looking for it, and
    a fresh session has no reason to: it does not know there is anything to
    find. `get_memory_list` is a tool it may never call. So the memories the
    model itself marked as the ones that must not be missed are put in the
    prompt instead - read once when the prompt is built, which is the start of
    the session and the few moments `app._refresh_system_prompt` rebuilds it.

    Not every memory, and not the whole of one. The prompt is the part of the
    context that is paid for on every single turn, so this is capped both ways
    (`MEMORY_IMPORTANT_MAX`, `MEMORY_IMPORTANT_CHARS`) and says out loud when
    it has cut something, rather than leaving the model with half a fact it has
    no reason to doubt.
    """
    listed = important_memories()
    if not listed:
        return ""
    shown = listed[:max(0, config.MEMORY_IMPORTANT_MAX)]
    if not shown:
        return ""
    lines = [
        "\n### MEMORY - what you were told to remember:",
        "These were saved with `important` set, so they are put in front of you "
        "at the start of every session instead of waiting to be looked up. They "
        "are what you already know about this user and this work - use them, and "
        "do not call `read_memory` for one that is written out here.",
    ]
    for memory_id, content in shown:
        text = " ".join(content.split())
        if len(text) > config.MEMORY_IMPORTANT_CHARS:
            text = (text[:config.MEMORY_IMPORTANT_CHARS]
                    + f"... (cut - `read_memory` with id '{memory_id}' for the rest)")
        lines.append(f"- {memory_id}: {text}")
    if len(listed) > len(shown):
        lines.append(f"({len(listed) - len(shown)} more are marked important and "
                     "not shown here - `get_memory_list` has them.)")
    return "\n".join(lines) + "\n"


# What `save_session` writes, and every shape `load_session` can still read.
# 4 added `cwd` - the directory the session was last worked in, which is the
# only thing `-c` has to go on.
SESSION_FORMAT = 4
SESSION_FORMATS = (2, 3, 4)


def _timestamp_id() -> str:
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def clean_title(raw: str) -> str:
    """Squeeze a model reply (or a user's /title text) into one usable title line."""
    text = re.sub(r'<think>.*?</think>', '', raw or '', flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<tool_call>.*?</tool_call>', '', text, flags=re.DOTALL)
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return ""
    title = lines[-1]
    for _ in range(2):  # "**Title: x**" needs the markers peeled on both sides of the prefix
        title = re.sub(r'^[\s\-*•#]+', '', title)
        title = re.sub(r'[\s*#]+$', '', title)
        title = re.sub(r'^(?:title|제목)\s*[:：]\s*', '', title, flags=re.IGNORECASE)
    title = title.strip('`').strip('"\'“”‘’').strip()
    title = re.sub(r'\s+', ' ', title).strip(' .。!?')
    return title[:config.SESSION_TITLE_MAX_LEN].strip()


def slugify_title(title: str) -> str:
    """Turn a human title into a filesystem-safe session id ("" if nothing survives).

    The rule itself lives in `paths`, because a note id becomes a filename the
    same way and for the same reasons.
    """
    return paths.safe_name(title, config.SESSION_SLUG_MAX_LEN, "session")


def _unique_session_id(base: str, current_id: str = None) -> str:
    """Append a counter until the id stops colliding with another session file."""
    if not base:
        base = _timestamp_id()
    candidate, n = base, 2
    while True:
        path = os.path.join(config.SESSION_DIR, f"{candidate}.json")
        if candidate == current_id or not os.path.exists(path):
            return candidate
        candidate = f"{base}-{n}"
        n += 1


def save_session(messages: list[dict], session_id: str) -> str:
    if not config.SAVE_CHAT_HISTORY:
        return session_id
    if not os.path.exists(config.SESSION_DIR):
        os.makedirs(config.SESSION_DIR)
    if not session_id:
        # An untitled session starts on a timestamp id; the title (from the model
        # or from /title) renames the file as soon as there is one.
        session_id = _unique_session_id(slugify_title(config.SESSION_TITLE))
    filepath = os.path.join(config.SESSION_DIR, f"{session_id}.json")

    data = {
        "version": SESSION_FORMAT,
        "title": config.SESSION_TITLE,
        "model": config.MODEL,
        "persona": config.CUSTOM_PERSONA,
        "cwd": os.getcwd(),
        "token_history": config.token_history,
        "messages": config.repair_messages(messages),
        "updated_at": datetime.datetime.now().isoformat()
    }

    try:
        atomic.write_json(filepath, data)
    except Exception as e:
        print(f"  {S.ERR}✗ Failed to save session: {e}{S.R}")
    return session_id


def rename_session(session_id: str, new_title: str) -> str:
    """Retitle a session and move its file to match. Returns the (possibly new) id."""
    new_title = clean_title(new_title)
    if not new_title:
        return session_id

    config.SESSION_TITLE = new_title
    old_path = os.path.join(config.SESSION_DIR, f"{session_id}.json") if session_id else ""
    if not old_path or not os.path.exists(old_path):
        return session_id

    new_id = _unique_session_id(slugify_title(new_title), current_id=session_id)
    try:
        with open(old_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data["title"] = new_title
            atomic.write_json(old_path, data)
        if new_id != session_id:
            os.replace(old_path, os.path.join(config.SESSION_DIR, f"{new_id}.json"))
        return new_id
    except Exception as e:
        print(f"  {S.ERR}✗ Failed to rename session: {e}{S.R}")
        return session_id


async def generate_session_title(messages: list[dict]) -> str:
    """Ask the model to name the conversation. Returns "" if it cannot."""
    convo = []
    for m in messages:
        if m["role"] == "system":
            continue
        content = re.sub(r'<tool_call>.*?</tool_call>', '', m.get("content", ""), flags=re.DOTALL).strip()
        if not content or content.startswith("[Tool Result"):
            continue
        role = "User" if m["role"] == "user" else "Assistant"
        convo.append(f"[{role}]: {content[:600]}")
        if len(convo) >= 4:
            break
    if not convo:
        return ""

    prompt = ttlp() + "\n\n".join(convo)
    try:
        text = await providers.complete(
            [{"role": "user", "content": prompt}], max_tokens=40)
        return clean_title(text)
    except Exception:
        return ""


def load_session(session_id: str) -> dict | list:
    filepath = os.path.join(config.SESSION_DIR, f"{session_id}.json")
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as file:
                return json.load(file)
        except Exception:
            pass
    return None


def _dir_key(path: str) -> str:
    """A directory as one comparable string, so two spellings of it match.

    Symlinks are resolved because `/home/me/work` and a symlink pointing at it
    are the same place to work in; case is folded only where the filesystem
    folds it, which `normcase` already knows per platform.
    """
    if not path:
        return ""
    return os.path.normcase(os.path.realpath(os.path.expanduser(path)))


def _session_records() -> list[dict]:
    """Every saved session, parsed once, newest first.

    One pass over the directory serves the listing, the lookup by name and the
    lookup by directory - they only differ in which field they read.
    """
    if not os.path.exists(config.SESSION_DIR):
        return []
    records = []
    for f in os.listdir(config.SESSION_DIR):
        if not f.endswith(".json"):
            continue
        sid = f[:-len(".json")]
        filepath = os.path.join(config.SESSION_DIR, f)
        title, meta_str, updated, cwd = "", "", "", ""
        try:
            with open(filepath, "r", encoding="utf-8") as file:
                data = json.load(file)
            if isinstance(data, dict) and data.get("version") in SESSION_FORMATS:
                title = data.get("title", "")
                updated = data.get("updated_at", "")
                cwd = data.get("cwd", "")          # "" in anything written before 4
                m = data.get("model", "unknown")
                msgs = len(data.get("messages", []))
                tokens = sum(t.get("prompt", 0) + t.get("completion", 0) for t in data.get("token_history", []))
                meta_str = f"[{m}] {msgs} msgs, {tokens} tokens"
            elif isinstance(data, list):
                meta_str = f"[legacy] {len(data)} msgs"
        except Exception:
            meta_str = "[error loading info]"
        if not updated:
            try:
                updated = datetime.datetime.fromtimestamp(os.path.getmtime(filepath)).isoformat()
            except OSError:
                updated = ""
        records.append({"id": sid, "title": title, "meta": meta_str,
                        "updated": updated, "cwd": cwd})
    records.sort(key=lambda r: r["updated"], reverse=True)
    return records


def list_sessions() -> list[tuple[str, str, str]]:
    """Return (session_id, title, meta), newest first."""
    return [(r["id"], r["title"], r["meta"]) for r in _session_records()]


def latest_in_dir(directory: str) -> str:
    """The id of the newest session last worked on in `directory` ("" if none).

    This is what `-c` resolves: continue where you left off *here*, rather than
    whatever conversation happened to be the most recent one anywhere. Sessions
    written before the format recorded a directory have nothing to match on and
    are never picked - guessing at one would be worse than saying there is none.
    """
    key = _dir_key(directory)
    if not key:
        return ""
    for record in _session_records():
        if record["cwd"] and _dir_key(record["cwd"]) == key:
            return record["id"]
    return ""


def find_sessions(query: str) -> list[tuple[str, str, str]]:
    """Look a session up by id or title: exact id, then exact title, then substring."""
    query = (query or "").strip()
    if not query:
        return []
    sessions = list_sessions()
    for entry in sessions:
        if entry[0] == query:
            return [entry]
    q = query.lower()
    exact = [e for e in sessions if e[1].lower() == q]
    if exact:
        return exact
    return [e for e in sessions if q in e[0].lower() or q in e[1].lower()]
