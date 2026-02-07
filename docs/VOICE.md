# Voice & Phone Integration

SuperClaw supports three voice modes. All routes converge through Hermes Bridge, giving every agent the same communication backbone regardless of how users connect.

```
                        ┌──────────────────┐
    Phone Call ─────────┤  ElevenLabs      ├──── ngrok ───┐
                        │  (STT + TTS)     │              │
                        └──────────────────┘              │
                                                          ▼
                        ┌──────────────────┐       ┌──────────────┐
    Local Mic ──────────┤  Voice Bridge    ├──────▶│   Hermes     │──▶ Agents
                        │  (Whisper + TTS) │       │   Bridge     │
                        └──────────────────┘       │  (port 8787) │
                                                   └──────┬───────┘
                        ┌──────────────────┐              │
    Discord Voice ──────┤  Discord Bridge  ├──────────────┘
                        │  (py-cord + TTS) │
                        └──────────────────┘
```

## Quick Start

```bash
# Install voice dependencies
bash scripts/setup-voice.sh

# Start local voice (talk to agents through your mic)
bridge/voice-venv/bin/python bridge/voice_bridge.py

# Start Discord voice bot
DISCORD_BOT_TOKEN=xxx bridge/voice-venv/bin/python bridge/discord_bridge.py
```

---

## Mode 1: Local Voice

Talk to agents through your computer's microphone and speaker.

**Stack:** faster-whisper (STT) + edge-tts (TTS) + sounddevice (audio I/O)

### Setup

```bash
bash scripts/setup-voice.sh
```

This installs all dependencies in `bridge/voice-venv/` using Python 3.12.

### Run

```bash
# Start the voice bridge (port 8686)
bridge/voice-venv/bin/python bridge/voice_bridge.py

# Start listening
curl -X POST http://localhost:8686/start

# Check status
curl http://localhost:8686/health
```

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `WHISPER_MODEL` | `base` | Model size: base, small, medium, large |
| `WHISPER_DEVICE` | `cuda` | Inference device: cuda or cpu |
| `HERMES_URL` | `http://localhost:8787` | Hermes Bridge address |
| `HERMES_TARGET` | `claude` | Which agent receives voice (claude, coordinator, etc.) |
| `TTS_VOICE` | `en-US-AriaNeural` | Edge-TTS voice (see below for options) |
| `VAD_SENSITIVITY` | `2` | 0-3, higher = more sensitive to speech |
| `VOICE_PORT` | `8686` | API port |

### WebSocket (Dashboard Integration)

Connect to `ws://localhost:8686/ws` to receive real-time events:

```json
{"event": "transcript", "text": "...", "stt_ms": 234}
{"event": "response", "text": "...", "hermes_ms": 1200}
{"event": "state", "state": "listening"}
```

### API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/health` | GET | Component status (Whisper, VAD, TTS, Hermes, audio) |
| `/start` | POST | Begin listening on microphone |
| `/stop` | POST | Stop listening |
| `/status` | GET | Current state (idle/listening/processing/speaking) |
| `/ws` | WebSocket | Real-time transcript + response stream |

---

## Mode 2: Phone (ElevenLabs + Twilio)

Call a phone number and talk to your agents. ElevenLabs handles voice, SuperClaw handles intelligence.

**Stack:** ElevenLabs Agents (STT + TTS) + ngrok (tunnel) + SuperClaw chat/completions

### Prerequisites

- ElevenLabs account: https://elevenlabs.io
- ngrok: https://ngrok.com
- Twilio account (for phone number): https://twilio.com

### Setup

1. **Enable chat/completions in the gateway:**

   Copy `configs/superclaw-gateway.json.example` to `~/.superclaw/superclaw.json` and ensure:
   ```json
   "gateway": {
     "http": {
       "endpoints": {
         "chatCompletions": { "enabled": true }
       }
     }
   }
   ```

2. **Expose gateway via ngrok:**
   ```bash
   ngrok http 18789
   ```
   Copy the `https://xxx.ngrok-free.app` URL.

3. **Create ElevenLabs Agent:**

   Follow the steps in `configs/elevenlabs-agent.json` or use the ElevenLabs dashboard:
   - Create Agent → Custom LLM
   - URL: `https://YOUR_NGROK_URL/v1/chat/completions`
   - Add your SuperClaw gateway token as auth header

4. **Connect phone number (optional):**
   - Buy a Twilio number
   - Link in ElevenLabs Agent settings
   - Call the number

### How It Works

```
Phone → Twilio → ElevenLabs (STT/TTS) → ngrok → SuperClaw /v1/chat/completions → Agent
```

ElevenLabs handles all voice processing. SuperClaw handles tools, memory, and agent routing. The `/v1/chat/completions` endpoint speaks standard OpenAI protocol — ElevenLabs treats your agent network as a custom LLM.

---

## Mode 3: Discord Voice

A bot joins your Discord voice channel, listens to conversation, and responds with voice + text.

