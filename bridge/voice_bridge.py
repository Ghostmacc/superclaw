#!/usr/bin/env python3
"""
SuperClaw Voice Bridge — Local Voice Interface

Captures mic audio, detects speech via WebRTC VAD, transcribes with
faster-whisper (GPU), sends to Hermes Bridge, speaks response via edge-tts.

Architecture:
  Mic → PipeWire → sounddevice → VAD → faster-whisper → Hermes → edge-tts → Speaker

Usage:
  python voice_bridge.py                    # start with defaults
  WHISPER_MODEL=small python voice_bridge.py  # use larger model
  HERMES_TARGET=developer python voice_bridge.py  # route to specific agent

Environment:
  WHISPER_MODEL   base|small|medium|large  (default: base)
  WHISPER_DEVICE  cuda|cpu                 (default: cuda)
  HERMES_URL      Hermes Bridge URL        (default: http://localhost:8787)
  HERMES_TARGET   Agent to route voice to  (default: claude)
  TTS_VOICE       edge-tts voice name      (default: en-US-AriaNeural)
  VAD_SENSITIVITY 0-3, higher=more sensitive (default: 2)
  VOICE_PORT      API port                 (default: 8686)
"""

import asyncio
import io
import json
import logging
import os
import struct
import tempfile
import threading
import time
import wave
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import edge_tts
import numpy as np
import sounddevice as sd
import soundfile as sf
import uvicorn
import webrtcvad
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from faster_whisper import WhisperModel
from pydub import AudioSegment

# ─── Configuration ───────────────────────────────────────────────────────────

WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cuda")
HERMES_URL = os.getenv("HERMES_URL", "http://localhost:8787")
HERMES_TARGET = os.getenv("HERMES_TARGET", "claude")
TTS_VOICE = os.getenv("TTS_VOICE", "en-US-AriaNeural")
VAD_SENSITIVITY = int(os.getenv("VAD_SENSITIVITY", "2"))
VOICE_PORT = int(os.getenv("VOICE_PORT", "8686"))

SAMPLE_RATE = 16000       # Whisper expects 16kHz
CHANNELS = 1              # Mono
FRAME_MS = 30             # VAD frame size in milliseconds
FRAME_SAMPLES = int(SAMPLE_RATE * FRAME_MS / 1000)
SILENCE_THRESHOLD_MS = 600  # Silence duration to end utterance
SILENCE_FRAMES = int(SILENCE_THRESHOLD_MS / FRAME_MS)
MAX_UTTERANCE_S = 30      # Maximum utterance length in seconds

log = logging.getLogger("voice_bridge")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

# ─── Global State ────────────────────────────────────────────────────────────

whisper_model: WhisperModel = None
vad: webrtcvad.Vad = None
ws_clients: set = set()

class VoiceState:
    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    SPEAKING = "speaking"

state = VoiceState.IDLE
listening_active = False
_listen_thread: threading.Thread = None


# ─── Whisper STT ─────────────────────────────────────────────────────────────

def init_whisper():
    """Load the Whisper model."""
    global whisper_model
    compute = "float16" if WHISPER_DEVICE == "cuda" else "int8"
    log.info("Loading Whisper '%s' on %s (%s)...", WHISPER_MODEL, WHISPER_DEVICE, compute)
    try:
        whisper_model = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=compute)
        log.info("Whisper model loaded")
    except Exception as e:
        log.warning("CUDA failed (%s), falling back to CPU", e)
        whisper_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        log.info("Whisper model loaded (CPU fallback)")


def transcribe(audio_np: np.ndarray) -> str:
    """Transcribe audio numpy array to text."""
    if whisper_model is None:
        return ""
    segments, info = whisper_model.transcribe(
        audio_np,
        beam_size=5,
        language="en",
        vad_filter=True,
    )
    text = " ".join(seg.text.strip() for seg in segments).strip()
    return text


# ─── Voice Activity Detection ────────────────────────────────────────────────

def init_vad():
    """Initialize WebRTC VAD."""
    global vad
    vad = webrtcvad.Vad(VAD_SENSITIVITY)
    log.info("VAD initialized (sensitivity=%d)", VAD_SENSITIVITY)


