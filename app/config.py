"""Loads the agent configuration (persona, knowledge and model settings)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "agent.yaml"


@dataclass
class AgentConfig:
    name: str = "Assistant"
    company: str = ""
    greeting: str = "Hello, how can I help you?"
    system_prompt: str = "You are a helpful voice assistant. Answer briefly."
    knowledge: list[dict] = field(default_factory=list)


@dataclass
class LLMConfig:
    base_url: str = "http://localhost:11434"
    model: str = "llama3.2:3b"
    temperature: float = 0.4
    max_history_turns: int = 8


@dataclass
class STTConfig:
    model: str = "base.en"
    device: str = "cpu"
    compute_type: str = "int8"
    language: str | None = "en"


@dataclass
class TTSConfig:
    voice_path: str = "voices/en_US-lessac-medium.onnx"
    length_scale: float = 1.0


@dataclass
class Config:
    agent: AgentConfig
    llm: LLMConfig
    stt: STTConfig
    tts: TTSConfig

    def full_system_prompt(self) -> str:
        """System prompt with the knowledge base appended."""
        prompt = self.agent.system_prompt.strip()
        if self.agent.knowledge:
            facts = "\n".join(
                f"Q: {item.get('q', '').strip()}\nA: {item.get('a', '').strip()}"
                for item in self.agent.knowledge
            )
            prompt += "\n\nKnowledge:\n" + facts
        return prompt


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.environ.get("AGENT_CONFIG") or DEFAULT_CONFIG)
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    tts = TTSConfig(**(raw.get("tts") or {}))
    # Relative voice paths are resolved from the repo root, not the cwd.
    if not Path(tts.voice_path).is_absolute():
        tts.voice_path = str(ROOT / tts.voice_path)

    agent = AgentConfig(**(raw.get("agent") or {}))
    if raw.get("knowledge"):
        agent.knowledge = list(raw["knowledge"])

    return Config(
        agent=agent,
        llm=LLMConfig(**(raw.get("llm") or {})),
        stt=STTConfig(**(raw.get("stt") or {})),
        tts=tts,
    )
