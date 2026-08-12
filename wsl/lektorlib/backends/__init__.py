"""Backend registry.

Imports are lazy on purpose: asking for the elevenlabs backend must not drag in torch, and asking
for chatterbox must not require a network key.
"""

from __future__ import annotations

from .base import Backend, Rendered, Utterance

__all__ = ["Backend", "Rendered", "Utterance", "get_backend", "BACKEND_NAMES"]

BACKEND_NAMES = ("elevenlabs", "piper", "chatterbox")


def get_backend(name: str, cfg) -> Backend:
    key = (name or "").strip().lower()
    if key == "elevenlabs":
        from .elevenlabs import ElevenLabsBackend

        return ElevenLabsBackend(cfg)
    if key == "piper":
        from .piper import PiperBackend

        return PiperBackend(cfg)
    if key == "chatterbox":
        from .chatterbox import ChatterboxBackend

        return ChatterboxBackend(cfg)
    raise ValueError(f"unknown voice backend {name!r} -- pick one of {', '.join(BACKEND_NAMES)}")
