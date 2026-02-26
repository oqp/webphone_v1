"""
═══════════════════════════════════════════════════════════════
deepgram_agent.py - Cliente del Deepgram Voice Agent API
═══════════════════════════════════════════════════════════════

Deepgram Voice Agent API es un WebSocket unificado que maneja:
  - STT (Speech-to-Text): modelo nova-3
  - LLM (Razonamiento): OpenAI, Anthropic, o custom
  - TTS (Text-to-Speech): modelos aura-2

Flujo:
  Audio PCM del usuario → [Deepgram WS] → Audio PCM de respuesta
                            ↕ eventos JSON (transcripción, estado, etc.)

Esto simplifica enormemente la arquitectura: en lugar de
orquestar STT + LLM + TTS por separado, un solo WebSocket
maneja todo el pipeline voice-to-voice.
"""

import os
import json
import asyncio
from typing import Callable, Optional
from loguru import logger
import websockets


# URL del Voice Agent API v1 de Deepgram
DEEPGRAM_AGENT_WS = "wss://agent.deepgram.com/agent"


class DeepgramVoiceAgent:
    """
    Cliente WebSocket para el Deepgram Voice Agent API.

    Envía audio PCM del llamante y recibe audio PCM de respuesta,
    junto con eventos de transcripción y estado de la conversación.
    """

    def __init__(self):
        self.api_key = os.getenv("DEEPGRAM_API_KEY", "")
        self.language = os.getenv("AGENT_LANGUAGE", "es")
        self.stt_model = os.getenv("DEEPGRAM_STT_MODEL", "nova-3")
        self.llm_provider = os.getenv("DEEPGRAM_LLM_PROVIDER", "open_ai")
        self.llm_model = os.getenv("DEEPGRAM_LLM_MODEL", "gpt-4o-mini")
        self.tts_model = os.getenv("DEEPGRAM_TTS_MODEL", "aura-2-luna-es")
        self.sample_rate = int(os.getenv("AUDIO_SAMPLE_RATE", "16000"))
        self.encoding = os.getenv("AUDIO_ENCODING", "linear16")
        self.system_prompt = os.getenv(
            "AGENT_SYSTEM_PROMPT",
            "Eres un asistente virtual de atención telefónica. "
            "Responde en español de forma breve, clara y profesional."
        )
        self.greeting = os.getenv(
            "AGENT_GREETING",
            "Hola, bienvenido. Soy el asistente virtual. ¿En qué puedo ayudarle?"
        )

        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._listener_task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None

        # Callbacks
        self._on_audio_response: Optional[Callable] = None
        self._on_transcript: Optional[Callable] = None
        self._on_agent_text: Optional[Callable] = None
        self._on_agent_thinking: Optional[Callable] = None
        self._on_user_started_speaking: Optional[Callable] = None
        self._on_agent_audio_done: Optional[Callable] = None
        self._on_error: Optional[Callable] = None

    # ── CONEXIÓN ─────────────────────────────────────────

    async def connect(self):
        """Conectar al Deepgram Voice Agent API."""
        logger.info("Conectando a Deepgram Voice Agent API...")

        headers = {
            "Authorization": f"Token {self.api_key}",
        }

        self.ws = await websockets.connect(
            DEEPGRAM_AGENT_WS,
            additional_headers=headers,
            ping_interval=5,
            ping_timeout=10,
        )

        logger.info("WebSocket Deepgram conectado")

        # Enviar configuración del agente
        await self._send_settings()

        # Iniciar listener de eventos
        self._listener_task = asyncio.create_task(self._event_listener())
        self._keepalive_task = asyncio.create_task(self._keepalive())

    async def _send_settings(self):
        """Enviar mensaje Settings para configurar el agente."""
        settings = {
            "type": "Settings",
            "audio": {
                "input": {
                    "encoding": self.encoding,
                    "sample_rate": self.sample_rate,
                },
                "output": {
                    "encoding": self.encoding,
                    "sample_rate": self.sample_rate,
                    "container": "none",    # raw PCM sin headers
                },
            },
            "agent": {
                "language": self.language,
                "listen": {
                    "provider": {
                        "type": "deepgram",
                        "model": self.stt_model,
                        "smart_format": True,
                    }
                },
                "think": {
                    "provider": {
                        "type": self.llm_provider,
                        "model": self.llm_model,
                        "temperature": 0.7,
                    },
                    "prompt": self.system_prompt,
                },
                "speak": {
                    "provider": {
                        "type": "deepgram",
                        "model": self.tts_model,
                    }
                },
                "greeting": self.greeting,
            },
        }

        await self.ws.send(json.dumps(settings))
        logger.info(f"Settings enviados (STT={self.stt_model}, LLM={self.llm_model}, TTS={self.tts_model})")

    # ── ENVÍO DE AUDIO ───────────────────────────────────

    async def send_audio(self, audio_data: bytes):
        """
        Enviar audio PCM del llamante a Deepgram.

        Args:
            audio_data: Audio PCM linear16, mono, al sample_rate configurado
        """
        if self.ws and self.ws.open:
            await self.ws.send(audio_data)

    # ── CALLBACKS ────────────────────────────────────────

    def on_audio_response(self, callback: Callable):
        """Registrar callback para recibir audio PCM de respuesta del agente."""
        self._on_audio_response = callback

    def on_transcript(self, callback: Callable):
        """Callback cuando se transcribe lo que dijo el usuario."""
        self._on_transcript = callback

    def on_agent_text(self, callback: Callable):
        """Callback con el texto de la respuesta del agente."""
        self._on_agent_text = callback

    def on_agent_thinking(self, callback: Callable):
        """Callback cuando el agente está procesando."""
        self._on_agent_thinking = callback

    def on_user_started_speaking(self, callback: Callable):
        """Callback cuando se detecta que el usuario empezó a hablar."""
        self._on_user_started_speaking = callback

    def on_agent_audio_done(self, callback: Callable):
        """Callback cuando el agente terminó de hablar."""
        self._on_agent_audio_done = callback

    def on_error(self, callback: Callable):
        """Callback para errores."""
        self._on_error = callback

    # ── CONTROL ──────────────────────────────────────────

    async def inject_user_message(self, text: str):
        """Inyectar un mensaje de texto como si lo hubiera dicho el usuario."""
        msg = {"type": "InjectUserMessage", "content": text}
        await self.ws.send(json.dumps(msg))

    async def inject_agent_message(self, text: str):
        """Inyectar un mensaje del agente (se sintetiza a audio)."""
        msg = {"type": "InjectAgentMessage", "message": text}
        await self.ws.send(json.dumps(msg))

    async def update_prompt(self, new_prompt: str):
        """Actualizar el prompt del sistema en tiempo real."""
        msg = {"type": "UpdatePrompt", "prompt": new_prompt}
        await self.ws.send(json.dumps(msg))

    # ── LISTENER ─────────────────────────────────────────

    async def _event_listener(self):
        """Escuchar eventos y audio del WebSocket de Deepgram."""
        try:
            async for message in self.ws:
                # Audio binario (respuesta TTS del agente)
                if isinstance(message, bytes):
                    if self._on_audio_response:
                        if asyncio.iscoroutinefunction(self._on_audio_response):
                            await self._on_audio_response(message)
                        else:
                            self._on_audio_response(message)
                    continue

                # Mensajes JSON de control/eventos
                data = json.loads(message)
                msg_type = data.get("type", "")

                if msg_type == "Welcome":
                    logger.info(f"Deepgram Welcome: session={data.get('session_id')}")

                elif msg_type == "SettingsApplied":
                    logger.success("Deepgram: Settings aplicados correctamente")

                elif msg_type == "ConversationText":
                    role = data.get("role", "")
                    content = data.get("content", "")
                    logger.info(f"[{role}] {content}")

                    if role == "user" and self._on_transcript:
                        cb = self._on_transcript
                        await cb(content) if asyncio.iscoroutinefunction(cb) else cb(content)

                    elif role == "assistant" and self._on_agent_text:
                        cb = self._on_agent_text
                        await cb(content) if asyncio.iscoroutinefunction(cb) else cb(content)

                elif msg_type == "UserStartedSpeaking":
                    logger.debug("Usuario empezó a hablar")
                    if self._on_user_started_speaking:
                        cb = self._on_user_started_speaking
                        await cb() if asyncio.iscoroutinefunction(cb) else cb()

                elif msg_type == "AgentThinking":
                    logger.debug("Agente procesando...")
                    if self._on_agent_thinking:
                        cb = self._on_agent_thinking
                        await cb() if asyncio.iscoroutinefunction(cb) else cb()

                elif msg_type == "AgentStartedSpeaking":
                    logger.debug("Agente empezó a hablar")

                elif msg_type == "AgentAudioDone":
                    logger.debug("Agente terminó de hablar")
                    if self._on_agent_audio_done:
                        cb = self._on_agent_audio_done
                        await cb() if asyncio.iscoroutinefunction(cb) else cb()

                elif msg_type == "Error":
                    desc = data.get("description", "error desconocido")
                    code = data.get("code", "")
                    logger.error(f"Deepgram error [{code}]: {desc}")
                    if self._on_error:
                        cb = self._on_error
                        await cb(data) if asyncio.iscoroutinefunction(cb) else cb(data)

                elif msg_type == "Warning":
                    logger.warning(f"Deepgram warning: {data.get('description')}")

                else:
                    logger.debug(f"Deepgram evento: {msg_type}")

        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"WebSocket Deepgram cerrado: {e}")
        except Exception as e:
            logger.error(f"Error en listener Deepgram: {e}")

    async def _keepalive(self):
        """Enviar keep-alive periódico."""
        while True:
            try:
                await asyncio.sleep(5)
                if self.ws and self.ws.open:
                    await self.ws.send(json.dumps({"type": "KeepAlive"}))
            except Exception:
                break

    # ── DESCONEXIÓN ──────────────────────────────────────

    async def disconnect(self):
        """Cerrar conexión con Deepgram."""
        if self._keepalive_task:
            self._keepalive_task.cancel()
        if self._listener_task:
            self._listener_task.cancel()
        if self.ws:
            await self.ws.close()
        logger.info("Desconectado de Deepgram")
