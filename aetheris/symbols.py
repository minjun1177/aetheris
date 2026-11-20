"""A symbol table for the whole project, built with Tree-sitter, for `find_symbol`.

`search_in_file` finds text, and a name is text in far more places than it is
defined: in comments, in strings, in every call. A small model asked "where is
`load_rules` defined?" got forty lines back and opened the wrong file. This
answers the question it actually asked: where each definition is, its
signature and the class it belongs to - and, when asked, where the name is used
as an identifier, skipping comments and strings.

The table is per file and kept for the process, keyed on each file's mtime and
size, so a second question costs a `stat` per file rather than a parse.
Parsing and the skeleton walk are `tools.py`'s own (`get_code_skeleton`), so
the two tools agree about what a definition is.
"""

import fnmatch
import os

from aetheris import config

MAX_FILES = 5000            # beyond this the project is indexed only partly, and says so
MAX_FILE_BYTES = 1_000_000  # a generated or vendored file this big is not where code is read
MAX_DEFINITIONS = 40
MAX_REFERENCES = 40

# (root, relative path) -> (mtime_ns, size, [definition, ...])
_cache: dict = {}


def _signature(entry: dict) -> str:
    if entry.get("type") != "function":
        bases = " ".join(entry.get("bases") or [])
        return f"{entry['name']}{bases}"
    parts = []
    for param in entry.get("parameters") or []:
        text = str(param.get("name") or "")
        if param.get("type"):
            text += f": {param['type']}"
        if param.get("default"):
            text += f" = {param['default']}"
        parts.append(text)
    returned = entry.get("return_type")
    tail = f" {returned}" if returned and returned.startswith("->") else (
        f" -> {returned}" if returned else "")
    return f"{entry['name']}({', '.join(parts)}){tail}"


def _flatten(skeleton: list, path: str, container: str = "") -> list:
    found = []
    for entry in skeleton:
        name = entry.get("name") or ""
        if name and name != "<anonymous>":
            found.append({"name": name, "kind": entry.get("type", ""), "file": path,
                          "line": entry.get("line", 0), "end_line": entry.get("end_line", 0),
                          "signature": _signature(entry), "container": container})
        inner = (entry.get("children") or []) + (entry.get("methods") or [])
        if inner:
            found.extend(_flatten(inner, path, name if name != "<anonymous>" else container))
    return found


def _parse(full: str, lang: str):
    from aetheris import tools
    parser, _ = tools._get_ts_parser(lang)
    if parser is None:
        return None, b""
    with open(full, "rb") as handle:
        src = handle.read(MAX_FILE_BYTES + 1)
    if len(src) > MAX_FILE_BYTES:
        return None, b""
    return parser.parse(src), src


def _definitions_in(root: str, path: str) -> list:
    """The definitions in one file, from the cache when the file has not changed."""
    from aetheris import tools
    full = os.path.join(root, path)
    try:
        stat = os.stat(full)
    except OSError:
        return []
    key = (root, path)
    cached = _cache.get(key)
    if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
        return cached[2]
    found = []
    lang = tools._detect_language(path)
    if lang and stat.st_size <= MAX_FILE_BYTES:
        try:
            tree, src = _parse(full, lang)
            if tree is not None:
                found = _flatten(tools._walk_skeleton(tree.root_node, src, lang),
                                 path.replace(os.sep, "/"))
        except Exception:
            found = []          # one file that will not parse is not the index failing
    _cache[key] = (stat.st_mtime_ns, stat.st_size, found)
    return found


def _source_files(root: str) -> tuple:
    """(the project's source files Tree-sitter can read, whether the list was cut)."""
    from aetheris import tools
    files = [name for name in tools._workspace_files(root) if tools._detect_language(name)]
    return files[:MAX_FILES], len(files) > MAX_FILES


def _matches(wanted: str, name: str) -> bool:
    if any(c in wanted for c in "*?["):
        return fnmatch.fnmatchcase(name, wanted)
    return name == wanted


def find(name: str, root: str = "") -> tuple:
    """(definitions named `name`, whether the index was cut short).

    Exact first. A glob (`load_*`) matches by pattern. When nothing is exactly
    called that, a case-insensitive part of the name is tried, so `loadrules`
    still finds `load_rules`'s neighbours rather than nothing at all.
    """
    root = os.path.realpath(root or os.getcwd())
    files, cut = _source_files(root)
    every = [d for path in files for d in _definitions_in(root, path)]
    exact = [d for d in every if _matches(name, d["name"])]
    if exact:
        return exact, cut
    folded = name.lower().replace("_", "")
    loose = [d for d in every if folded and folded in d["name"].lower().replace("_", "")]
    return loose, cut


def references(name: str, root: str = "", skip: tuple = ()) -> list:
    """(file, line, text) where `name` is used as an identifier. Not in comments or strings.

    Only files whose bytes contain the name are parsed, which is what keeps
    this from being a parse of the whole project per question. `skip` holds
    (file, line) pairs to leave out - the definitions already reported.
    """
    from aetheris import tools
    root = os.path.realpath(root or os.getcwd())
    wanted = name.encode("utf-8")
    found = []
    files, _ = _source_files(root)
    for path in files:
        full = os.path.join(root, path)
        try:
            if os.path.getsize(full) > MAX_FILE_BYTES:
                continue
            with open(full, "rb") as handle:
                if wanted not in handle.read():
                    continue
            tree, src = _parse(full, tools._detect_language(path))
        except Exception:
            continue
        if tree is None:
            continue
        lines = src.split(b"\n")
        shown = path.replace(os.sep, "/")
        seen_lines = set()
        stack = [tree.root_node]
        while stack and len(found) < MAX_REFERENCES:
            node = stack.pop()
            if "identifier" in node.type and node.text == wanted:
                line = node.start_point[0]
                if (shown, line + 1) not in skip and line not in seen_lines:
                    seen_lines.add(line)
                    found.append((shown, line + 1,
                                  lines[line].decode("utf-8", "replace").strip()))
                continue
            if "comment" in node.type or node.type in ("string", "string_literal",
                                                       "template_string", "raw_string_literal"):
                continue
            stack.extend(reversed(node.children))
        if len(found) >= MAX_REFERENCES:
            break
    found.sort(key=lambda item: (item[0], item[1]))
    return found


def available() -> bool:
    return bool(config.TREE_SITTER_AVAILABLE)


def forget() -> None:
    _cache.clear()
