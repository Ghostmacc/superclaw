#!/usr/bin/env python3
"""
SuperClaw Discord Voice Bridge — Voice Bot for Discord Channels

Joins Discord voice channels, listens to users via py-cord's recording API,
transcribes with faster-whisper, routes through Hermes Bridge, and speaks
responses back using edge-tts.

Architecture:
  Discord Voice → py-cord start_recording → faster-whisper → Hermes → edge-tts → FFmpegPCMAudio

Usage:
  DISCORD_BOT_TOKEN=xxx python discord_bridge.py

Environment:
  DISCORD_BOT_TOKEN   Required — from Discord Developer Portal
  WHISPER_MODEL       base|small|medium|large  (default: base)
  WHISPER_DEVICE      cuda|cpu                 (default: cuda)
  HERMES_URL          Hermes Bridge URL        (default: http://localhost:8787)
  DEFAULT_AGENT       Default agent target     (default: coordinator)
  TTS_VOICE           edge-tts voice name      (default: en-US-GuyNeural)
"""

import asyncio
import io
import logging
import os
import tempfile
import time
import wave

import aiohttp
import discord
import edge_tts
import numpy as np
from discord.ext import commands
from faster_whisper import WhisperModel

# ─── Configuration ───────────────────────────────────────────────────────────

DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cuda")
HERMES_URL = os.getenv("HERMES_URL", "http://localhost:8787")
DEFAULT_AGENT = os.getenv("DEFAULT_AGENT", "coordinator")
TTS_VOICE = os.getenv("TTS_VOICE", "en-US-GuyNeural")

SAMPLE_RATE = 48000       # Discord sends 48kHz
WHISPER_RATE = 16000      # Whisper expects 16kHz
CHANNELS = 2              # Discord sends stereo
SILENCE_THRESHOLD_S = 1.0 # Seconds of silence before processing

log = logging.getLogger("discord_bridge")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

# ─── Globals ─────────────────────────────────────────────────────────────────

whisper_model: WhisperModel = None
channel_targets: dict[int, str] = {}  # channel_id → agent name


# ─── Whisper STT ─────────────────────────────────────────────────────────────

def init_whisper():
    """Load faster-whisper model."""
    global whisper_model
    compute = "float16" if WHISPER_DEVICE == "cuda" else "int8"
    log.info("Loading Whisper '%s' on %s (%s)...", WHISPER_MODEL, WHISPER_DEVICE, compute)
    try:
        whisper_model = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=compute)
    except Exception as e:
        log.warning("CUDA failed (%s), falling back to CPU", e)
        whisper_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    log.info("Whisper model loaded")


def transcribe(audio_np: np.ndarray) -> str:
    """Transcribe audio numpy array to text."""
    if whisper_model is None:
        return ""
    segments, _ = whisper_model.transcribe(
        audio_np, beam_size=5, language="en", vad_filter=True,
    )
    return " ".join(seg.text.strip() for seg in segments).strip()


# ─── Audio Helpers ───────────────────────────────────────────────────────────

def resample_48k_to_16k(audio_48k: np.ndarray) -> np.ndarray:
    """Downsample from 48kHz stereo to 16kHz mono for Whisper."""
    # Stereo to mono
    if audio_48k.ndim == 2:
        mono = audio_48k.mean(axis=1)
    else:
        mono = audio_48k

    # Naive downsample: take every 3rd sample (48000/16000 = 3).
    # Adequate for speech; swap for scipy.signal.resample if quality matters.
    resampled = mono[::3]
    return resampled.astype(np.float32)


def wav_bytes_to_numpy(wav_bytes: bytes) -> np.ndarray:
    """Convert WAV bytes to numpy float32 array."""
    with io.BytesIO(wav_bytes) as buf:
        with wave.open(buf, "rb") as wf:
            frames = wf.readframes(wf.getnframes())
            dtype = np.int16 if wf.getsampwidth() == 2 else np.int32
            audio = np.frombuffer(frames, dtype=dtype).astype(np.float32)
            audio /= np.iinfo(dtype).max
            if wf.getnchannels() == 2:
                audio = audio.reshape(-1, 2)
            return audio


# ─── Hermes Client ───────────────────────────────────────────────────────────

