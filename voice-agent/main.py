"""
═══════════════════════════════════════════════════════════════
main.py - Orquestador: Janus SIP ↔ Deepgram Voice Agent
═══════════════════════════════════════════════════════════════

Arquitectura del flujo de audio:

  ┌──────────┐   SIP/RTP    ┌──────────┐   WebSocket   ┌──────────┐   WebSocket   ┌──────────┐
  │ Teléfono │ ◄──────────► │  UCM6302 │ ◄───────────► │  Janus   │ ◄───────────► │  Voice   │
  │   SIP    │              │          │               │ Gateway  │               │  Agent   │
  └──────────┘              └──────────┘               └──────────┘               │ (Python) │
                                                                                  └────┬─────┘
                                                                                       │ WS
                                                                                  ┌────┴─────┐
                                                                                  │ Deepgram │
                                                                                  │ Voice    │
                                                                                  │ Agent    │
                                                                                  │ API      │
                                                                                  │ STT+LLM  │
                                                                                  │ +TTS     │
                                                                                  └──────────┘

Pipeline por llamada:
  1. Llamada entrante llega a la UCM6302
  2. UCM6302 rutea a la extensión del agente (Janus)
  3. Janus negocia WebRTC y establece flujo RTP con UCM
  4. Este script abre conexión con Deepgram Voice Agent API
  5. Audio del llamante → Deepgram (STT → LLM → TTS)
  6. Audio de respuesta ← Deepgram → Janus → UCM → Teléfono

NOTA: En esta versión el audio se maneja a nivel señalización.
Para producción completa necesitarás integrar captura RTP real
(via GStreamer, ffmpeg, o aiortc) para extraer/inyectar audio
del PeerConnection de Janus.
"""

import os
import asyncio
import signal
from dotenv import load_dotenv
from loguru import logger

from janus_sip_client import JanusSIPClient
from deepgram_agent import DeepgramVoiceAgent

load_dotenv()

# ── CONFIGURACIÓN ────────────────────────────────────────

UCM_HOST = os.getenv("UCM_HOST", "192.168.1.100")
UCM_PORT = int(os.getenv("UCM_PORT", "5060"))
SIP_EXTENSION = os.getenv("SIP_EXTENSION", "9000")
SIP_PASSWORD = os.getenv("SIP_PASSWORD", "")
SIP_DISPLAY_NAME = os.getenv("SIP_DISPLAY_NAME", "Agente IA")
JANUS_WS_URL = os.getenv("JANUS_WS_URL", "ws://127.0.0.1:8188")
JANUS_API_SECRET = os.getenv("JANUS_API_SECRET", "")


