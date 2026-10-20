# Publicar en Render

Tiempo estimado: 20–30 minutos. Necesita el código en GitHub (repositorio `APP-SPORTS`).

## 1. Crear el servidor y la base de datos

1. Cree una cuenta en https://render.com (puede entrar con su cuenta de GitHub).
2. **New → Blueprint** → elija el repositorio `APP-SPORTS`.
3. Render lee `render.yaml` y propone dos cosas: el servidor `leadflow` y la base de datos `leadflow-db`.
4. Le pedirá estos valores (puede dejar vacíos los que aún no tenga y llenarlos después en *Environment*):

| Variable | Dónde se consigue |
|---|---|
| `AGENT_NAME` | El nombre que firma los SMS (p. ej. Daniel) |
| `AGENT_PHONE` | Opcional |
| `ANTHROPIC_API_KEY` | https://console.anthropic.com → API Keys |
| `LOFTY_API_KEY` | En Lofty, en la sección de integraciones / API. Si no la encuentra, pídala al soporte de Lofty |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | https://console.twilio.com → *Account Info* |
| `TWILIO_FROM_NUMBER` | Twilio → *Phone Numbers → Active numbers* (formato `+1XXXXXXXXXX`) |
| `ALERT_WEBHOOK_URL` | La URL del Zap de alertas (ver `docs/zapier.md`, Zap 2) |

5. Clic en **Apply**. Cuando termine, abra `https://SU-SERVICIO.onrender.com/health`.
   Debe decir `"ok": true` y `"scheduler": "on"`.

`API_KEY` la genera Render. Cópiela desde *Environment*: la necesita para Zapier.
`PUBLIC_BASE_URL` no hace falta en Render: el servidor usa su URL de Render automáticamente.

Use el plan *Starter* (no *Free*): el gratuito se apaga cuando no hay tráfico y los SMS
entrantes y el programador no funcionarían.

## 2. Conectar Twilio (respuestas SMS, sin Zapier)

En Twilio → *Phone Numbers → Active numbers* → su número → *Messaging Configuration*:

- **A message comes in:** Webhook, `https://SU-SERVICIO.onrender.com/webhooks/sms/inbound`, HTTP POST.

Importante: en EE. UU., Twilio exige registrar la marca y la campaña (A2P 10DLC) para enviar
SMS desde números normales. Sin ese registro, los mensajes se bloquean. Se hace en
Twilio → *Messaging → Regulatory Compliance*.

## 3. Ajustar las reglas de Lofty

En `rules.yaml` → `lofty`:

- `pond_id`: el número del pond "Daniel R-N" (Lofty lo pide como número, no como nombre).
- `lead_types`: el número del tipo "Seller", cuando lo confirme.

Cada cambio en GitHub vuelve a desplegar el servidor automáticamente.

## 4. Probar sin enviar nada real

Con `DRY_RUN=true` el sistema hace todo, pero no llama a Lofty, Twilio ni BeenVerified:

1. Suba un CSV pequeño (3–5 leads) con el Zap 1, o desde `https://SU-SERVICIO.onrender.com/docs`.
2. Revise `GET /reports/summary` y `GET /review`.

## 5. Encender

Cuando las claves estén puestas y las pruebas se vean bien: *Environment* → `DRY_RUN` = `false` → *Save*.
Desde ese momento, los mensajes iniciales salen de lunes a viernes a las 10:00 (configurable en
`rules.yaml` → `scheduler`).
