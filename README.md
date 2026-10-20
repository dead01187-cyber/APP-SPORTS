# Leadflow: automatización de leads inmobiliarios

Servicio que implementa el diagrama de arquitectura:
**Propwire → BeenVerified → Lofty → SMS + Claude → CMA**.
Zapier trae los CSV de Propwire y entrega las alertas; el resto (seguimientos, campañas,
reintentos, conciliación y reporte semanal) lo programa el propio servidor.

```
Propwire (CSV) ──► 1 · Prospección ──► Lofty (leads limpios, etiquetados)
                        │
                        ▼
                  2 · Contacto ──► SMS ──► respuesta ──► Claude clasifica ──► acción
                        │                                    (Hot / Warm / Follow-up / DNC)
                        ▼
                  3 · CMA ──► Lofty crea el CMA ──► SMS con el link ──► nota en Lofty + llamada
```

## Qué hace cada parte del diagrama

| Diagrama | Dónde está |
|---|---|
| 1 · Prospección: obtener, limpiar, enriquecer, filtrar, deduplicar, importar a Lofty | `leadflow/pipeline/prospecting.py`, `leadflow/normalize.py` |
| Filter Rules (nombre, teléfono 1-3, dirección, duplicados, tipo de propiedad, Pond, Level, Call & Text) | `rules.yaml` → `prospecting`, `lofty` |
| 2 · Contacto: verificar teléfono, modo SMS/llamada, mensaje inicial EN/ES, procesar respuestas, seguimientos | `leadflow/pipeline/contact.py` |
| Response Classification (Claude): 8 categorías | `leadflow/integrations/claude.py`, `rules.yaml` → `actions` |
| 3 · CMA: elegir vendedores warm/hot, filtro ES, crear CMA (5 millas, costo 0), enviar por SMS, registrar en Lofty | `leadflow/pipeline/cma.py`, `rules.yaml` → `cma` |
| Database (PostgreSQL): leads, historial, duplicados, trabajos de seguimiento, auditoría | `leadflow/models.py` |
| Rules & Decision Logic | `rules.yaml` (editable sin programar) |
| Monitoring & Error Handling: errores de API, reintentos, log, alertas, conciliación, cola de revisión manual | `leadflow/monitoring.py`, `leadflow/jobs.py` |
| Reports & Analytics | `leadflow/reports.py` → `GET /reports/summary` + reporte semanal por alerta |
| Horarios (seguimientos, campaña diaria, conciliación) | `leadflow/scheduler.py`, `rules.yaml` → `scheduler` |
| Zapier (2 Zaps: importar CSV y alertas) | `docs/zapier.md` |

## Listas de Propwire

El importador lee la exportación de Propwire tal cual (56 columnas). Probado con una lista real de 250 propiedades:

- **Registro No Llamar:** los teléfonos marcados `dnc` en "Do Not 1/2/3 Call" no se usan y quedan en la lista DNC
  interna, para que no se les escriba aunque aparezcan en otra lista sin la marca.
- **Litigantes:** las filas marcadas `tcpa` en "Litigator" se descartan, y sus números quedan en la lista DNC.
- **Listas posteriores:** si un CSV nuevo marca un número o un litigante que ya estaba en el sistema, ese número se
  bloquea en los leads existentes (y en Lofty).
- **Tipo de teléfono:** los fijos ("Land Line") van solo a la lista de llamadas; nunca reciben SMS.
- **Mismo contacto en varias propiedades** (unidades de un condominio): un solo lead y un solo SMS; las otras
  propiedades se anotan en ese lead.
- **Contacto que no es el dueño:** cuando "Name 1" no coincide con ningún dueño (a menudo un inquilino), el primer SMS
  pregunta "¿es usted el dueño de…?" en vez de ofrecer vender (plantilla `initial_owner_check`).
- Cada lead nuevo lleva en Lofty una nota con el dueño registrado, la propiedad, la dirección postal, el APN y el link.

## Cómo se decide cada respuesta

| Claude clasifica | Estado | Siguiente paso (configurable en `rules.yaml`) |
|---|---|---|
| Interested (Selling) | Hot | Alerta al agente con un link para pegar el CMA; el sistema lo envía por SMS |
| Interested (Buying) | Hot | Alerta al agente + cola de revisión |
| Question | Warm | Cola de revisión con la respuesta sugerida por Claude |
| Future Follow-up | Follow-up | Programa seguimiento (fecha que dio la persona, o 6 meses) |
| Not Interested | Not Interested | Vuelve a escribir en 1 año (0 = nunca) |
| Wrong Number | sin cambio | Marca el número y prueba el siguiente teléfono (hasta 3) |
| Unclear / confianza < 60 % | sin cambio | Cola de revisión manual |
| Opt-out (STOP) | DNC | Lista DNC, cancela todo lo pendiente; nunca más se le escribe |

Reglas de seguridad que no dependen del modelo:

- **STOP, ALTO, BAJA, UNSUBSCRIBE…** se detectan con palabras clave *antes* de llamar a Claude.
- Ningún SMS sale a números en la lista DNC, ni fuera del horario permitido (9:00–20:00 hora local por defecto).
- Si alguien que pidió no recibir mensajes vuelve a escribir, va a revisión humana; no se le reactiva solo.
- Los errores de Twilio “número dado de baja” (21610) agregan el número a DNC automáticamente.

## Ponerlo en marcha