class VoiceAgentService:
    """
    Servicio principal que conecta Janus (SIP) con Deepgram (Voice AI).

    Gestiona el ciclo de vida completo:
      - Registro SIP en la UCM6302 via Janus
      - Espera de llamadas entrantes
      - Creación de sesión Deepgram por cada llamada
      - Bridge de audio bidireccional
    """

    def __init__(self):
        self.janus = JanusSIPClient(JANUS_WS_URL, JANUS_API_SECRET)
        self.deepgram: DeepgramVoiceAgent = None
        self.running = True
        self.in_call = False

    async def start(self):
        """Iniciar el servicio completo."""
        logger.info("=" * 60)
        logger.info("  VOICE AGENT: Janus + UCM6302 + Deepgram")
        logger.info("=" * 60)

        # 1. Conectar a Janus Gateway
        logger.info("[1/3] Conectando a Janus Gateway...")
        await self.janus.connect()
        await self.janus.attach_sip_plugin()

        # 2. Registrar handlers de eventos SIP
        logger.info("[2/3] Configurando handlers de eventos...")
        self._setup_sip_handlers()

        # 3. Registrar extensión en la UCM6302
        logger.info(f"[3/3] Registrando ext {SIP_EXTENSION} en UCM6302 ({UCM_HOST})...")
        await self.janus.register(
            ucm_host=UCM_HOST,
            ucm_port=UCM_PORT,
            extension=SIP_EXTENSION,
            password=SIP_PASSWORD,
            display_name=SIP_DISPLAY_NAME,
        )

        # Mantener el servicio activo
        logger.info("Esperando llamadas...")
        try:
            while self.running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    def _setup_sip_handlers(self):
        """Configurar handlers para eventos SIP de Janus."""
        self.janus.on_event("registered", self._on_registered)
        self.janus.on_event("registration_failed", self._on_registration_failed)
        self.janus.on_event("incomingcall", self._on_incoming_call)
        self.janus.on_event("accepted", self._on_call_accepted)
        self.janus.on_event("hangup", self._on_hangup)
        self.janus.on_event("calling", self._on_calling)
        self.janus.on_event("ringing", self._on_ringing)

    # ── HANDLERS SIP ─────────────────────────────────────

    async def _on_registered(self, data):
        """Registro exitoso en la UCM6302."""
        pd = data.get("plugindata", {}).get("data", {})
        username = pd.get("result", {}).get("username", SIP_EXTENSION)
        logger.success(f"✅ Registrado como {username} en la UCM6302")
        logger.info(f"   Listo para recibir llamadas en ext {SIP_EXTENSION}")

    async def _on_registration_failed(self, data):
        """Error en el registro SIP."""
        pd = data.get("plugindata", {}).get("data", {})
        result = pd.get("result", {})
        logger.error(f"❌ Registro fallido: {result.get('code')} - {result.get('reason')}")
        logger.error("   Verifica: extensión, contraseña, IP de la UCM, puertos")

    async def _on_incoming_call(self, data):
        """
        Llamada entrante recibida.

        Flujo:
          1. Extraer info del llamante y SDP offer
          2. Aceptar la llamada en Janus
          3. Abrir sesión Deepgram Voice Agent
          4. Iniciar bridge de audio
        """
        pd = data.get("plugindata", {}).get("data", {})
        result = pd.get("result", {})
        caller = result.get("username", result.get("displayname", "desconocido"))
        jsep = data.get("jsep")  # SDP offer del llamante

        logger.info(f"📞 Llamada entrante de: {caller}")

        if self.in_call:
            logger.warning("Ya hay una llamada activa, rechazando...")
            await self.janus.hangup()
            return

        self.in_call = True

        try:
            # Aceptar la llamada en Janus (envía SDP answer)
            logger.info("Aceptando llamada...")
            await self.janus.accept_call()

            # Iniciar sesión Deepgram Voice Agent para esta llamada
            logger.info("Iniciando sesión Deepgram Voice Agent...")
            self.deepgram = DeepgramVoiceAgent()

            # Configurar callbacks de Deepgram
            self._setup_deepgram_callbacks()

            # Conectar a Deepgram
            await self.deepgram.connect()

            logger.success(f"✅ Llamada activa con {caller} - Deepgram conectado")
            logger.info("   Audio: Llamante → Janus → [bridge] → Deepgram → [bridge] → Janus → Llamante")
            logger.info("")
            logger.info("   ⚠️  NOTA: Para bridge de audio RTP real, integrar GStreamer o aiortc")
            logger.info("   Esta versión maneja señalización. Ver README para integración completa.")

        except Exception as e:
            logger.error(f"Error aceptando llamada: {e}")
            self.in_call = False

    async def _on_call_accepted(self, data):
        """Llamada aceptada (saliente)."""
        logger.info("Llamada aceptada")

    async def _on_calling(self, data):
        """Llamada saliente en progreso."""
        logger.info("Llamando...")

    async def _on_ringing(self, data):
        """Destino timbrando."""
        logger.info("Timbrando...")

    async def _on_hangup(self, data):
        """Llamada finalizada."""
        reason = "desconocido"
        if isinstance(data, dict):
            pd = data.get("plugindata", {}).get("data", {})
            reason = pd.get("result", {}).get("reason", data.get("reason", "desconocido"))

        logger.info(f"📴 Llamada finalizada: {reason}")

        # Cerrar sesión Deepgram
        if self.deepgram:
            await self.deepgram.disconnect()
            self.deepgram = None

        self.in_call = False
        logger.info("Esperando nueva llamada...")

    # ── CALLBACKS DEEPGRAM ───────────────────────────────

    def _setup_deepgram_callbacks(self):
        """Configurar callbacks para eventos de Deepgram."""

        # Audio de respuesta del agente → enviar a Janus → llamante
        self.deepgram.on_audio_response(self._on_deepgram_audio)

        # Transcripción de lo que dijo el usuario
        self.deepgram.on_transcript(self._on_user_transcript)

        # Texto de la respuesta del agente
        self.deepgram.on_agent_text(self._on_agent_response_text)

        # Agente terminó de hablar
        self.deepgram.on_agent_audio_done(self._on_agent_done)

        # Errores
        self.deepgram.on_error(self._on_deepgram_error)

    async def _on_deepgram_audio(self, audio_data: bytes):
        """
        Audio PCM de respuesta del agente recibido de Deepgram.

        TODO: En producción, este audio debe inyectarse en el
        flujo RTP de Janus hacia el llamante. Esto requiere:
          - Capturar el track de audio del PeerConnection
          - Convertir PCM al codec negociado (PCMU/PCMA/opus)
          - Enviar como paquetes RTP via el data channel o track
        """
        # logger.debug(f"Audio de Deepgram: {len(audio_data)} bytes")
        pass  # Integrar con el pipeline RTP de Janus

    async def _on_user_transcript(self, text: str):
        """El usuario dijo algo (transcripción de Deepgram)."""
        logger.info(f"  👤 Usuario: {text}")

    async def _on_agent_response_text(self, text: str):
        """El agente respondió (texto generado por LLM)."""
        logger.info(f"  🤖 Agente: {text}")

    async def _on_agent_done(self):
        """El agente terminó de hablar."""
        logger.debug("  🤖 [fin de respuesta]")

    async def _on_deepgram_error(self, error_data: dict):
        """Error de Deepgram."""
        logger.error(f"Error Deepgram: {error_data}")

    # ── SHUTDOWN ─────────────────────────────────────────

    async def shutdown(self):
        """Apagar el servicio limpiamente."""
        logger.info("Apagando servicio...")
        self.running = False

        if self.deepgram:
            await self.deepgram.disconnect()

        try:
            await self.janus.hangup()
        except Exception:
            pass
        await self.janus.disconnect()

        logger.info("Servicio detenido")


# ── PUNTO DE ENTRADA ─────────────────────────────────────

async def main():
    service = VoiceAgentService()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(service.shutdown()))

    try:
        await service.start()
    except KeyboardInterrupt:
        await service.shutdown()
    except Exception as e:
        logger.error(f"Error fatal: {e}")
        await service.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
