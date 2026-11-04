"""
Discovery and configuration for the providers folder.

WHAT THIS DELIBERATELY DOES NOT DO
    It does not scan the folder and execute every file in it.

    That is what `core/plugin_loader.py` does, and it is correct there: a plugin
    is a skill and the app cannot know what a skill is without running its
    module. The cost is real and this project has already measured it — plugin
    discovery took 201 ms because two modules imported `requests` at file scope.

    Providers do not need that. There will be a handful of them and they are
    part of the app rather than dropped in by a user, so they are listed by name
    below and imported ONE AT A TIME, only when something asks for one. Choosing
    a provider in the panel imports one small module. Running the assistant on
    Gemini imports none of them, ever.

WHERE THE SETTINGS LIVE
    In `config/api_keys.json` under "providers", alongside every other setting
    the app already keeps there. Not a second config file: one place to back up,
    one place already in `.gitignore`, and one place a user already knows to
    look. `memory/config_manager.py` owns the reading and writing; this module
    only shapes what goes in.
"""
from __future__ import annotations

import importlib
from typing import Any, Optional

from .base import Field, caps_of, validate

# Every provider module in this package, by kind. Listed rather than scanned —
# see the module docstring. A name here that does not import is reported and
# skipped, never raised.
_MODULES: dict[str, tuple[str, ...]] = {
    "llm": ("llm_openai",),
    "stt": ("stt_whisper",),
    "tts": ("tts_piper", "tts_system", "tts_elevenlabs"),
    "engine": ("engine_pipeline",),
}

_declarations: dict[str, dict] = {}     # id -> PROVIDER, for modules imported so far
_instances: dict[str, Any] = {}         # id -> built provider, keyed by config
_failed: dict[str, str] = {}            # module name -> why it would not import


def _load(module_name: str) -> Optional[dict]:
    """Import one provider module and return its declaration, or None."""
    try:
        mod = importlib.import_module(f".{module_name}", __package__)
    except Exception as e:
        _failed[module_name] = f"{type(e).__name__}: {e}"
        return None

    decl = getattr(mod, "PROVIDER", None)
    err = validate(decl or {})
    if err:
        _failed[module_name] = err
        return None

    decl = dict(decl)
    decl["_module"] = module_name
    decl["_available"] = bool(getattr(mod, "available", lambda: True)())
    _declarations[decl["id"]] = decl
    return decl


def discover(kind: str = "") -> list[dict]:
    """Declarations for one kind, or all of them.

    This is the only thing that imports provider modules, and the settings panel
    is the only thing that calls it — on click, not at startup, for the same
    reason the wake-word check was moved off the UI thread: a drawer that opens
    in a measured 2.1 seconds is a drawer people think is broken.
    """
    names = (_MODULES.get(kind, ()) if kind
             else tuple(n for group in _MODULES.values() for n in group))
    out = []
    for n in names:
        found = next((d for d in _declarations.values() if d.get("_module") == n),
                     None)
        out.append(found or _load(n))
    return [d for d in out if d]


def split_id(provider_id: str) -> tuple[str, str]:
    """"openai_compat@minimax" -> ("openai_compat", "minimax").

    The part after "@" names one service in the provider's catalog, which has
    settings of its own — see config_manager._upgrade_ids. An id without one is
    a provider that has no catalog, and its entry is itself."""
    base, _, entry = (provider_id or "").partition("@")
    return base, entry or base


def catalog_entry(provider_id: str) -> dict:
    """The catalog row a service id names, or {} for a provider without one."""
    base, entry = split_id(provider_id)
    decl = get_declaration(base) or {}
    return next((e for e in decl.get("catalog") or () if e.get("id") == entry), {})


def get_declaration(provider_id: str) -> Optional[dict]:
    provider_id = split_id(provider_id)[0]
    if provider_id in _declarations:
        return _declarations[provider_id]
    for group in _MODULES.values():
        for n in group:
            d = _load(n)
            if d and d["id"] == provider_id:
                return d
    return None


def failures() -> dict[str, str]:
    """Modules that would not load, so the panel can say so rather than showing
    a shorter list with no explanation."""
    return dict(_failed)


# ─────────────────────────────────────────────────────────────────────────────
#  Building one
# ─────────────────────────────────────────────────────────────────────────────
def _fingerprint(provider_id: str, cfg: dict) -> str:
    #  is a callable handed in by whoever built this, not a setting. It
    # differs between the panel and the session and would make every provider
    # look newly configured, defeating the cache that exists so a 300 MB speech
    # model is loaded once.
    items = sorted((k, str(v)) for k, v in cfg.items() if k != "log")
    return provider_id + "|" + "|".join(f"{k}={v}" for k, v in items)


def build(provider_id: str, cfg: dict):
    """A live provider, built once per distinct configuration.

    Cached because a provider may hold something expensive — a loaded speech
    model is hundreds of megabytes and takes seconds to bring up, and rebuilding
    it per request would make local mode unusable. The cache key is the
    configuration itself, so changing the model in the panel produces a new
    instance rather than silently reusing the old one.
    """
    key = _fingerprint(provider_id, cfg)
    if key in _instances:
        return _instances[key]

    decl = get_declaration(provider_id)
    if decl is None:
        raise LookupError(f"no provider called {provider_id!r}")
    if not decl.get("_available", True):
        missing = ", ".join(decl.get("requires", ()))
        raise RuntimeError(f"{decl['label']} needs {missing}, which is not installed")

    inst = decl["factory"](cfg)
    if not hasattr(inst, "caps"):
        inst.caps = caps_of(decl)
    _instances[key] = inst
    return inst


def reset() -> None:
    """Drop every built provider. Called when the panel applies a change, so a
    new setting takes effect without a restart — the same promise the voice and
    audio-device pickers already make."""
    _instances.clear()


# ─────────────────────────────────────────────────────────────────────────────
#  Defaults for a provider's stored settings
# ─────────────────────────────────────────────────────────────────────────────
def defaults(provider_id: str) -> dict:
    """What a provider's config looks like before anyone has filled it in,
    derived from its own `needs` so the two can never drift apart."""
    decl = get_declaration(provider_id)
    if decl is None:
        return {}
    return {f.key: f.default for f in decl.get("needs", ()) if isinstance(f, Field)}


def missing_fields(provider_id: str, cfg: dict) -> list[str]:
    """Required fields with nothing in them — what the panel shows in red, and
    what stops a half-configured provider from being tried at runtime."""
    decl = get_declaration(provider_id)
    if decl is None:
        return []
    out = [f.label for f in decl.get("needs", ())
           if isinstance(f, Field) and f.required
           and not str(cfg.get(f.key, "")).strip()]
    # The key is optional for the provider — local servers take none — and is
    # not optional for a hosted service, which refuses every request without
    # one. Reporting it here keeps a keyless MiniMax off the ladder instead of
    # spending a round trip on a 401 each time.
    if (catalog_entry(provider_id).get("local") is False
            and not str(cfg.get("api_key", "")).strip()):
        out.append("API key")
    return out