### Probar en su computadora (sin tocar servicios reales)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # DRY_RUN=true: no envía nada real
uvicorn leadflow.api:app --reload
```

Abra http://localhost:8000/docs para ver y probar todos los endpoints.

### Producción en Render (recomendado)

Siga `docs/render.md`: **New → Blueprint** con este repositorio crea el servidor y la base de datos
usando `render.yaml`. Después, `docs/zapier.md` para los 2 Zaps.

### Producción con Docker (servidor propio)

```bash
cp .env.example .env   # complete las claves, SCHEDULER_ENABLED=true, DRY_RUN=false
docker compose up -d --build
```

### Variables principales (`.env`)

| Variable | Para qué |
|---|---|
| `API_KEY` | Clave que Zapier envía en `X-API-Key` (obligatoria) |
| `DATABASE_URL` | PostgreSQL en producción |
| `ANTHROPIC_API_KEY` | Claude, para clasificar respuestas |
| `LOFTY_API_KEY` | Lofty CRM |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` | Envío y recepción de SMS |
| `BEENVERIFIED_API_KEY`, `BEENVERIFIED_API_URL` | Enriquecimiento (opcional) |
| `ALERT_WEBHOOK_URL` | Alertas a Slack o a un Zap que envíe email |
| `PUBLIC_BASE_URL` | URL pública, para validar firmas de Twilio (en Render se detecta sola) |
| `SCHEDULER_ENABLED` | `true` = el servidor programa seguimientos, campañas, conciliación y reporte |
| `DRY_RUN` | `true` para probar sin enviar nada |

## Endpoints

Todos piden el encabezado `X-API-Key`, salvo `/health` (y los webhooks firmados por Twilio).

| Método | Ruta | Uso |
|---|---|---|
| GET | `/health` | Estado de la base de datos y de cada integración |
| POST | `/prospecting/import-csv` | Subir el CSV exportado de Propwire (cuerpo = el archivo) |
| POST | `/prospecting/import` | Importar leads en JSON: `{"leads": [{...}]}` |
| POST | `/contact/campaign` | Enviar mensajes iniciales: `{"mode": "sms" \| "call", "limit": 100}` |
| POST | `/webhooks/sms/inbound` | Respuestas SMS (Twilio directo o Zapier) |
| POST | `/webhooks/sms/status` | Estados de entrega de Twilio |
| POST | `/contact/call-outcome` | Notas de una llamada: `{"lead_id": 1, "notes": "..."}` (Claude las clasifica) |
| POST | `/cma/run` | Crear y enviar CMAs pendientes |
| POST | `/cma/{lead_id}/link` | Enviar un CMA hecho a mano: `{"url": "..."}` |
| POST | `/jobs/run-due` | Ejecutar seguimientos, CMAs y reintentos pendientes (cada 15–60 min) |
| POST | `/monitoring/reconcile` | Conciliar con Lofty y reactivar trabajos atascados (diario) |
| GET | `/review` | Cola de revisión manual (`?kind=call` para la lista de llamadas) |
| POST | `/review/{id}/resolve` | Cerrar un pendiente; `send_reply` envía un SMS (p. ej. la sugerencia de Claude) |
| POST | `/dnc` | Agregar un número a la lista DNC |
| GET | `/reports/summary?days=30` | Importados, contactados, tasa de respuesta, CMAs, oportunidades Hot, errores |
| GET | `/leads/{id}` | Ver un lead |

## Pruebas

```bash
pytest                                   # 89 pruebas, SQLite en memoria
TEST_DATABASE_URL=postgresql+psycopg://usuario@localhost/leadflow_test pytest   # mismas pruebas en PostgreSQL
```

Las pruebas reemplazan Lofty, Twilio, BeenVerified y Claude por versiones falsas: no envían nada real.

## Pendiente de configurar o verificar

Estos puntos están marcados como *Pending setup* en el diagrama o dependen de su cuenta:

1. **API de Lofty (probada).** Se creó, actualizó y borró un lead de prueba en su cuenta: nombre, emails,
   teléfonos, dirección, etiquetas, tipo Seller (`1`), notas y "no textear / no llamar" funcionan.
   - El campo *source* solo acepta fuentes de la lista de Lofty (si no, queda "Other"); la etiqueta
     "Propwire" sí se guarda.
   - Los leads se asignan a **Mark Rubio-Novack** (`rules.yaml` → `lofty` → `assigned_user_id`).
     Para mandarlos a un pond en su lugar, ponga su número en `pond_id`.
   - "Level: Company" no está disponible para la cuenta conectada (solo *Personal*).
2. **CMA.** Lofty no tiene API de CMA en su cuenta. Cuando alguien quiere vender, llega una alerta con un
   link seguro: abre una página, pega el link del CMA hecho en Lofty y el sistema lo envía por SMS,
   lo anota en Lofty y programa la llamada.
3. **BeenVerified.** No tiene una API pública estándar. El cliente envía nombre + dirección a
   `BEENVERIFIED_API_URL`; ajuste `parse_response()` a la respuesta de su cuenta, o déjelo vacío y el paso se omite.
4. **Propwire** entra por CSV exportado (o filas enviadas desde Zapier). Las columnas se reconocen por nombre
   (`leadflow/normalize.py` → `FIELD_ALIASES`); agregue alias si su exportación usa otros encabezados.
5. **Twilio A2P 10DLC.** En EE. UU. hay que registrar la marca y la campaña en Twilio para enviar SMS
   desde números normales; sin ese registro los mensajes se bloquean.
6. **Cumplimiento (TCPA / DNC).** El sistema respeta STOP, horario permitido y su propia lista DNC, pero
   **no** consulta el Registro Nacional No Llame. Enviar SMS de marketing a números obtenidos de listas
   puede requerir consentimiento previo; confirme sus obligaciones con un abogado antes de enviar en volumen.
