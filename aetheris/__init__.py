"""Aetheris - a terminal AI assistant built for small local models.

Nothing is imported here on purpose. `config` builds the system prompt at import
time (ARCHITECTURE 5.2), so anything this file pulled in would be dragged into
every `from aetheris import ...` in the package, and the import order that
invariant depends on would stop being obvious.
"""

__version__ = "1.0.0"
