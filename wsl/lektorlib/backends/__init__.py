"""Backend registry.

Imports are lazy on purpose: asking for the elevenlabs backend must not drag in torch, and asking
for chatterbox must not require a network key.

kikivoice is the reason the laziness is tested rather than assumed: it ships 153 KB of vendored
client next to this file (kikivoice_client.py plus kikivoice_batch.py), and neither importing the
registry nor importing the kikivoice backend itself may pull any of that in. Both are asserted in
wsl/tests/test_kikivoice_wiring.py, with a spy on sys.meta_path rather than a look at sys.modules
– torch is not installed in this venv, so a sys.modules check would pass on an empty promise.
"""

from __future__ import annotations

from .base import Backend, Rendered, Utterance

__all__ = ["Backend", "Rendered", "Utterance", "get_backend", "BACKEND_NAMES"]

BACKEND_NAMES = ("elevenlabs", "piper", "chatterbox", "omnivoice", "kikivoice")


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
    if key == "kikivoice":
        from .kikivoice import KikiVoiceBackend

        return KikiVoiceBackend(cfg)
    if key == "omnivoice":
        from .omnivoice import OmniVoiceBackend

        return OmniVoiceBackend(cfg)
    raise ValueError(f"unknown voice backend {name!r} – pick one of {', '.join(BACKEND_NAMES)}")