async def send_to_hermes(text: str, agent: str) -> str:
    """Send text to Hermes Bridge and get response."""
    endpoint = f"{HERMES_URL}/api/v1/claude/ask" if agent == "claude" \
        else f"{HERMES_URL}/api/v1/agent/ask"

    payload = {
        "caller_id": "discord_bridge",
        "message": text,
        "priority": "normal",
    }
    if agent != "claude":
        payload["target_agent"] = agent

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                endpoint, json=payload,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("response", data.get("message", str(data)))
                else:
                    return f"Hermes error: {resp.status}"
    except Exception as e:
        log.error("Hermes error: %s", e)
        return "I couldn't reach the agent network."


# ─── TTS ─────────────────────────────────────────────────────────────────────

async def synthesize_to_file(text: str) -> str:
    """Generate TTS audio and save to temp file. Returns file path."""
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp.close()

    communicate = edge_tts.Communicate(text, TTS_VOICE)
    await communicate.save(tmp.name)
    return tmp.name


# ─── Custom Audio Sink ───────────────────────────────────────────────────────

class UserAudioSink(discord.sinks.WaveSink):
    """Collects per-user WAV audio from Discord voice."""
    pass


# ─── Bot Setup ───────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    log.info("Discord bot online: %s (ID: %s)", bot.user.name, bot.user.id)
    log.info("In %d server(s)", len(bot.guilds))


# ─── Slash Commands ──────────────────────────────────────────────────────────

@bot.slash_command(name="join", description="Join your voice channel and start listening")
async def join_voice(ctx: discord.ApplicationContext):
    """Join the user's voice channel and begin recording."""
    if not ctx.author.voice:
        await ctx.respond("You need to be in a voice channel.", ephemeral=True)
        return

    channel = ctx.author.voice.channel
    vc = ctx.voice_client

    if vc and vc.is_connected():
        await vc.move_to(channel)
    else:
        vc = await channel.connect()

    agent = channel_targets.get(channel.id, DEFAULT_AGENT)
    await ctx.respond(f"Joined **{channel.name}** — routing voice to **{agent}**")
    log.info("Joined voice channel: %s (agent: %s)", channel.name, agent)

    # Start recording
    vc.start_recording(
        UserAudioSink(),
        finished_callback,
        ctx,
    )


async def finished_callback(sink: UserAudioSink, ctx: discord.ApplicationContext):
    """Called when recording stops. Process each user's audio."""
    vc = ctx.voice_client
    channel_id = vc.channel.id if vc and vc.channel else 0
    agent = channel_targets.get(channel_id, DEFAULT_AGENT)

    for user_id, audio_data in sink.audio_data.items():
        user = ctx.guild.get_member(user_id)
        username = user.display_name if user else f"User {user_id}"

        # Convert recorded WAV to numpy
        audio_data.file.seek(0)
        wav_bytes = audio_data.file.read()

        if len(wav_bytes) < 1000:
            continue  # Too short

        try:
            audio_np = wav_bytes_to_numpy(wav_bytes)
            audio_16k = resample_48k_to_16k(audio_np)

            # Skip if too quiet or too short
            if len(audio_16k) < WHISPER_RATE * 0.5:
                continue

            # Transcribe
            start = time.monotonic()
            text = transcribe(audio_16k)
            stt_ms = (time.monotonic() - start) * 1000

            if not text or len(text.strip()) < 2:
                continue

            log.info("[%s] %s (%.0fms STT)", username, text, stt_ms)

            # Post transcript to text channel
            text_channel = ctx.channel
            await text_channel.send(f"**{username}:** {text}")

            # Send to Hermes
            start = time.monotonic()
            response = await send_to_hermes(text, agent)
            hermes_ms = (time.monotonic() - start) * 1000

            log.info("[%s → %s] %s (%.0fms)", agent, username, response[:100], hermes_ms)

            # Post response to text channel
            await text_channel.send(f"**{agent}:** {response}")

            # TTS + voice playback
            if vc and vc.is_connected():
                tts_file = await synthesize_to_file(response)
                source = discord.FFmpegPCMAudio(tts_file)
                if not vc.is_playing():
                    vc.play(source, after=lambda e: _cleanup_tts(tts_file, e))
                else:
                    # Queue — wait for current playback to finish
                    while vc.is_playing():
                        await asyncio.sleep(0.5)
                    vc.play(source, after=lambda e: _cleanup_tts(tts_file, e))

        except Exception as e:
            log.error("Error processing audio from %s: %s", username, e)

    # Restart recording for continuous conversation
    if vc and vc.is_connected():
        try:
            vc.start_recording(
                UserAudioSink(),
                finished_callback,
                ctx,
            )
        except Exception as e:
            log.warning("Could not restart recording: %s", e)


