"""
═══════════════════════════════════════════════════════════════
janus_sip_client.py - Cliente SIP via Janus WebSocket API
═══════════════════════════════════════════════════════════════

Maneja la conexión WebSocket con Janus Gateway para:
  1. Crear sesión y adjuntar el plugin SIP
  2. Registrar extensión SIP en la UCM6302
  3. Recibir/realizar llamadas
  4. Gestionar señalización SDP y flujo de audio
"""

import json
import asyncio
import uuid
from loguru import logger
import websockets


class JanusSIPClient:
    """Cliente asíncrono para el plugin SIP de Janus via WebSocket."""

    def __init__(self, ws_url: str, api_secret: str = None):
        self.ws_url = ws_url
        self.api_secret = api_secret
        self.ws = None
        self.session_id = None
        self.handle_id = None
        self.transactions = {}
        self.event_handlers = {}
        self._keepalive_task = None
        self._listener_task = None

    # ── CONEXIÓN ─────────────────────────────────────────

    async def connect(self):
        """Conectar al WebSocket de Janus y crear sesión."""
        logger.info(f"Conectando a Janus: {self.ws_url}")
        self.ws = await websockets.connect(
            self.ws_url,
            subprotocols=["janus-protocol"],
            ping_interval=30,
            ping_timeout=10,
        )

        self.session_id = await self._create_session()
        logger.info(f"Sesión Janus creada: {self.session_id}")

        self._listener_task = asyncio.create_task(self._event_listener())
        self._keepalive_task = asyncio.create_task(self._keepalive())
        return self.session_id

    async def _create_session(self) -> int:
        resp = await self._send_request({"janus": "create"})
        return resp["data"]["id"]

    async def attach_sip_plugin(self) -> int:
        """Adjuntar el plugin SIP a la sesión."""
        resp = await self._send_request({
            "janus": "attach",
            "session_id": self.session_id,
            "plugin": "janus.plugin.sip",
        })
        self.handle_id = resp["data"]["id"]
        logger.info(f"Plugin SIP adjuntado (handle={self.handle_id})")
        return self.handle_id

    # ── REGISTRO SIP ─────────────────────────────────────

    async def register(self, ucm_host: str, ucm_port: int,
                       extension: str, password: str,
                       display_name: str = "Agente IA"):
        """Registrar como extensión SIP en la UCM6302."""
        register_msg = {
            "request": "register",
            "username": f"sip:{extension}@{ucm_host}",
            "authuser": extension,
            "display_name": display_name,
            "secret": password,
            "proxy": f"sip:{ucm_host}:{ucm_port}",
            "force_udp": True,
        }
        logger.info(f"Registrando ext {extension} en {ucm_host}:{ucm_port}...")
        return await self._send_plugin_message(register_msg)

    # ── LLAMADAS ─────────────────────────────────────────

    async def call(self, uri: str):
        """Realizar una llamada SIP saliente."""
        logger.info(f"Llamando a {uri}...")
        return await self._send_plugin_message({
            "request": "call",
            "uri": uri,
            "autoaccept_reinvites": True,
        })

    async def accept_call(self, jsep: dict = None):
        """Aceptar una llamada entrante con un SDP answer."""
        msg = {"request": "accept"}
        return await self._send_plugin_message(msg, jsep=jsep)

    async def hangup(self):
        """Colgar la llamada actual."""
        logger.info("Colgando llamada...")
        return await self._send_plugin_message({"request": "hangup"})

    async def send_dtmf(self, tones: str):
        """Enviar tonos DTMF."""
        return await self._send_plugin_message({
            "request": "dtmf_info",
            "digit": tones,
        })

    # ── EVENTOS ──────────────────────────────────────────

    def on_event(self, event_type: str, handler):
        """
        Registrar handler para eventos SIP.

        Eventos: registered, registration_failed, incomingcall,
                 accepted, hangup, calling, ringing, progress,
                 jsep, webrtcup, media
        """
        self.event_handlers[event_type] = handler

    async def _event_listener(self):
        """Loop principal de escucha de eventos del WebSocket."""
        try:
            async for message in self.ws:
                data = json.loads(message)
                janus_type = data.get("janus")

                # Respuestas a transacciones pendientes
                tx = data.get("transaction")
                if tx and tx in self.transactions:
                    fut = self.transactions.pop(tx)
                    if not fut.done():
                        fut.set_result(data)
                    continue

                # Eventos del plugin SIP
                if janus_type == "event":
                    pd = data.get("plugindata", {}).get("data", {})
                    result = pd.get("result", {})
                    ev = result.get("event") or pd.get("event")

                    if ev and ev in self.event_handlers:
                        h = self.event_handlers[ev]
                        asyncio.create_task(h(data)) if asyncio.iscoroutinefunction(h) else h(data)

                    # JSEP (SDP offer/answer)
                    jsep = data.get("jsep")
                    if jsep and "jsep" in self.event_handlers:
                        h = self.event_handlers["jsep"]
                        asyncio.create_task(h(jsep, data)) if asyncio.iscoroutinefunction(h) else h(jsep, data)

                elif janus_type == "webrtcup":
                    logger.info("WebRTC PeerConnection activa")
                    if "webrtcup" in self.event_handlers:
                        h = self.event_handlers["webrtcup"]
                        asyncio.create_task(h(data)) if asyncio.iscoroutinefunction(h) else h(data)

                elif janus_type == "media":
                    logger.debug(f"Media {data.get('type')}: receiving={data.get('receiving')}")

                elif janus_type == "hangup":
                    logger.info(f"Hangup: {data.get('reason', '?')}")
                    if "hangup" in self.event_handlers:
                        h = self.event_handlers["hangup"]
                        asyncio.create_task(h(data)) if asyncio.iscoroutinefunction(h) else h(data)

        except websockets.exceptions.ConnectionClosed:
            logger.warning("WebSocket Janus desconectado")
        except Exception as e:
            logger.error(f"Error en listener: {e}")

    # ── COMUNICACIÓN INTERNA ─────────────────────────────

    async def _send_request(self, msg: dict, timeout: float = 10.0) -> dict:
        tx = str(uuid.uuid4())[:12]
        msg["transaction"] = tx
        if self.api_secret:
            msg["apisecret"] = self.api_secret

        fut = asyncio.get_event_loop().create_future()
        self.transactions[tx] = fut
        await self.ws.send(json.dumps(msg))

        try:
            result = await asyncio.wait_for(fut, timeout=timeout)
            if result.get("janus") == "error":
                raise Exception(f"Janus error: {result.get('error', {}).get('reason')}")
            return result
        except asyncio.TimeoutError:
            self.transactions.pop(tx, None)
            raise Exception("Timeout esperando respuesta de Janus")

    async def _send_plugin_message(self, body: dict, jsep: dict = None) -> dict:
        msg = {
            "janus": "message",
            "session_id": self.session_id,
            "handle_id": self.handle_id,
            "body": body,
        }
        if jsep:
            msg["jsep"] = jsep
        return await self._send_request(msg, timeout=30.0)

    async def _keepalive(self):
        while True:
            try:
                await asyncio.sleep(25)
                await self._send_request({
                    "janus": "keepalive",
                    "session_id": self.session_id,
                })
            except Exception as e:
                logger.warning(f"Keepalive error: {e}")
                break

    async def disconnect(self):
        if self._keepalive_task:
            self._keepalive_task.cancel()
        if self._listener_task:
            self._listener_task.cancel()
        if self.ws:
            try:
                await self._send_request({
                    "janus": "destroy",
                    "session_id": self.session_id,
                })
            except Exception:
                pass
            await self.ws.close()
        logger.info("Desconectado de Janus")
