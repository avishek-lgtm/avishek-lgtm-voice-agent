"""Tests that run without any models: STT, LLM and TTS are replaced by fakes."""

import asyncio
import json

from fastapi.testclient import TestClient

from app.config import load_config
from app.llm import clean_for_speech, sentences
from app.server import create_app


class FakeSTT:
    def transcribe(self, pcm16: bytes) -> str:
        return "what are your opening hours" if pcm16 else ""


class FakeLLM:
    def __init__(self):
        self.calls = []

    async def stream(self, messages):
        self.calls.append(messages)
        for tok in ["We are open ", "nine to six. ", "**Anything** else?"]:
            yield tok


class FakeTTS:
    def synthesize(self, text: str) -> bytes:
        return b"RIFF" + text.encode()


def make_client():
    llm = FakeLLM()
    app = create_app(cfg=load_config(), stt=FakeSTT(), llm=llm, tts=FakeTTS())
    return TestClient(app), llm


def receive_until_done(ws):
    events = []
    while True:
        msg = ws.receive()
        if msg.get("bytes") is not None:
            events.append(("audio", msg["bytes"]))
            continue
        data = json.loads(msg["text"])
        events.append((data["type"], data))
        if data["type"] in ("done", "error"):
            return events


def test_config_includes_knowledge():
    cfg = load_config()
    prompt = cfg.full_system_prompt()
    assert "Knowledge:" in prompt
    assert "opening hours" in prompt


def test_sentence_splitting():
    async def tokens():
        for t in ["Hello there. How", " are you? I am", " fine"]:
            yield t

    async def collect():
        return [s async for s in sentences(tokens())]

    assert asyncio.run(collect()) == ["Hello there.", "How are you?", "I am fine"]


def test_clean_for_speech():
    assert clean_for_speech("**Bold** and [link](http://x.y)") == "Bold and link"
    assert clean_for_speech("- item one") == "item one"


def test_greeting_then_text_turn():
    client, llm = make_client()
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "ready"
        greeting = receive_until_done(ws)
        assert any(kind == "audio" for kind, _ in greeting)

        ws.send_json({"type": "text", "text": "When are you open?"})
        events = receive_until_done(ws)
        said = [d["text"] for kind, d in events if kind == "assistant"]
        assert said == ["We are open nine to six.", "Anything else?"]
        assert sum(kind == "audio" for kind, _ in events) == 2

    system, *rest = llm.calls[0]
    assert system["role"] == "system"
    assert rest[-1] == {"role": "user", "content": "When are you open?"}


def test_audio_turn_is_transcribed():
    client, _ = make_client()
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        receive_until_done(ws)
        ws.send_bytes(b"\x00\x01" * 8000)
        events = receive_until_done(ws)
        users = [d["text"] for kind, d in events if kind == "user"]
        assert users == ["what are your opening hours"]


def test_ollama_stream_parsing():
    import httpx

    from app.config import LLMConfig
    from app.llm import OllamaLLM

    body = "\n".join(
        json.dumps(c)
        for c in [
            {"message": {"content": "Hi"}, "done": False},
            {"message": {"content": " there."}, "done": False},
            {"message": {"content": ""}, "done": True},
        ]
    )

    def handler(request):
        sent = json.loads(request.content)
        assert sent["model"] == "llama3.2:3b" and sent["stream"] is True
        return httpx.Response(200, text=body)

    llm = OllamaLLM(LLMConfig())
    llm.client = httpx.AsyncClient(base_url="http://ollama", transport=httpx.MockTransport(handler))

    async def collect():
        return [t async for t in llm.stream([{"role": "user", "content": "hi"}])]

    assert asyncio.run(collect()) == ["Hi", " there."]


def test_talk_page_is_served():
    client, _ = make_client()
    with client:
        resp = client.get("/")
        assert resp.status_code == 200
        assert "app.js" in resp.text
