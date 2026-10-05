"""Chat with a local model through Ollama's HTTP API."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator

import httpx

from .config import LLMConfig

# A sentence ends at . ! or ? followed by whitespace (or a newline anywhere).
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")


class OllamaLLM:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.client = httpx.AsyncClient(base_url=cfg.base_url, timeout=httpx.Timeout(120.0))

    async def check(self) -> None:
        """Fail early with a clear message if Ollama or the model is missing."""
        try:
            resp = await self.client.get("/api/tags")
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise RuntimeError(
                f"Cannot reach Ollama at {self.cfg.base_url}. Is `ollama serve` running?"
            ) from e
        names = {m.get("name") for m in resp.json().get("models", [])}
        wanted = self.cfg.model if ":" in self.cfg.model else f"{self.cfg.model}:latest"
        if wanted not in names:
            raise RuntimeError(
                f"Ollama model '{self.cfg.model}' is not pulled. Run: ollama pull {self.cfg.model}"
            )

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        """Yield the reply token by token."""
        payload = {
            "model": self.cfg.model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": self.cfg.temperature},
        }
        async with self.client.stream("POST", "/api/chat", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                if "error" in chunk:
                    raise RuntimeError(chunk["error"])
                text = chunk.get("message", {}).get("content", "")
                if text:
                    yield text
                if chunk.get("done"):
                    break

    async def extract(self, messages: list[dict], schema: dict) -> dict:
        """Return JSON matching `schema` (Ollama structured output)."""
        payload = {
            "model": self.cfg.model,
            "messages": messages,
            "stream": False,
            "format": schema,
            "options": {"temperature": 0},
        }
        resp = await self.client.post("/api/chat", json=payload)
        resp.raise_for_status()
        data = json.loads(resp.json()["message"]["content"])
        return data if isinstance(data, dict) else {}

    async def aclose(self) -> None:
        await self.client.aclose()


async def sentences(tokens: AsyncIterator[str]) -> AsyncIterator[str]:
    """Group a token stream into sentences so speech can start early."""
    buf = ""
    async for tok in tokens:
        buf += tok
        parts = _SENTENCE_END.split(buf)
        for done in parts[:-1]:
            if done.strip():
                yield done.strip()
        buf = parts[-1]
    if buf.strip():
        yield buf.strip()


def clean_for_speech(text: str) -> str:
    """Strip markdown and symbols that TTS would read out literally."""
    text = re.sub(r"[*_`#>|~]+", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # [label](url) -> label
    text = re.sub(r"^\s*[-•]\s+", "", text)
    return re.sub(r"\s+", " ", text).strip()
