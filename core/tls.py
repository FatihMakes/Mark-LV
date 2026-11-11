"""
core/tls.py — which HTTPS certificates the assistant trusts, in one place.

THE PROBLEM
    Python's own HTTPS (urllib, ssl.create_default_context) trusts the
    certificate authorities in the OPERATING SYSTEM's store, and nothing
    else. Windows fills its store on demand: a browser asks Windows for a
    missing root and gets it, Python does not ask. A computer that has never
    needed a given root (or where Windows Update is off or restricted) has
    an incomplete store, and every https call to a site under that root fails:

        CERTIFICATE_VERIFY_FAILED ... unable to get local issuer certificate

    Members saw it as "the weather cannot be fetched due to an SSL problem"
    (MET Norway is signed under HARICA's root, which many Windows installs
    have not fetched yet), while web search worked: requests/httpx carry
    their own list (certifi).

THE FIX
    One context that trusts BOTH: the system's store (a company's or an
    antivirus's own root keeps working) and certifi's list, which every
    install already has (requests depends on it). install() makes it the
    default for every urllib call that does not bring a context of its own,
    so a plugin written tomorrow gets it without knowing this exists.

    Calls that PIN a certificate (the cloud body, core/cloud) build their own
    context and are not touched: they trust one certificate and nothing else,
    on purpose. Certificates and host names are checked exactly as before.
"""
from __future__ import annotations

import ssl
import threading

_lock = threading.Lock()
_ctx: ssl.SSLContext | None = None


def add_bundled_roots(ctx: ssl.SSLContext) -> bool:
    """certifi's roots added to `ctx`. False when there is no certifi (the
    system's store alone is then what it trusts, as before)."""
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
        return True
    except Exception as e:
        print(f"[tls] bundled certificate list not loaded: {type(e).__name__}")
        return False


def context() -> ssl.SSLContext:
    """The shared client context: the system's roots plus certifi's, with
    certificate and host name checked as always."""
    global _ctx
    with _lock:
        if _ctx is None:
            ctx = ssl.create_default_context()
            add_bundled_roots(ctx)
            _ctx = ctx
        return _ctx


def https_handler():
    """An HTTPSHandler with context(), for code that builds its own opener
    (urllib.request.build_opener(MyRedirectRule, tls.https_handler()))."""
    import urllib.request
    return urllib.request.HTTPSHandler(context=context())


def install() -> None:
    """Make context() the default for urllib.request.urlopen (calls that pass
    their own `context=` keep it)."""
    import urllib.request
    urllib.request.install_opener(urllib.request.build_opener(https_handler()))
