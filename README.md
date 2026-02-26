# 🎙️ Voice Agent: Janus WebRTC + UCM6302 + Deepgram

Agente de voz IA que se conecta a la central Grandstream UCM6302 via SIP
y usa [Deepgram Voice Agent API](https://deepgram.com/product/voice-agent-api)
para manejar conversaciones telefónicas automáticas con IA.

## Arquitectura

```
┌──────────┐   SIP/RTP    ┌──────────┐              ┌──────────┐   WebSocket   ┌──────────┐
│ Teléfono │ ◄──────────► │  UCM6302 │ ◄──SIP/RTP─► │  Janus   │ ◄───────────► │  Voice   │
│   SIP    │              │ (PBX)    │              │ Gateway  │               │  Agent   │
└──────────┘              └──────────┘              └──────────┘               │ (Python) │
                                                                               └────┬─────┘
                                                                                    │ WS
                                                                               ┌────┴─────┐
                                                                               │ Deepgram │
                                                                               │ STT+LLM  │
                                                                               │ +TTS     │
                                                                               └──────────┘
```

**Deepgram Voice Agent API** unifica STT + LLM + TTS en un solo WebSocket,
eliminando la necesidad de orquestar servicios separados.

---

## Requisitos previos

1. **Servidor Linux** con Docker y Docker Compose
2. **UCM6302** accesible en la red local
3. **API Key de Deepgram** → [console.deepgram.com/signup](https://console.deepgram.com/signup) ($200 créditos gratis)
4. Puertos abiertos en el firewall (ver sección Puertos)

---

## Paso 1: Configurar extensión en la UCM6302

Antes de iniciar, crea una extensión SIP dedicada para el agente:

1. Accede al panel web de la UCM6302
2. Ve a **Extension/Trunk → Extensions → Create New Extension**
3. Configura:
   - **Extension**: `9000` (o el número que prefieras)
   - **Password (SIP)**: una contraseña segura
   - **Concurrent Registrations**: `1`
   - **NAT**: `Yes` (si Janus está en otra subred)
   - **DTMF Mode**: `RFC2833`
   - **Codecs permitidos**: Asegura que **PCMU (G.711 μ-law)** y **PCMA** estén habilitados
4. Guarda y aplica los cambios

### Opcional: Crear ruta de entrada (IVR)

Para que las llamadas externas lleguen al agente:
1. Ve a **Call Features → IVR**
2. Crea un IVR que redirija a la extensión `9000`
3. O configura un **Ring Group** / **Inbound Route** que apunte a `9000`

---

## Paso 2: Instalar el proyecto

```bash
# Clonar o copiar el proyecto
cd /opt  # o tu directorio preferido
mkdir janus-voice-agent && cd janus-voice-agent

# Copiar todos los archivos del proyecto aquí
# (conf/, voice-agent/, docker-compose.yml, .env)
```

---

## Paso 3: Configurar variables de entorno

Edita el archivo `.env`:

```bash
nano .env
```

Campos **obligatorios** a modificar:

```env
# IP de tu UCM6302
UCM_HOST=192.168.1.100

# Extensión y contraseña creadas en Paso 1
SIP_EXTENSION=9000
SIP_PASSWORD=tu_password_sip

# Tu API key de Deepgram
DEEPGRAM_API_KEY=tu_deepgram_api_key

# Modelo TTS (voces en español disponibles)
DEEPGRAM_TTS_MODEL=aura-2-luna-es

# Prompt del agente (personaliza según tu caso de uso)
AGENT_SYSTEM_PROMPT=Eres un asistente virtual de la clínica XYZ...
AGENT_GREETING=Hola, gracias por llamar a la clínica XYZ...
```

---

## Paso 4: Iniciar los servicios

```bash
# Construir y levantar
docker compose up -d --build

# Ver logs en tiempo real
docker compose logs -f

# Ver solo logs del agente
docker compose logs -f voice-agent

# Ver solo logs de Janus
docker compose logs -f janus-gateway
```

### Verificar que funciona

1. **Janus está corriendo**:
   ```bash
   curl http://localhost:8088/janus/info
   ```
   Debe retornar un JSON con información del servidor.

2. **El agente se registró en la UCM**:
   En los logs del voice-agent deberías ver:
   ```
   ✅ Registrado como sip:9000@192.168.1.100 en la UCM6302
      Listo para recibir llamadas en ext 9000
   ```

3. **Verificar en la UCM**:
   En el panel de la UCM6302, ve a **Status → PBX Status**.
   La extensión `9000` debe aparecer como **Registered**.

---

## Paso 5: Probar

Desde cualquier teléfono registrado en la UCM6302:
1. Marca la extensión `9000`
2. El agente debe contestar automáticamente
3. En los logs verás la conversación transcrita

---

## Puertos requeridos

| Puerto | Protocolo | Servicio | Dirección |
|--------|-----------|----------|-----------|
| 5060 | UDP | SIP (UCM → Janus) | Entrada/Salida |
| 8088 | TCP | Janus REST API | Local |
| 8188 | TCP | Janus WebSocket | Local |
| 10000-10500 | UDP | RTP Audio (SIP side) | Entrada/Salida |
| 20000-40000 | UDP | RTP Audio (WebRTC side) | Entrada/Salida |

---

## Archivos de configuración de Janus

Los archivos en `conf/` están preconfigurados. Los más relevantes:

| Archivo | Propósito |
|---------|-----------|
| `janus.jcfg` | Config principal, NAT/STUN, puertos RTP |
| `janus.plugin.sip.jcfg` | Plugin SIP, codecs, DTMF |
| `janus.transport.http.jcfg` | API REST (puerto 8088) |
| `janus.transport.websockets.jcfg` | WebSocket API (puerto 8188) |

### Ajustes importantes en `janus.jcfg`

Si el servidor está detrás de NAT:
```
nat: {
    nat_1_1_mapping = "TU_IP_PUBLICA"
    stun_server = "stun.l.google.com"
    stun_port = 19302
}
```

---

## Personalización del agente

### Cambiar el LLM

Deepgram permite usar diferentes proveedores de LLM:

```env
# OpenAI
DEEPGRAM_LLM_PROVIDER=open_ai
DEEPGRAM_LLM_MODEL=gpt-4o-mini

# Anthropic
DEEPGRAM_LLM_PROVIDER=anthropic
DEEPGRAM_LLM_MODEL=claude-3-haiku

# Custom (tu propio endpoint OpenAI-compatible)
DEEPGRAM_LLM_PROVIDER=custom
DEEPGRAM_LLM_MODEL=tu-modelo
```

### Voces TTS disponibles en español

```env
DEEPGRAM_TTS_MODEL=aura-2-luna-es      # Femenina
DEEPGRAM_TTS_MODEL=aura-2-orion-es     # Masculina
```

### Function Calling

Deepgram Voice Agent soporta function calling para integrar
con tu sistema (consultar base de datos, agendar citas, etc.).
Ver `deepgram_agent.py` para implementar.

---

## Limitaciones actuales y próximos pasos

### ⚠️ Bridge de audio RTP

Esta versión maneja correctamente la **señalización SIP** (registro,
aceptar/colgar llamadas) pero el **bridge de audio RTP** entre
Janus y Deepgram requiere integración adicional:

**Opciones para completar el bridge de audio:**

1. **aiortc** (Python WebRTC): Crear un PeerConnection local
   que se conecte con Janus y extraiga/inyecte audio.

2. **GStreamer**: Pipeline `rtpbin → audioconvert → Deepgram WS`
   para captura RTP directa.

3. **Janus AudioBridge + RTP forward**: Usar el plugin AudioBridge
   para hacer forward del audio a un puerto UDP local, capturarlo
   con Python y enviarlo a Deepgram.

4. **FFmpeg**: Capturar RTP con ffmpeg y pipe a Python.

La opción más viable para producción es **aiortc** o el
**AudioBridge con RTP forward**.

---

## Troubleshooting

### "Registration failed: 401 Unauthorized"
- Verifica usuario y contraseña SIP en `.env`
- Asegura que la extensión existe en la UCM6302
- Revisa que el campo `authuser` coincida con el número de extensión

### "No se recibe audio"
- Verifica que los puertos RTP estén abiertos (10000-10500, 20000-40000)
- Si hay NAT, configura `nat_1_1_mapping` en `janus.jcfg`
- Asegura que los codecs PCMU/PCMA estén habilitados en la UCM

### "Deepgram connection failed"
- Verifica tu API key en `.env`
- Asegura que el servidor tiene acceso a internet (para `wss://agent.deepgram.com`)
- Revisa créditos disponibles en [console.deepgram.com](https://console.deepgram.com)

### Janus no inicia
```bash
# Ver logs detallados
docker compose logs janus-gateway

# Verificar que los archivos de config existen
ls -la conf/

# Entrar al contenedor para debug
docker exec -it janus-gateway bash
```

---

## Detener servicios

```bash
docker compose down
```

---

## Licencia

MIT