**Stack:** py-cord (Discord API) + faster-whisper (STT) + edge-tts (TTS) + Hermes Bridge

### Prerequisites

1. **Create Discord Bot:**
   - Go to https://discord.com/developers/applications
   - Click "New Application" → name it (e.g., "SuperClaw Voice")
   - Go to **Bot** tab → click "Add Bot"
   - Enable **Privileged Gateway Intents**: Message Content Intent
   - Go to **OAuth2 → URL Generator**:
     - Scopes: `bot`, `applications.commands`
     - Bot Permissions: Connect, Speak, Use Voice Activity, Send Messages, Read Message History
   - Copy the generated URL → open in browser → invite bot to your server
   - Copy the **Bot Token**

2. **Install voice dependencies:**
   ```bash
   bash scripts/setup-voice.sh
   ```

### Run

```bash
DISCORD_BOT_TOKEN=your_token_here bridge/voice-venv/bin/python bridge/discord_bridge.py
```

### Usage (in Discord)

| Command | Description |
|---------|-------------|
| `/join` | Bot joins your voice channel, starts listening |
| `/leave` | Bot leaves voice channel |
| `/target <agent>` | Set which agent handles voice (coordinator, developer, etc.) |
| `/voice-status` | Show current configuration |
| `@BotName <message>` | Text chat — routes through Hermes (works in any channel) |

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `DISCORD_BOT_TOKEN` | (required) | Bot token from Developer Portal |
| `WHISPER_MODEL` | `base` | Whisper model size |
| `WHISPER_DEVICE` | `cuda` | cuda or cpu |
| `HERMES_URL` | `http://localhost:8787` | Hermes Bridge address |
| `DEFAULT_AGENT` | `coordinator` | Default agent for voice routing |
| `TTS_VOICE` | `en-US-GuyNeural` | Edge-TTS voice name |

### Per-Channel Agent Routing

Use `/target` to assign different agents to different voice channels:

```
#ops-voice    → /target coordinator
#dev-voice    → /target developer
#research     → /target researcher
```

---

## Discord Text (Native SuperClaw)

SuperClaw has built-in Discord text channel support — no custom code needed.

```bash
superclaw onboard
# Select "Discord" when prompted
# Paste your bot token
```

This gives you text-based agent interaction in Discord channels. The Discord Voice Bridge (above) adds voice on top.

---

## TTS Voice Options

Edge-TTS provides Microsoft Neural Voices for free. Some good options:

| Voice | Language | Style |
|-------|----------|-------|
| `en-US-AriaNeural` | English (US) | Warm, conversational |
| `en-US-GuyNeural` | English (US) | Calm, professional |
| `en-US-JennyNeural` | English (US) | Friendly, clear |
| `en-US-DavisNeural` | English (US) | Authoritative |
| `en-GB-SoniaNeural` | English (UK) | British accent |
| `en-AU-NatashaNeural` | English (AU) | Australian accent |

List all available voices:
```bash
bridge/voice-venv/bin/python -m edge_tts --list-voices | grep en-
```

---

## Troubleshooting

### No audio input detected
- Check mic connection: `arecord -l`
- Set default source: `pactl set-default-source alsa_input.pci-0000_00_1f.3.analog-stereo`
- The voice bridge defaults to PipeWire/PulseAudio's default input device

### Whisper is slow
- Ensure CUDA is being used: check `WHISPER_DEVICE=cuda` in health endpoint
- If CUDA fails, you may need: `pip install torch --index-url https://download.pytorch.org/whl/cu126`
- Use `base` model for speed, `small` for accuracy

### VRAM issues
- faster-whisper `base` needs ~200MB, `small` ~500MB
- If Ollama is using too much VRAM, unload unused models: `ollama rm model_name`
- Check usage: `nvidia-smi`

### Discord bot not responding
- Verify bot token is correct
- Ensure Message Content Intent is enabled in Developer Portal
- Check bot has correct permissions in the server
- Run with `LOGLEVEL=DEBUG` for verbose output

### Hermes Bridge unreachable
- Verify Hermes is running: `curl http://localhost:8787/api/v1/health`
- Check the voice bridge and discord bridge are registered in `hermes_policy.json`

---

## The Ship

Discord becomes the convergence point — the "bridge of the ship" — where all communication modes meet:

```
Phone callers ─────┐
Local voice ───────┤
Discord voice ─────┤──▶ Hermes Bridge ──▶ Agent Network
Discord text ──────┤
n8n workflows ─────┤
Dashboard chat ────┤
Remote swarms ─────┘
```

Every input channel routes through Hermes with the same policy enforcement, rate limiting, audit logging, and agent routing. Whether someone calls from a phone, speaks into a local mic, types in Discord, or triggers an n8n workflow — the agents see the same message format and respond the same way.

To bridge remote machines and swarms:
1. Run Hermes Bridge accessible on your network (it already binds 0.0.0.0)
2. Point remote agents' `HERMES_URL` to your machine's IP
3. All agents from all machines appear in the same Dashboard and task queue
