"""60db STT — peer of speech_processing.speech_to_text.get_transcript.

Exposes the same `async get_transcript(callback)` signature so
conversation_manager.py keeps its existing call shape. Uses PyAudio
to capture mic frames at 16 kHz LINEAR16 PCM and pushes them to
60db's STT WebSocket in "browser mode" (JSON-enveloped base64 PCM).

The first transcription event with is_final + speech_final closes the
WS and fires the callback — matching how speech_to_text.py closes
after one final per turn so the conversation loop can advance.

Reference: https://docs.60db.ai/websocket-api/stt
"""

from __future__ import annotations

import asyncio
import base64
import json
import os

import pyaudio
from dotenv import load_dotenv

load_dotenv()

DEFAULT_API_BASE = "https://api.60db.ai"
SAMPLE_RATE = 16000
CHUNK_MS = 60
CHUNK_BYTES = int(SAMPLE_RATE * 2 * (CHUNK_MS / 1000))  # 16-bit mono


async def get_transcript(callback):
    """Mic → 60db /ws/stt → final transcript → callback(text)."""
    try:
        import websockets
    except ImportError as e:
        print(f"60db STT requires 'websockets': {e}")
        return

    api_key = os.getenv("SIXTYDB_API_KEY")
    if not api_key:
        print("SIXTYDB_API_KEY not set")
        return
    api_base = os.getenv("SIXTYDB_API_BASE", DEFAULT_API_BASE).rstrip("/")
    ws_base = api_base.replace("https://", "wss://").replace("http://", "ws://")
    url = f"{ws_base}/ws/stt?apiKey={api_key}"

    print("Listening...")

    transcription_complete = asyncio.Event()
    final_text: dict[str, str] = {"text": ""}
    sender_task: asyncio.Task | None = None

    pa = pyaudio.PyAudio()
    stream = pa.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=CHUNK_BYTES // 2,
    )

    try:
        async with websockets.connect(url, max_size=None) as ws:
            # 1. The docs specify the server emits {"connecting": true, ...}
            #    BEFORE {"connection_established": {...}}. Loop until we see
            #    connection_established, ignoring intermediate handshake
            #    frames so a future protocol addition can't break startup.
            while True:
                probe = json.loads(await ws.recv())
                if "connection_established" in probe:
                    break
                # "connecting" or any other transient handshake frame
                # is informational — skip silently.
            # 2. Send start config — linear PCM 16k, voicebot-friendly tuning.
            lang = os.getenv("SIXTYDB_STT_LANGUAGE", "en")
            await ws.send(
                json.dumps(
                    {
                        "type": "start",
                        "languages": [lang],
                        "config": {
                            "encoding": "linear",
                            "sample_rate": SAMPLE_RATE,
                            "utterance_end_ms": 500,
                            "continuous_mode": False,
                            "interim_results_frequency": 300,
                            "audio_enhancement": "adaptive",
                        },
                    }
                )
            )

            async def _send_audio():
                # Read mic in a thread (PyAudio is blocking) and forward
                # to the WS as base64 JSON envelopes.
                loop = asyncio.get_running_loop()
                while not transcription_complete.is_set():
                    try:
                        chunk = await loop.run_in_executor(
                            None, stream.read, CHUNK_BYTES // 2, False
                        )
                    except OSError:
                        break
                    if not chunk:
                        continue
                    try:
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "audio",
                                    "audio": base64.b64encode(chunk).decode(),
                                    "encoding": "linear",
                                    "sample_rate": SAMPLE_RATE,
                                }
                            )
                        )
                    except Exception:
                        break

            # 3. Run recv loop until we get a canonical final.
            async for raw in ws:
                msg = json.loads(raw)
                msg_type = msg.get("type")
                if msg_type == "connected" and sender_task is None:
                    sender_task = asyncio.create_task(_send_audio())
                    continue
                if msg_type == "speech_started":
                    # Informational — server detected speech onset.
                    continue
                if msg_type == "session_stopped":
                    # Server-side close after our `stop` request.
                    break
                if msg_type != "transcription":
                    continue
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                if msg.get("is_final") and msg.get("speech_final"):
                    final_text["text"] = text
                    print(f"Human: {text}")
                    callback(text)
                    transcription_complete.set()
                    # Per docs: client-driven termination is
                    # `{"type":"stop"}` → server emits `session_stopped`
                    # with billing summary. Send it and wait briefly
                    # for the ack so the credit balance is recorded.
                    try:
                        await ws.send(json.dumps({"type": "stop"}))
                    except Exception:
                        pass
                    try:
                        # Drain at most one more frame for session_stopped.
                        await asyncio.wait_for(ws.recv(), timeout=2.0)
                    except (asyncio.TimeoutError, Exception):
                        pass
                    break

            if sender_task:
                sender_task.cancel()
                try:
                    await sender_task
                except (asyncio.CancelledError, Exception):
                    pass
    except Exception as e:  # noqa: BLE001
        print(f"Could not open socket: {e}")
    finally:
        try:
            stream.stop_stream()
            stream.close()
        except Exception:
            pass
        pa.terminate()


# Keep parity with speech_to_text.py's module-level test entry point.
def handle_full_sentence(full_sentence):
    print(f"DEBUG transcript: {full_sentence}")


if __name__ == "__main__":
    asyncio.run(get_transcript(handle_full_sentence))
