"""Speech-to-text with faster-whisper (runs locally)."""

from __future__ import annotations

import logging

import numpy as np

from .config import STTConfig

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000


class WhisperSTT:
    def __init__(self, cfg: STTConfig):
        from faster_whisper import WhisperModel

        log.info("Loading Whisper model %s on %s", cfg.model, cfg.device)
        self.cfg = cfg
        self.model = WhisperModel(cfg.model, device=cfg.device, compute_type=cfg.compute_type)

    def transcribe(self, pcm16: bytes) -> str:
        """Transcribe 16 kHz mono signed 16-bit PCM audio."""
        audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        if audio.size < SAMPLE_RATE // 4:  # under 250 ms: nothing useful
            return ""
        segments, _ = self.model.transcribe(
            audio,
            language=self.cfg.language,
            beam_size=1,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        return " ".join(s.text.strip() for s in segments).strip()
