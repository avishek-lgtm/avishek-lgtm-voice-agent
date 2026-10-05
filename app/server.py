"""Web server: serves the talk page and runs the voice loop over a WebSocket.

Protocol on /ws
  browser -> server
    binary frame                      one utterance, 16 kHz mono PCM16
    {"type": "text", "text": "..."}   typed message (no microphone needed)
    {"type": "interrupt"}             stop the current reply
    {"type": "reset"}                 forget the conversation
  server -> browser
    {"type": "ready", "agent": name}
    {"type": "user", "text": ...}         what Whisper heard
    {"type": "status", "state": ...}      listening | thinking | speaking
    {"type": "assistant", "text": ...}    one sentence of the reply,
    binary frame                          followed by its WAV audio
    {"type": "lead", ...}                 caller details captured so far
    {"type": "done"} / {"type": "error", "message": ...}
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from contextlib import asynccontextmanager
from typing import Protocol

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from .config import ROOT, Config, load_config
from .intake import Intake
from .leads import LeadStore
from .llm import clean_for_speech, sentences

log = logging.getLogger(__name__)


class STT(Protocol):
    def transcribe(self, pcm16: bytes) -> str: ...


class TTS(Protocol):
    def synthesize(self, text: str) -> bytes: ...


class Conversation:
    """One connected caller: history plus the reply currently being spoken."""

    def __init__(self, ws: WebSocket, cfg: Config, stt: STT, llm, tts: TTS, store: LeadStore):
        self.ws, self.cfg, self.stt, self.llm, self.tts, self.store = ws, cfg, stt, llm, tts, store
        self.history: list[dict] = []
        self.task: asyncio.Task | None = None
        self.intake = Intake(cfg, store, llm)

    def messages(self) -> list[dict]:
        keep = self.cfg.llm.max_history_turns * 2
        system = self.cfg.full_system_prompt() + "\n\n" + self.intake.context()
        return [{"role": "system", "content": system}] + self.history[-keep:]

    def reset(self) -> None:
        self.intake.save(self.history)
        self.history.clear()
        self.intake = Intake(self.cfg, self.store, self.llm)

    async def speak(self, text: str) -> None:
        await self.ws.send_json({"type": "assistant", "text": text})
        audio = await asyncio.to_thread(self.tts.synthesize, text)
        await self.ws.send_bytes(audio)

    async def greet(self) -> None:
        greeting = self.cfg.agent.greeting
        self.history.append({"role": "assistant", "content": greeting})
        await self.ws.send_json({"type": "status", "state": "speaking"})
        await self.speak(greeting)
        await self.ws.send_json({"type": "done"})

    async def respond(self, user_text: str) -> None:
        self.history.append({"role": "user", "content": user_text})
        await self.ws.send_json({"type": "status", "state": "thinking"})
        await self.intake.update(self.history)
        await self.ws.send_json({"type": "lead", **self.intake.snapshot()})
        spoken: list[str] = []
        try:
            async for sentence in sentences(self.llm.stream(self.messages())):
                sentence = clean_for_speech(sentence)
                if not sentence:
                    continue
                if not spoken:
                    await self.ws.send_json({"type": "status", "state": "speaking"})
                spoken.append(sentence)
                await self.speak(sentence)
            await self.ws.send_json({"type": "done"})
        finally:
            # Keep whatever was actually said, even if the caller interrupted.
            if spoken:
                self.history.append({"role": "assistant", "content": " ".join(spoken)})
            self.intake.save(self.history)

    async def handle_audio(self, pcm16: bytes) -> None:
        text = await asyncio.to_thread(self.stt.transcribe, pcm16)
        if not text:
            await self.ws.send_json({"type": "status", "state": "listening"})
            return
        await self.ws.send_json({"type": "user", "text": text})
        await self.respond(text)

    async def run_turn(self, coro) -> None:
        """Start a new turn, cancelling any reply still in progress (barge-in)."""
        await self.cancel()

        async def guarded():
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception as e:  # report to the page instead of dropping the socket
                log.exception("Turn failed")
                with contextlib.suppress(Exception):
                    await self.ws.send_json({"type": "error", "message": str(e)})

        self.task = asyncio.create_task(guarded())

    async def cancel(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        self.task = None


def create_app(
    cfg: Config | None = None,
    stt: STT | None = None,
    llm=None,
    tts: TTS | None = None,
    store: LeadStore | None = None,
) -> FastAPI:
    """Build the app. Components can be injected (tests); otherwise real models load at startup."""
    state: dict = {"cfg": cfg, "stt": stt, "llm": llm, "tts": tts, "store": store}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if state["cfg"] is None:
            state["cfg"] = load_config()
        c: Config = state["cfg"]
        if state["store"] is None:
            state["store"] = LeadStore(c.database)
        if state["llm"] is None:
            from .llm import OllamaLLM

            state["llm"] = OllamaLLM(c.llm)
            await state["llm"].check()
        if state["stt"] is None:
            from .stt import WhisperSTT

            state["stt"] = WhisperSTT(c.stt)
        if state["tts"] is None:
            from .tts import PiperTTS

            state["tts"] = PiperTTS(c.tts)
        log.info("Voice agent '%s' ready", c.agent.name)
        yield
        if hasattr(state["llm"], "aclose"):
            await state["llm"].aclose()

    app = FastAPI(title="Local voice agent", lifespan=lifespan)

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        conv = Conversation(ws, state["cfg"], state["stt"], state["llm"], state["tts"], state["store"])
        await ws.send_json({"type": "ready", "agent": conv.cfg.agent.name})
        await conv.run_turn(conv.greet())
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes"):
                    await conv.run_turn(conv.handle_audio(msg["bytes"]))
                    continue
                data = _parse_json(msg.get("text"))
                kind = data.get("type")
                if kind == "text" and data.get("text", "").strip():
                    text = data["text"].strip()
                    await ws.send_json({"type": "user", "text": text})
                    await conv.run_turn(conv.respond(text))
                elif kind == "interrupt":
                    await conv.cancel()
                    await ws.send_json({"type": "status", "state": "listening"})
                elif kind == "reset":
                    await conv.cancel()
                    conv.reset()
                    await ws.send_json({"type": "lead", **conv.intake.snapshot()})
                    await ws.send_json({"type": "status", "state": "listening"})
        except WebSocketDisconnect:
            pass
        finally:
            await conv.cancel()
            conv.intake.save(conv.history)

    @app.get("/api/leads")
    def list_leads():
        return state["store"].list_leads()

    @app.get("/api/leads.csv")
    def leads_csv():
        return Response(
            state["store"].to_csv(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="leads.csv"'},
        )

    app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")
    return app


def _parse_json(text: str | None) -> dict:
    try:
        data = json.loads(text or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}