def is_speech(frame_bytes: bytes) -> bool:
    """Check if a 30ms frame contains speech."""
    try:
        return vad.is_speech(frame_bytes, SAMPLE_RATE)
    except Exception:
        return False


# ─── TTS ─────────────────────────────────────────────────────────────────────

async def synthesize_speech(text: str) -> bytes:
    """Convert text to audio bytes using edge-tts."""
    communicate = edge_tts.Communicate(text, TTS_VOICE)
    audio_chunks = []
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_chunks.append(chunk["data"])
    return b"".join(audio_chunks)


def play_audio_bytes(audio_bytes: bytes):
    """Play MP3 audio bytes through the speaker."""
    audio = AudioSegment.from_mp3(io.BytesIO(audio_bytes))
    samples = np.array(audio.get_array_of_samples(), dtype=np.float32)
    samples = samples / (2**15)  # Normalize int16 to float32

    if audio.channels == 2:
        samples = samples.reshape(-1, 2)

    sd.play(samples, samplerate=audio.frame_rate)
    sd.wait()


# ─── Hermes Client ───────────────────────────────────────────────────────────

async def send_to_hermes(text: str) -> str:
    """Send transcribed text to Hermes Bridge and get response."""
    endpoint = f"{HERMES_URL}/api/v1/claude/ask" if HERMES_TARGET == "claude" \
        else f"{HERMES_URL}/api/v1/agent/ask"

    payload = {
        "caller_id": "voice_bridge",
        "message": text,
        "priority": "normal",
    }

    if HERMES_TARGET != "claude":
        payload["target_agent"] = HERMES_TARGET  # matches Hermes AgentAskRequest schema

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(endpoint, json=payload, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("response", data.get("message", str(data)))
                else:
                    body = await resp.text()
                    log.error("Hermes %d: %s", resp.status, body[:200])
                    return f"Error from Hermes: {resp.status}"
    except aiohttp.ClientError as e:
        log.error("Hermes connection failed: %s", e)
        return "I couldn't reach the agent network. Is Hermes Bridge running?"
    except asyncio.TimeoutError:
        log.error("Hermes request timed out")
        return "The agent took too long to respond."


# ─── WebSocket Broadcast ────────────────────────────────────────────────────

async def broadcast(event: str, data: dict):
    """Broadcast event to all connected WebSocket clients."""
    msg = json.dumps({"event": event, **data})
    dead = set()
    for ws in ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    ws_clients.difference_update(dead)


# ─── Voice Pipeline ─────────────────────────────────────────────────────────

def voice_capture_loop(loop: asyncio.AbstractEventLoop):
    """Main mic capture loop — runs in a background thread."""
    global state
    state = VoiceState.LISTENING
    log.info("Listening... (speak into your mic)")

    speech_buffer = []
    silence_count = 0
    in_speech = False
    max_frames = int(MAX_UTTERANCE_S * 1000 / FRAME_MS)

    def audio_callback(indata, frames, time_info, status):
        nonlocal speech_buffer, silence_count, in_speech

        if not listening_active:
            return

        # Convert float32 to int16 for VAD
        pcm = (indata[:, 0] * 32767).astype(np.int16)
        frame_bytes = pcm.tobytes()

        speech = is_speech(frame_bytes, )

        if speech:
            if not in_speech:
                in_speech = True
                speech_buffer = []
                log.debug("Speech start")
            speech_buffer.append(pcm.copy())
            silence_count = 0
        elif in_speech:
            silence_count += 1
            speech_buffer.append(pcm.copy())

            if silence_count >= SILENCE_FRAMES or len(speech_buffer) >= max_frames:
                # Utterance complete — process it
                audio_data = np.concatenate(speech_buffer)
                in_speech = False
                silence_count = 0
                speech_buffer = []

                # Process in the async event loop
                asyncio.run_coroutine_threadsafe(
                    process_utterance(audio_data.astype(np.float32) / 32767.0),
                    loop,
                )

    try:
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            blocksize=FRAME_SAMPLES,
            dtype="float32",
            callback=audio_callback,
        ):
            while listening_active:
                time.sleep(0.1)
    except Exception as e:
        log.error("Audio capture error: %s", e)
    finally:
        state = VoiceState.IDLE
        log.info("Stopped listening")


