"""Aetheris - a terminal AI assistant built for small local models.

Nothing is imported here on purpose. `config` builds the system prompt at import
time (ARCHITECTURE 5.2), so anything this file pulled in would be dragged into
every `from aetheris import ...` in the package, and the import order that
invariant depends on would stop being obvious.

The names in `__all__` are the library: the parts that work on their own and
are promised to keep working the same way until the next major version
(CHANGELOG, Compatibility). Each is imported the first time it is asked for, so
`import aetheris` still loads nothing. Everything else under `aetheris.*` is the
harness's implementation and changes when the harness needs it to.
"""

from typing import TYPE_CHECKING

__version__ = "1.1.0"

__all__ = ["MCPError", "MCPServer", "ShellSession", "start_shell"]

# public name -> (module, attribute). Both modules are stdlib-only at import
# time; settings are read from `config` when they are first needed.
_PUBLIC = {
    "MCPError": ("mcp_client", "MCPError"),
    "MCPServer": ("mcp_client", "MCPServer"),
    "ShellSession": ("shell_session", "Session"),
    "start_shell": ("shell_session", "start"),
}

if TYPE_CHECKING:
    # What a type checker sees instead of the lazy lookup below - which is kept
    # from it, so that a misspelt name is an error rather than an `Any`.
    from aetheris.mcp_client import MCPError, MCPServer
    from aetheris.shell_session import Session as ShellSession
    from aetheris.shell_session import start as start_shell
else:
    def __getattr__(name: str):
        if name not in _PUBLIC:
            raise AttributeError(f"module 'aetheris' has no attribute '{name}'")
        import importlib
        module, attribute = _PUBLIC[name]
        value = getattr(importlib.import_module(f"aetheris.{module}"), attribute)
        globals()[name] = value
        return value

    def __dir__() -> list:
        return sorted(set(globals()) | set(__all__))
