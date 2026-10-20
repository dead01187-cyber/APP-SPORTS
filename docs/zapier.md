# Configurar Zapier (2 Zaps)

El servidor ya hace solo los seguimientos, campañas, reintentos, la conciliación con Lofty y el
reporte semanal (programador interno, `SCHEDULER_ENABLED=true`). Las respuestas SMS llegan
directo de Twilio. En Zapier solo hacen falta dos Zaps.

Antes de empezar necesita:
- La URL del servidor (p. ej. `https://leadflow.onrender.com`). Ver `docs/render.md`.
- La `API_KEY` del servidor (Render → *Environment*).

---

## Zap 1 · Importar leads de Propwire

1. **Disparador:** Google Drive → *New File in Folder*. Elija la carpeta donde guarda los CSV
   exportados de Propwire.
2. **Acción:** Webhooks by Zapier → *POST*
   - URL: `https://SU-SERVIDOR/prospecting/import-csv`
   - Payload Type: `form`
   - File: el campo *File* del paso 1
   - Headers: `X-API-Key` = su `API_KEY`
3. Pruébelo con un CSV pequeño. La respuesta muestra `created`, `duplicates`, `rejected` e
   `imported_to_lofty`.

Exporte siempre en **CSV** (no Excel). Si sube un Excel, el servidor responde con un error que lo indica.

*Si usa el asistente de IA del editor de Zapier,* puede pegar esto:
> Cuando se agregue un archivo nuevo a la carpeta "Propwire" de Google Drive, enviarlo con un POST
> de Webhooks by Zapier a https://SU-SERVIDOR/prospecting/import-csv, como archivo (payload type form),
> con el encabezado X-API-Key: SU_API_KEY.

## Zap 2 · Alertas (vendedores interesados, errores, reporte semanal)

1. **Disparador:** Webhooks by Zapier → *Catch Hook*. Copie la URL que le da Zapier.
2. Pegue esa URL en Render → *Environment* → `ALERT_WEBHOOK_URL`.
3. **Acción:** Gmail → *Send Email* (o Twilio → *Send SMS* a su celular) con el campo `text`
   como cuerpo del mensaje.

Llegan alertas cuando:
- alguien responde que quiere vender o comprar;
- una API falla y necesita atención;
- el lunes a las 8:00, con el reporte semanal (importados, contactados, tasa de respuesta,
  CMAs enviados, oportunidades Hot, errores).

---

## Opcional: llamadas y revisión manual

`GET /review` lista lo que necesita a una persona: respuestas poco claras, preguntas (con la
respuesta sugerida por Claude), la lista de llamadas, CMAs para hacer a mano y errores.

- Cerrar un pendiente y enviar la respuesta:
  `POST /review/{id}/resolve` con `{"resolution": "respondido", "send_reply": "Hola, soy Daniel..."}`
- Registrar una llamada (Claude clasifica las notas como si fueran un SMS):
  `POST /contact/call-outcome` con `{"lead_id": 12, "notes": "Quiere vender en primavera"}`

Puede abrir `https://SU-SERVIDOR/docs` para usar estos endpoints desde el navegador.

## Si prefiere que Zapier dispare todo

Ponga `SCHEDULER_ENABLED=false` y cree Zaps con *Schedule by Zapier* que llamen (POST, con `X-API-Key`):
- `/jobs/run-due` cada hora,
- `/contact/campaign` con `{"mode": "sms"}` de lunes a viernes a las 10:00,
- `/monitoring/reconcile` una vez al día.