def _cleanup_tts(filepath: str, error):
    """Clean up temp TTS file after playback."""
    if error:
        log.error("Playback error: %s", error)
    try:
        os.unlink(filepath)
    except OSError:
        pass


@bot.slash_command(name="leave", description="Leave voice channel")
async def leave_voice(ctx: discord.ApplicationContext):
    """Leave the voice channel."""
    vc = ctx.voice_client
    if vc and vc.is_connected():
        if vc.recording:
            vc.stop_recording()
        await vc.disconnect()
        await ctx.respond("Left voice channel.")
        log.info("Left voice channel")
    else:
        await ctx.respond("Not in a voice channel.", ephemeral=True)


@bot.slash_command(name="target", description="Set which agent handles voice in this channel")
async def set_target(
    ctx: discord.ApplicationContext,
    agent: discord.Option(
        str,
        description="Agent name (coordinator, developer, researcher, monitor, analyst, claude)",
        required=True,
    ),
):
    """Set the agent target for this voice channel."""
    if not ctx.author.voice:
        await ctx.respond("Join a voice channel first.", ephemeral=True)
        return

    channel_targets[ctx.author.voice.channel.id] = agent
    await ctx.respond(f"Voice in **{ctx.author.voice.channel.name}** now routes to **{agent}**")
    log.info("Channel %s target set to: %s", ctx.author.voice.channel.name, agent)


@bot.slash_command(name="voice-status", description="Show voice bridge status")
async def voice_status(ctx: discord.ApplicationContext):
    """Show current voice bridge configuration."""
    vc = ctx.voice_client
    connected = vc and vc.is_connected()
    channel_name = vc.channel.name if connected else "none"
    agent = channel_targets.get(vc.channel.id, DEFAULT_AGENT) if connected else DEFAULT_AGENT

    # Check Hermes
    hermes_ok = False
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{HERMES_URL}/api/v1/health",
                timeout=aiohttp.ClientTimeout(total=3),
            ) as resp:
                hermes_ok = resp.status == 200
    except Exception:
        pass

    embed = discord.Embed(title="Voice Bridge Status", color=0x00ff88 if connected else 0xff4444)
    embed.add_field(name="Voice Channel", value=channel_name, inline=True)
    embed.add_field(name="Agent Target", value=agent, inline=True)
    embed.add_field(name="Recording", value="Yes" if (connected and vc.recording) else "No", inline=True)
    embed.add_field(name="Whisper Model", value=f"{WHISPER_MODEL} ({WHISPER_DEVICE})", inline=True)
    embed.add_field(name="TTS Voice", value=TTS_VOICE, inline=True)
    embed.add_field(name="Hermes", value="Online" if hermes_ok else "Offline", inline=True)

    await ctx.respond(embed=embed)


# ─── Text Channel Fallback ───────────────────────────────────────────────────

@bot.event
async def on_message(message: discord.Message):
    """Handle text messages — route through Hermes if bot is mentioned."""
    if message.author == bot.user:
        return

    if bot.user.mentioned_in(message):
        # Strip the mention from the message
        text = message.content.replace(f"<@{bot.user.id}>", "").strip()
        if not text:
            await message.reply("Say something after mentioning me.")
            return

        agent = DEFAULT_AGENT
        async with message.channel.typing():
            response = await send_to_hermes(text, agent)

        await message.reply(f"**{agent}:** {response}")

    await bot.process_commands(message)


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    if not DISCORD_TOKEN:
        print("ERROR: Set DISCORD_BOT_TOKEN environment variable")
        print("  1. Create bot at https://discord.com/developers/applications")
        print("  2. Copy bot token")
        print("  3. Run: DISCORD_BOT_TOKEN=xxx python discord_bridge.py")
        return

    init_whisper()
    log.info("Starting Discord Voice Bridge...")
    log.info("  Agent target: %s", DEFAULT_AGENT)
    log.info("  Whisper: %s on %s", WHISPER_MODEL, WHISPER_DEVICE)
    log.info("  TTS: %s", TTS_VOICE)
    log.info("  Hermes: %s", HERMES_URL)

    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
