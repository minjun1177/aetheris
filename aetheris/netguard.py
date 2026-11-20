"""Where a request that nobody approves may go.

`get_url` and the pages `search_web` reads run without an approval prompt, so
the model - or a page that told it what to do - chooses the address. Pointed
at this machine or its network, that is a read of things the person never
meant to hand over: an admin page on the router, a dev server's debug
endpoint, and on a cloud VM `169.254.169.254`, which answers with the
machine's credentials. So an unapproved request may only go to a public
address. Anything else goes through `call_api`, which asks.

The check resolves the name and refuses when any address it resolves to is
not public; `get_url` repeats it for every redirect. The request resolves the
name again when it connects, so a name that answers differently the second
time (DNS rebinding) is not covered - this narrows the door, it does not
weld it shut.

Stdlib only, importing nothing local but `config`.
"""

import ipaddress
import socket
from urllib.parse import urlsplit


def _not_public(address: str) -> str:
    """Why `address` is not a public one, or ""."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return "an address that could not be read"
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if ip.is_loopback:
        return "this machine"
    if ip.is_link_local:
        return "a link-local address (where cloud metadata lives)"
    if ip.is_private:
        return "a private network address"
    if ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return "an address that is not on the public internet"
    return ""


def refusal(url: str, resolve=None) -> str:
    """"" when an unapproved request may go to `url`, otherwise why not."""
    from aetheris import config
    try:
        parts = urlsplit(url)
    except ValueError:
        return "it is not a URL that can be read"
    if parts.scheme not in ("http", "https"):
        return f"only http and https are fetched, not '{parts.scheme or 'nothing'}'"
    host = parts.hostname or ""
    if not host:
        return "it names no host"
    if config.URL_ALLOW_PRIVATE:
        return ""
    if host.lower() in ("localhost", "localhost.localdomain") or host.lower().endswith(".localhost"):
        return f"{host} is this machine"
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return "its port is not a number"
    try:
        answers = (resolve or socket.getaddrinfo)(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError) as error:
        return f"{host} does not resolve ({error})"
    for answer in answers:
        why = _not_public(answer[4][0])
        if why:
            return f"{host} is {why} ({answer[4][0]})"
    return ""
