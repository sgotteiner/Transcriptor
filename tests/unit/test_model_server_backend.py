"""ModelServerBackend: posts float32 audio to the inference server and parses text.

The HTTP client is faked so this stays a fast unit test (no server, no model).
"""

import numpy as np

from app.backends.model_server import ModelServerBackend
from app.core.chunking import SAMPLE_RATE
from app.models import Backend


class _FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self):
        self.calls = []

    async def post(self, url, params=None, content=None, headers=None):
        self.calls.append({"url": url, "params": params, "content": content})
        return _FakeResponse({"text": "  hello world  "})


async def test_posts_audio_and_returns_text():
    be = ModelServerBackend("http://model-server:8001")
    fake = _FakeClient()
    be._client = fake  # inject fake client

    samples = np.array([0.1, 0.2, 0.3], dtype=np.float32)
    text = await be.transcribe(samples, SAMPLE_RATE, "openai/whisper-tiny")

    assert text == "hello world"                       # stripped
    assert be.kind is Backend.LOCAL                     # it is the local tier
    call = fake.calls[0]
    assert call["url"].endswith("/transcribe")
    assert call["params"]["model_id"] == "openai/whisper-tiny"
    assert call["content"] == samples.tobytes()         # raw float32 bytes
