"""60db TTS — peer of speech_processing.text_to_speech.TTS.

Same `TTS().speak(text)` signature so conversation_manager.py keeps its
existing call shape. Three transport surfaces, picked via env:

    SIXTYDB_TTS_TRANSPORT = sync  (default) → POST /tts-synthesize
                         = stream            → POST /tts-stream (NDJSON)
                         = ws                → wss://api.60db.ai/ws/tts

For this push-to-talk loop the sync surface is the natural fit (playsound
blocks during playback anyway). The other two are wired for completeness.

Reference: https://docs.60db.ai
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import wave
from pathlib import Path

import requests
from dotenv import load_dotenv
from playsound import playsound

load_dotenv()

DEFAULT_API_BASE = "https://api.60db.ai"
DEFAULT_VOICE_ID = "fbb75ed2-975a-40c7-9e06-38e30524a9a1"
WS_SAMPLE_RATE = 24000


def _config() -> tuple[str, str, str, str]:
    api_key = os.getenv("SIXTYDB_API_KEY")
    if not api_key:
        raise RuntimeError("SIXTYDB_API_KEY environment variable not set")
    api_base = os.getenv("SIXTYDB_API_BASE", DEFAULT_API_BASE).rstrip("/")
    voice = os.getenv("SIXTYDB_TTS_VOICE_ID", DEFAULT_VOICE_ID)
    transport = os.getenv("SIXTYDB_TTS_TRANSPORT", "sync").strip().lower()
    return api_base, api_key, voice, transport


class TTS:
    """Drop-in replacement for speech_processing.text_to_speech.TTS."""

    def __init__(self):
        # Match the Deepgram path's behaviour — output file in CWD.
        self.filename = "output.mp3"  # WS path overrides to .wav at runtime

    def speak(self, text: str) -> None:
        try:
            api_base, api_key, voice, transport = _config()
            if transport == "stream":
                self._synth_ndjson(text, voice, api_base, api_key)
            elif transport == "ws":
                self.filename = "output.wav"
                self._synth_ws(text, voice, api_base, api_key)
            else:
                self._synth_sync(text, voice, api_base, api_key)
            playsound(self.filename)
        except Exception as e:  # noqa: BLE001 — match the Deepgram TTS error policy
            print(f"Exception: {e}")

    # ---- sync REST (mp3) ---------------------------------------------------

    def _synth_sync(self, text, voice, api_base, api_key):
        payload = {
            "text": text,
            "voice_id": voice,
            "enhance": True,
            "speed": 1,
            "stability": 50,
            "similarity": 75,
            "output_format": "mp3",
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        r = requests.post(
            f"{api_base}/tts-synthesize", json=payload, headers=headers, timeout=60
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("success") or not data.get("audio_base64"):
            raise RuntimeError(f"60db tts-synthesize empty: {data.get('message')}")
        Path(self.filename).write_bytes(base64.b64decode(data["audio_base64"]))

    # ---- NDJSON stream (mp3 chunks) ---------------------------------------

    def _synth_ndjson(self, text, voice, api_base, api_key):
        payload = {"text": text, "voice_id": voice, "output_format": "mp3"}
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        chunks: list[bytes] = []
        with requests.post(
            f"{api_base}/tts-stream",
            json=payload,
            headers=headers,
            stream=True,
            timeout=120,
        ) as r:
            r.raise_for_status()
            for raw in r.iter_lines(decode_unicode=True):
                line = (raw or "").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") == "error":
                    raise RuntimeError(f"60db tts-stream error: {line[:200]}")
                if msg.get("type") == "complete":
                    break
                content = msg.get("audioContent")
                if content:
                    chunks.append(base64.b64decode(content))
        Path(self.filename).write_bytes(b"".join(chunks))

    # ---- WebSocket (LINEAR16 PCM → WAV) -----------------------------------

    def _synth_ws(self, text, voice, api_base, api_key):
        try:
            import websockets  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "SIXTYDB_TTS_TRANSPORT=ws requires the 'websockets' package"
            ) from e
        pcm = asyncio.new_event_loop().run_until_complete(
            _ws_collect(text, voice, api_base, api_key)
        )
        if not pcm:
            raise RuntimeError("60db ws/tts returned no audio")
        # Wrap raw 16-bit PCM into a WAV container that playsound can
        # consume without any extra deps.
        with wave.open(self.filename, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(WS_SAMPLE_RATE)
            wav.writeframes(pcm)


async def _ws_collect(text, voice, api_base, api_key) -> bytes:
    import websockets

    ws_base = api_base.replace("https://", "wss://").replace("http://", "ws://")
    url = f"{ws_base}/ws/tts?apiKey={api_key}"
    context_id = f"ctx-{os.getpid()}"
    pcm_chunks: list[bytes] = []
    async with websockets.connect(url, max_size=None) as ws:
        async for raw in ws:
            msg = json.loads(raw)
            if "connection_established" in msg:
                await ws.send(
                    json.dumps(
                        {
                            "create_context": {
                                "context_id": context_id,
                                "voice_id": voice,
                                "audio_config": {
                                    "audio_encoding": "LINEAR16",
                                    "sample_rate_hertz": WS_SAMPLE_RATE,
                                },
                            }
                        }
                    )
                )
                continue
            if "context_created" in msg:
                await ws.send(
                    json.dumps(
                        {"send_text": {"context_id": context_id, "text": text}}
                    )
                )
                await ws.send(
                    json.dumps({"flush_context": {"context_id": context_id}})
                )
                continue
            chunk_b64 = msg.get("audio_chunk", {}).get("audioContent")
            if chunk_b64:
                pcm_chunks.append(base64.b64decode(chunk_b64))
                continue
            if "flush_completed" in msg:
                try:
                    await ws.send(
                        json.dumps(
                            {"close_context": {"context_id": context_id}}
                        )
                    )
                except Exception:
                    pass
                break
    return b"".join(pcm_chunks)


if __name__ == "__main__":
    tts = TTS()
    tts.speak("Hello from 60db, this is a test.")
