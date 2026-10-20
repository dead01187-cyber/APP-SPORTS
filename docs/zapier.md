# Configurar Zapier

Zapier es el “motor de automatización”: dispara cada paso llamando a este servicio.
Todos los Zaps usan **Webhooks by Zapier → Custom Request** (o POST) con:

- URL: `https://SU-SERVIDOR/<ruta>`
- Encabezados: `X-API-Key: <su API_KEY>` y `Content-Type: application/json`

La respuesta siempre es JSON, así que los pasos siguientes del Zap (Paths, Filter, Slack, Email)
pueden usar sus campos.

---

## Zap 1 · Importar leads de Propwire

**Disparador:** Google Drive → *New File in Folder* (la carpeta donde guarda los CSV exportados de Propwire).

**Acción:** Webhooks by Zapier → *Custom Request*
- Método: `POST`
- URL: `https://SU-SERVIDOR/prospecting/import-csv`
- Encabezados: `X-API-Key: ...`, `Content-Type: text/csv`
- Data: el contenido del archivo (campo *File* del disparador)

*Alternativa:* Google Sheets → *New Spreadsheet Row* → POST a `/prospecting/import` con
`{"leads": [{"owner_name": "...", "property_address": "...", "zip_code": "...", "phone1": "..."}]}`.

La respuesta trae `created`, `duplicates`, `rejected` y `imported_to_lofty`.

## Zap 2 · Campaña de mensajes iniciales

**Disparador:** Schedule by Zapier → *Every Weekday* a las 10:00.

**Acción:** POST `https://SU-SERVIDOR/contact/campaign` con `{"mode": "sms", "limit": 100}`.

Para trabajar por llamadas uno por uno, use `{"mode": "call"}`: los leads quedan en la lista de
llamadas (`GET /review?kind=call`). Después de llamar, envíe las notas a `/contact/call-outcome`
y Claude las clasifica igual que un SMS.

## Zap 3 · Respuestas SMS

**Opción A (recomendada, sin Zapier):** en la consola de Twilio, en su número →
*Messaging → A message comes in* → Webhook `https://SU-SERVIDOR/webhooks/sms/inbound` (HTTP POST).
Configure también `PUBLIC_BASE_URL` para que la firma de Twilio se valide.

**Opción B (con Zapier):**
- Disparador: Twilio → *New Received SMS*.
- Acción: POST `https://SU-SERVIDOR/webhooks/sms/inbound` con
  `{"from": "{{From}}", "body": "{{Body}}", "message_id": "{{MessageSid}}"}`.
- Opcional: *Paths by Zapier* según `status` de la respuesta:
  - `hot` → mensaje de Slack o SMS al agente.
  - `dnc` → nada más (ya quedó en la lista DNC).

## Zap 4 · Trabajos programados (seguimientos, CMA, reintentos)

**Disparador:** Schedule by Zapier → *Every Hour*.

**Acción:** POST `https://SU-SERVIDOR/jobs/run-due` con `{}`.

Esto envía los seguimientos de 6 meses / 1 año / fecha específica, crea y envía los CMAs,
y reintenta las llamadas a APIs que fallaron (1 min, 5 min, 30 min, 2 h, 12 h). Si un trabajo
falla 5 veces, va a la cola de revisión y se envía una alerta.

## Zap 5 · Conciliación y reporte

- **Diario:** Schedule → POST `https://SU-SERVIDOR/monitoring/reconcile`.
  Reintenta leads que no llegaron a Lofty y reactiva trabajos atascados.
- **Semanal:** Schedule → GET `https://SU-SERVIDOR/reports/summary?days=7` →
  Email by Zapier / Slack con `leads_imported`, `contacted`, `response_rate`, `cma_sent`,
  `opportunities_hot` y `errors_last_24h`.

## Zap 6 · Alertas

- Disparador: Webhooks by Zapier → *Catch Hook*. Copie la URL que le da Zapier.
- Póngala en `ALERT_WEBHOOK_URL` del servicio.
- Acción: Email by Zapier (o Slack) con el campo `text`.

Llegan alertas cuando hay un vendedor interesado, un comprador, o un error que necesita atención.

## Cola de revisión manual

`GET /review` lista lo que necesita a una persona: respuestas poco claras, preguntas (con la
respuesta sugerida por Claude), la lista de llamadas, CMAs para hacer a mano y errores.
Para cerrar un pendiente y, si quiere, enviar la respuesta:

```
POST /review/{id}/resolve
{"resolution": "respondido", "send_reply": "Hola, soy Daniel..."}
```