async def process_utterance(audio_np: np.ndarray):
    """Process a complete utterance: STT → Hermes → TTS → playback."""
    global state

    if len(audio_np) < SAMPLE_RATE * 0.3:
        return  # Too short, likely noise

    state = VoiceState.PROCESSING

    # Transcribe
    start = time.monotonic()
    text = transcribe(audio_np)
    stt_ms = (time.monotonic() - start) * 1000

    if not text or len(text.strip()) < 2:
        state = VoiceState.LISTENING
        return

    log.info("You: %s (%.0fms STT)", text, stt_ms)
    await broadcast("transcript", {"text": text, "stt_ms": stt_ms})

    # Send to Hermes
    start = time.monotonic()
    response = await send_to_hermes(text)
    hermes_ms = (time.monotonic() - start) * 1000

    log.info("Agent: %s (%.0fms)", response[:100], hermes_ms)
    await broadcast("response", {"text": response, "hermes_ms": hermes_ms})

    # TTS + playback
    state = VoiceState.SPEAKING
    try:
        start = time.monotonic()
        audio_bytes = await synthesize_speech(response)
        tts_ms = (time.monotonic() - start) * 1000
        log.info("TTS: %.0fms, playing...", tts_ms)

        await asyncio.get_event_loop().run_in_executor(None, play_audio_bytes, audio_bytes)
    except Exception as e:
        log.error("TTS/playback error: %s", e)

    state = VoiceState.LISTENING
    await broadcast("state", {"state": state})


# ─── FastAPI App ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: load models. Shutdown: stop capture."""
    init_vad()
    init_whisper()
    log.info("Voice Bridge ready on port %d", VOICE_PORT)
    yield
    global listening_active
    listening_active = False
    log.info("Voice Bridge shutting down")


app = FastAPI(title="SuperClaw Voice Bridge", version="1.0", lifespan=lifespan)


@app.get("/health")
async def health():
    """Health check — reports status of all components."""
    hermes_ok = False
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{HERMES_URL}/api/v1/health", timeout=aiohttp.ClientTimeout(total=3)) as resp:
                hermes_ok = resp.status == 200
    except Exception:
        pass

    devices = sd.query_devices()
    input_devices = [d for d in devices if d["max_input_channels"] > 0]

    return {
        "status": "healthy" if whisper_model and hermes_ok else "degraded",
        "whisper": {
            "model": WHISPER_MODEL,
            "device": WHISPER_DEVICE,
            "loaded": whisper_model is not None,
        },
        "vad": {"loaded": vad is not None, "sensitivity": VAD_SENSITIVITY},
        "tts": {"voice": TTS_VOICE, "engine": "edge-tts"},
        "hermes": {"url": HERMES_URL, "target": HERMES_TARGET, "reachable": hermes_ok},
        "audio": {
            "input_devices": len(input_devices),
            "sample_rate": SAMPLE_RATE,
        },
        "state": state,
        "ws_clients": len(ws_clients),
    }


@app.post("/start")
async def start_listening():
    """Begin mic capture and voice processing."""
    global listening_active, _listen_thread, state

    if listening_active:
        return {"status": "already_listening"}

    listening_active = True
    loop = asyncio.get_event_loop()
    _listen_thread = threading.Thread(target=voice_capture_loop, args=(loop,), daemon=True)
    _listen_thread.start()

    return {"status": "listening", "target": HERMES_TARGET}


@app.post("/stop")
async def stop_listening():
    """Stop mic capture."""
    global listening_active, state
    listening_active = False
    state = VoiceState.IDLE
    return {"status": "stopped"}


@app.get("/status")
async def get_status():
    """Current voice bridge state."""
    return {
        "state": state,
        "listening": listening_active,
        "target": HERMES_TARGET,
        "model": WHISPER_MODEL,
        "voice": TTS_VOICE,
    }


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """Real-time transcript + response streaming."""
    await ws.accept()
    ws_clients.add(ws)
    log.info("WebSocket client connected (%d total)", len(ws_clients))
    try:
        while True:
            # Keep alive — client can also send commands
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text(json.dumps({"event": "pong"}))
    except WebSocketDisconnect:
        ws_clients.discard(ws)
        log.info("WebSocket client disconnected (%d remaining)", len(ws_clients))


# ─── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=VOICE_PORT, log_level="info")
