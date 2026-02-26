# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Voice Agent SIP Gateway that bridges a Grandstream UCM6302 PBX (telephone system) with Deepgram's Voice Agent API (unified STT + LLM + TTS) through a Janus WebRTC Gateway. Written primarily in Python with an async-first design. All documentation and user-facing strings are in Spanish.

**Current status:** SIP signaling is complete; RTP audio bridge between Janus and Deepgram is not yet implemented.

## Build & Run Commands

```bash
# Build and start all services
docker compose up -d --build

# View all logs
docker compose logs -f

# View only voice-agent logs
docker compose logs -f voice-agent

# View only Janus logs
docker compose logs -f janus-gateway

# Stop all services
docker compose down

# Verify Janus is running
curl http://localhost:8088/janus/info
```

There is no test suite, linter, or CI pipeline configured.

## Architecture

```
Phone ←SIP/RTP→ UCM6302 PBX ←SIP/RTP→ Janus Gateway ←WebSocket→ Voice Agent (Python) ←WSS→ Deepgram
```

Three Docker Compose services:
- **janus-gateway** — Janus WebRTC server with SIP plugin, handles SIP signaling and RTP media with the PBX
- **voice-agent** — Python asyncio service (`voice-agent/main.py`) that orchestrates Janus ↔ Deepgram
- **web-panel** — nginx serving a vanilla JS dashboard (`web/index.html`) at port 8080

### Core Python Modules (in `voice-agent/`)

- **`main.py`** — `VoiceAgentService` orchestrator. Connects to Janus, registers SIP extension on UCM6302, handles call lifecycle (incoming call → accept → create Deepgram session → bridge audio → hangup). Entry point via `asyncio.run(main())`.
- **`janus_sip_client.py`** — `JanusSIPClient`. Async WebSocket client to Janus Gateway. Manages sessions/handles, SIP operations (register, accept, hangup, DTMF), event listener dispatch, keep-alive (25s), SDP handling.
- **`deepgram_agent.py`** — `DeepgramVoiceAgent`. WebSocket client to `wss://agent.deepgram.com/agent`. Sends PCM audio, receives STT transcriptions + LLM text + TTS audio via callbacks. Configurable STT/LLM/TTS models.

### Key Design Patterns

- **Event-driven / Observer pattern:** Both `JanusSIPClient` and `DeepgramVoiceAgent` use callback registration (`on_event`, `on_audio_response`, etc.) to decouple components.
- **Transaction-based RPC:** Janus client tracks requests via transaction IDs for async request/response correlation.
- **One Deepgram session per call:** A new `DeepgramVoiceAgent` instance is created for each incoming call and torn down on hangup.

## Configuration

All config is via `.env` file (loaded by docker-compose and python-dotenv). Key variables:
- `UCM_HOST`, `UCM_PORT`, `SIP_EXTENSION`, `SIP_PASSWORD` — PBX connection
- `JANUS_WS_URL` — overridden to `ws://janus-gateway:8188` inside Docker
- `DEEPGRAM_API_KEY` — required
- `DEEPGRAM_STT_MODEL`, `DEEPGRAM_LLM_PROVIDER`, `DEEPGRAM_LLM_MODEL`, `DEEPGRAM_TTS_MODEL` — AI model selection
- `AGENT_SYSTEM_PROMPT`, `AGENT_GREETING`, `AGENT_LANGUAGE` — agent behavior
- `AUDIO_SAMPLE_RATE` (16000), `AUDIO_ENCODING` (linear16)

## Janus Configuration

Config files in `conf/` are mounted into the Janus container. Key files:
- `janus.jcfg` — NAT settings, RTP port ranges, logging
- `janus.plugin.sip.jcfg` — SIP plugin (codecs, DTMF, registration TTL)
- `janus.transport.websockets.jcfg` — WebSocket API on port 8188

## Incomplete: RTP Audio Bridge

The `_on_deepgram_audio` callback in `main.py` is a stub. Viable approaches documented in README:
1. **aiortc** — Python WebRTC PeerConnection to Janus
2. **GStreamer** — RTP pipeline to Deepgram WebSocket
3. **Janus AudioBridge + RTP forward** — forward audio to local UDP, capture in Python
4. **FFmpeg** — RTP capture piped to Python

## Dependencies

Python packages (in `voice-agent/requirements.txt`): deepgram-sdk, websockets, aiohttp, numpy, soundfile, pydub, python-dotenv, loguru. Python 3.11.
