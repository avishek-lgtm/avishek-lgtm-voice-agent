"""Text-to-speech with Piper (runs locally)."""

from __future__ import annotations

import io
import logging
import wave
from pathlib import Path

from .config import TTSConfig

log = logging.getLogger(__name__)


class PiperTTS:
    def __init__(self, cfg: TTSConfig):
        from piper import PiperVoice, SynthesisConfig

        if not Path(cfg.voice_path).exists():
            raise RuntimeError(
                f"Piper voice not found at {cfg.voice_path}. Run scripts/setup.sh first."
            )
        log.info("Loading Piper voice %s", cfg.voice_path)
        self.voice = PiperVoice.load(cfg.voice_path)
        self.syn_config = SynthesisConfig(length_scale=cfg.length_scale)

    def synthesize(self, text: str) -> bytes:
        """Return a complete WAV file for the given text."""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            self.voice.synthesize_wav(text, wav, syn_config=self.syn_config)
        return buf.getvalue()
