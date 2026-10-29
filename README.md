# TikTok ↔ Kommo · Atribución de conversaciones

Servicio propio para cerrar el ciclo entre **TikTok Ads → WhatsApp → Kommo → TikTok Ads**.
Captura el clic de TikTok, lo une con la conversación de WhatsApp y devuelve a TikTok
los eventos del embudo (contacto, interesado, calificado, compra) **sin duplicados** y
**solo con la etapa más alta alcanzada**.

Corre en un contenedor aislado, con PostgreSQL privado, detrás de **Traefik** y sin
publicar puertos al host. No toca otros proyectos del VPS.

---

## 1. Por qué hace falta un puente

TikTok solo agrega el `ttclid` (TikTok Click ID) a la **URL de una landing page**.
Si el anuncio va directo a `wa.me`, ese identificador se pierde y TikTok no puede
unir la venta con el clic. Las etiquetas en Kommo sirven para reportar, pero no
identifican al usuario a nivel de clic.

El flujo correcto es:

```
Anuncio TikTok
   │  (TikTok agrega ?ttclid=... y tus UTMs)
   ▼
https://track.tudominio.com/r   ← puente propio
   │  guarda {ttclid, UTMs, ip, user-agent}
   │  genera REF-XXXXXXXX
   ▼
https://wa.me/57...?text=Hola... REF-XXXXXXXX
   │
   ▼
Kommo crea el lead y dispara add_message
   │
   ▼
Este servicio lee el REF, vincula el clic y envía el evento a TikTok
```

El `REF-XXXXXXXX` es la llave que une el clic con el lead. Es visible en el primer
mensaje, pero es inocuo. Sin él, la atribución depende solo de email/teléfono
hasheados y la tasa de coincidencia baja.

> **UTMs:** TikTok no las agrega automáticamente. Configúralas en el anuncio
> (`utm_source=tiktok`, `utm_medium=paid`, `utm_campaign=...`). **No agregues
> `ttclid` a mano**: TikTok lo añade solo y duplicarlo rompe el valor.

---

## 2. Qué hace exactamente

- `GET /r`: puente de atribución. Guarda el clic y redirige a WhatsApp con el REF.
- `POST /webhook/kommo`: recibe `add_lead`, `update_lead`, `status_lead` y
  `add_message`, valida el secreto y encola el trabajo.
- Worker interno: consulta el lead real en Kommo, extrae contacto, REF, `ttclid`,
  valor y etapa; decide si corresponde un evento nuevo; lo envía a TikTok Events API.
- PostgreSQL: guarda clics, leads, cola de trabajos, outbox de eventos, logs y
  deduplicación. Todo sobrevive reinicios.
- Panel web con Basic Auth: métricas, eventos, clics, webhooks, mapa de etapas y
  botones de reintento/sincronización.

### Regla anti-duplicados

Cada etapa tiene un `rank` (orden del embudo). Solo se envía un evento cuando el
lead llega a una etapa de **rank mayor** que la máxima ya enviada. Si estaba en
“interesado” y compra, se envía `Purchase`; un webhook atrasado de “interesado” se
ignora. Si el lead retrocede y vuelve a una etapa inferior, tampoco se reenvía.

Cada evento usa un `event_id` estable (`lead + status + evento + rank`). TikTok
deduplica por `event_source_id + event + event_id`.

---

## 3. Despliegue en el VPS (Hostinger Ubuntu + Docker + Traefik)

### 3.1 Requisitos

- Docker y Docker Compose.
- Traefik ya corriendo, con la red externa `traefik-proxy` y un resolver TLS
  llamado `letsencrypt` (ajústalo si en tu servidor se llama distinto).
- Un subdominio, por ejemplo `track.tudominio.com`, con registro **A** apuntando
  a la IP del VPS.

### 3.2 Subir el proyecto

```bash
sudo mkdir -p /opt/tiktok-kommo-attribution
sudo chown "$USER":"$USER" /opt/tiktok-kommo-attribution
# copia o clona el contenido de esta carpeta allí
cd /opt/tiktok-kommo-attribution
```

### 3.3 Instalación guiada

Dentro de la carpeta del proyecto:

```bash
chmod +x install.sh
sudo ./install.sh
```

El script pregunta dominio, subdominio de Kommo y datos de Traefik. Genera
automáticamente:

- `.env` con contraseña de PostgreSQL.
- `SECRET_KEY` para cifrar los secretos.
- Secreto del webhook de Kommo.
- Contraseña aleatoria del panel.
- Arranca `docker compose` y aplica las migraciones.

Al final imprime la URL del panel, el usuario y la contraseña. **Guárdala**.

> No necesitas pegar tokens de Kommo/TikTok en el chat ni en `.env`. Se cargan
> después desde **Configuración** dentro del panel.

### 3.4 Verificar Traefik

El instalador comprueba que exista la red indicada. Si no usas los valores por
defecto, revisa:

```bash
docker network ls | grep traefik
docker ps --format '{{.Names}}' | grep traefik
```

El compose usa estas variables del `.env`:

```env
TRAEFIK_NETWORK=traefik-proxy
TRAEFIK_CERTRESOLVER=letsencrypt
TRAEFIK_ENTRYPOINT=websecure
```

### 3.5 Abrir el panel

```text
https://track.TUDOMINIO.com
```

Entra con el usuario `admin` y la contraseña que imprimió el instalador.
Luego abre **Configuración**.

### 3.6 Configuración desde el panel

En `/settings` se configuran:

- Token y subdominio de Kommo.
- Secreto del webhook y mapa de etapas.
- Access token, Pixel ID o Event Set ID de TikTok.
- Número y mensaje de WhatsApp.
- Campos personalizados y parámetros del worker.

Los secretos se guardan **cifrados** con `SECRET_KEY`; el panel nunca los vuelve
a mostrar. Si dejas un campo secreto vacío, se conserva el valor actual. Para
borrarlo, marca la casilla correspondiente.

Los valores guardados en el panel tienen prioridad sobre `.env`. El `.env` queda
solo para infraestructura (dominio, base de datos, Traefik, contraseña del panel
y `SECRET_KEY`).

> **Backup:** guarda `.env` y el volumen `appdata`. Si pierdes `SECRET_KEY`,
> los secretos guardados no se pueden descifrar y hay que volver a escribirlos.

### 3.7 Aislamiento

- `app` está en `internal` y `traefik-proxy`.
- `db` está **solo** en `internal`, sin puertos publicados.
- El volumen `pgdata` es propio de este proyecto.
- No se publica nada al host; solo entra por Traefik.
- Para depurar sin Traefik, descomenta `ports` en el compose (queda en
  `127.0.0.1:8010`, no expuesto a internet).

---

## 4. Configurar Kommo

### 4.1 Token y subdominio

En Kommo: **Ajustes → Integraciones → API** (o un token de larga duración).
En el panel, **Configuración → Kommo**, completa subdominio y token. Guarda.

El token queda cifrado en PostgreSQL y no se vuelve a mostrar en la interfaz.

### 4.2 Webhook

En Kommo: **Ajustes → Integraciones → Web hooks → Añadir webhook**.

- URL: la que muestra el panel, por ejemplo
  `https://track.tudominio.com/webhook/kommo?secret=TU_SECRETO`
  Si Kommo elimina el query string, usa la variante por ruta:
  `https://track.tudominio.com/webhook/kommo/TU_SECRETO`
- Eventos: `add_lead`, `update_lead`, `status_lead`, `add_message`.
- Para probar sin encolar nada, añade `&dry_run=1` a la URL del webhook
  (también acepta el header `X-Webhook-Secret`).

Kommo puede enviar `application/x-www-form-urlencoded` o JSON; el servicio acepta
ambos. El secreto se valida con comparación constante.

### 4.3 Obtener los IDs de las etapas

Con el panel arriba:

```bash
curl -u admin:TU_PASSWORD https://track.tudominio.com/api/pipelines | python3 -m json.tool
```

También puedes verlos en la URL de Kommo al abrir una etapa, o con la API
`/api/v4/leads/pipelines`.

### 4.4 Mapear etapas → eventos

En **Configuración → Kommo → Mapa de etapas** pega un JSON con el `status_id`
de Kommo como clave:

```json
{"111111":{"key":"nuevo","rank":1,"event":"Contact"},"222222":{"key":"interesado","rank":2,"event":"SubmitForm"},"333333":{"key":"calificado","rank":3,"event":"CompleteRegistration"},"444444":{"key":"compra","rank":4,"event":"Purchase"}}
```

- `rank` debe crecer con el embudo. Nunca lo repitas para etapas distintas.
- `enabled: false` desactiva una etapa sin borrarla.
- Para `event_source=web`, usa eventos estándar (`Contact`, `SubmitForm`,
  `CompleteRegistration`, `Purchase`). Un nombre inventado llega como evento
  personalizado y **no** sirve como objetivo de optimización web.
- Para `event_source=crm`, los nombres pueden ser personalizados; luego los mapeas
  a las etapas del embudo en Events Manager.

Al guardar en el panel no hace falta reiniciar: el worker toma la configuración
en el siguiente ciclo.

### 4.5 Refuerzo opcional en Kommo

El flujo normal ya funciona con `add_message`: el texto del primer WhatsApp trae el
`REF-XXXXXXXX`. Si tu cuenta no envía `lead_id` en ese webhook, el servicio lo
resuelve por `contact_id`.

Para evitar que `add_lead` se procese antes de que llegue el mensaje con el REF,
el servicio espera `LEAD_JOB_DELAY_SECONDS` (5 s por defecto) y fusiona los dos
webhooks en un solo trabajo. Si tu Kommo tarda más en entregar `add_message`,
sube ese valor.

También puedes guardar el REF en un campo personalizado y configurar
`KOMMO_FIELD_REF`, pero no es obligatorio.

---

## 5. Configurar TikTok

### 5.1 Elegir el modo

| Modo | Cuándo usarlo | `event_source_id` | Nombres de evento |
|---|---|---|---|
| `web` | Campañas de conversión web; tienes Pixel y `ttclid` | Pixel ID | Estándar |
| `crm` | Campañas de Lead Generation / calidad de lead; quieres enviar el embudo del CRM | CRM Event Set ID | Personalizados, mapeados en Events Manager |
| `offline` | Conversiones offline con Offline Event Set | Offline Event Set ID | Estándar |

Recomendación práctica:

- Si tu campaña es de **conversiones web** y quieres atribuir la venta por `ttclid`,
  usa `web`.
- Si tu campaña es de **mensajería / Lead Generation** y quieres que TikTok aprenda
  de leads calificados y compras, usa `crm` (postback de señales). TikTok documenta
  este camino como *lead quality optimization*.
- La integración nativa de Kommo suele usar el camino de CRM/offline. Como no la
  tienes disponible, este servicio hace ese trabajo.

### 5.2 Token

En **Events Manager → Data Sources → tu fuente → Settings → Generate access token**.
Pega el token en el panel, **Configuración → TikTok → Access token**. No se
muestra de vuelta.

### 5.3 Modo web

En **Configuración → TikTok** elige:

```text
Modo: web
Pixel ID: tu Pixel ID
Event Set ID: (vacío)
```

Si tienes el pixel en una landing concreta, configura la **URL de página para
page.url** con esa URL. Si la dejas vacía, se usa la URL del puente `/r`.

### 5.4 Modo CRM

1. En Events Manager: **Connect data source → CRM → Manual setup**.
2. Crea el **CRM Event Set**.
3. Obtén su ID con la API `/crm/list/` o desde la URL de Events Manager.
4. En el panel elige **Modo: crm** y pega el Event Set ID.
5. En Events Manager, mapea tus eventos CRM a las etapas del embudo
   (por ejemplo: `LeadNuevo`, `LeadInteresado`, `LeadCalificado`, `Compra`).
6. El servicio envía `lead.lead_id`, que es obligatorio en CRM.

### 5.5 Probar antes de gastar

En **Configuración → TikTok → Test event code** escribe temporalmente:

```text
TEST12345
```

Los eventos aparecen en **Events Manager → Test events** y **no** entran a la
optimización. Cuando todo esté validado, borra el campo y guarda.

También puedes usar el formulario **“Enviar evento de prueba”** del panel.

---

## 6. Configurar el anuncio

> **Importante:** para recibir `ttclid`, el anuncio debe ser de **tráfico /
> conversión web** y tener una URL de destino web. Si usas el formato nativo de
> **mensajería a WhatsApp**, TikTok no agrega `ttclid` ni permite una landing
> intermedia; en ese caso solo puedes hacer postback de calidad de lead (modo
> `crm`) usando REF, email o teléfono, sin atribución por clic. Para atribución
> por `ttclid`, cambia el destino a este puente.

1. En el anuncio, usa como destino:

   ```
   https://track.tudominio.com/r
   ```

   No uses `wa.me` directo si quieres atribución por clic.

2. Agrega UTMs en el anuncio (TikTok no las agrega solo):

   ```
   utm_source=tiktok&utm_medium=paid&utm_campaign=nombre_campana&utm_content=nombre_anuncio
   ```

3. No agregues `ttclid` manualmente. TikTok lo añade al ser una landing web.
4. Haz una prueba real: publica el anuncio, ábrelo desde la app de TikTok y
   revisa en el panel el clic y el `ttclid`. La vista previa del anuncio **no**
   siempre genera un clic real.

### Probar qué manda TikTok sin guardar

```bash
curl "https://track.tudominio.com/r?dry_run=1&ttclid=prueba&utm_source=tiktok"
```

Devuelve el `ref`, el `ttclid` recibido y la URL de WhatsApp, sin crear el clic.

---

## 7. Cómo se usa el REF

El puente redirige a:

```
https://wa.me/573001234567?text=Hola%2C%20quiero%20informaci%C3%B3n.%20REF-ABCD2345
```

El cliente envía ese mensaje. Kommo dispara `add_message`; el servicio busca
`REF-ABCD2345`, lo vincula con el `ttclid` guardado y sigue el embudo con los
`status_lead`.

Puedes personalizar el mensaje en **Configuración → WhatsApp → Mensaje prellenado**:

```text
Hola, vengo de TikTok. Quiero información. {ref}
```

`{ref}` es obligatorio. Si lo omites, el servicio lo agrega al final.

---

## 8. Panel y API

| Ruta | Descripción |
|---|---|
| `GET /` | Panel (Basic Auth) |
| `GET /settings` | Configuración de APIs y datos (Basic Auth) |
| `POST /settings` | Guardar configuración (CSRF + Basic Auth) |
| `GET /r` | Puente de atribución |
| `POST /webhook/kommo` | Webhook de Kommo (secreto por query o header) |
| `POST /webhook/kommo/{secret}` | Variante con el secreto en la ruta |
| `GET /api/health` | Salud + errores de configuración |
| `GET /api/stats` | Métricas (auth) |
| `GET /api/pipelines` | Pipelines/etapas de Kommo (auth) |
| `GET /api/clicks/{code}` | Detalle de un clic (auth) |
| `POST /admin/outbox/{id}/retry` | Reintentar evento (auth) |
| `POST /admin/leads/{id}/sync` | Releer lead desde Kommo (auth) |
| `POST /admin/test-event` | Encolar evento sintético (auth) |

---

## 9. Seguridad y datos personales

- Contraseña del panel y secreto de webhook son obligatorios.
- Los tokens de Kommo/TikTok y el secreto del webhook se guardan cifrados en
  PostgreSQL con `SECRET_KEY`; el panel nunca los devuelve en el HTML.
- La base de datos no está expuesta a internet.
- Email y teléfono se envían a TikTok **hasheados SHA-256** y normalizados.
- `ttclid`, `_ttp`, IP y user-agent van sin cifrar: TikTok los usa para emparejar.
- `STORE_RAW_PAYLOADS=true` guarda el payload crudo de Kommo (útil para depurar).
  Contiene datos personales; pon una retención/limpieza si tu política lo exige.
- Respeta el consentimiento: configura `KOMMO_FIELD_CONSENT` y el servicio no
  envía eventos si el valor es `no`, `false`, `0`, `negado` o `rechazado`.
- Rota `KOMMO_TOKEN`, `TIKTOK_ACCESS_TOKEN`, `KOMMO_WEBHOOK_SECRET` y
  `DASHBOARD_PASSWORD` si se filtran.
- El panel no muestra tokens; solo indica si están configurados.

---

## 10. Operación

### Logs

```bash
docker compose logs -f app
```

### Reiniciar tras cambiar `.env`

```bash
docker compose up -d --force-recreate
```

### Actualizar código

```bash
git pull
docker compose up -d --build
```

### Backup

Guarda también `.env` (contiene `SECRET_KEY`) y el volumen `appdata`:

```bash
docker compose exec db pg_dump -U attribution attribution > backup_$(date +%F).sql
cp .env backup_env_$(date +%F).txt
```

Sin `SECRET_KEY` los secretos de `/settings` no se pueden descifrar.

### Restaurar

```bash
cat backup.sql | docker compose exec -T db psql -U attribution -d attribution
```

### Smoke test

```bash
./scripts/smoke.sh
```

### Pruebas unitarias

```bash
python3 -m pip install --break-system-packages -r requirements-dev.txt
python3 -m pytest -q
ruff check app tests
```

---

## 11. Diagnóstico rápido

| Síntoma | Causa probable | Solución |
|---|---|---|
| El panel no abre / 502 | Traefik no ve el contenedor o el DNS no apunta al VPS | `docker compose logs -f`, `docker network inspect traefik-proxy` |
| Webhook 401 | Secreto distinto | Revisa **Configuración → Kommo → Secreto del webhook** y la URL en Kommo |
| No se encolan leads | Faltan eventos en Kommo o el secreto está mal | Panel → Webhooks; activa `add_lead`/`status_lead`/`add_message` |
| Lead sin evento | Su `status_id` no está en el mapa de etapas o su `max_rank` ya es mayor | Revisa **Configuración → Mapa de etapas** y el rank |
| Evento en `dead` | Token inválido, parámetro rechazado o 4xx de TikTok | Mira `last_error`, corrige y pulsa “Reintentar” |
| TikTok `40001` | Access token inválido | Genera otro token en Events Manager |
| TikTok `40100` | Rate limit | El worker reintenta con backoff |
| Clic sin `ttclid` | El destino no era una landing web o se usó `wa.me` directo | Usa `https://track.../r`; prueba con un clic real |
| No aparecen eventos en TikTok | Test event code activo, modo equivocado o token sin permiso | Revisa Test Events y **Configuración → TikTok** |
| Certificado inválido | Traefik no emitió TLS | Espera, reinicia Traefik y revisa el resolver `letsencrypt` |

### Ver qué manda TikTok realmente

1. Publica un anuncio de prueba con destino `https://track.tudominio.com/r`.
2. Ábrelo desde la app (no desde la vista previa).
3. En el panel, sección **Clics del puente**, revisa el `ttclid`.
4. Si está vacío, TikTok no lo agregó. Revisa que el destino sea una URL web y que
   el anuncio no esté usando una plantilla que elimine parámetros.
5. Con `dry_run=1` puedes inspeccionar todos los parámetros sin crear el clic.

---

## 12. Sobre la IA

La IA no es necesaria para atribuir: la fuente de verdad es la **etapa real del lead
en Kommo**. Si ya tienes un bot/IA (por ejemplo `whatsapp-kommo-gemini`) que mueve
etapas según la conversación, este servicio se alimenta de esos cambios y envía los
eventos. La IA puede seguir clasificando, calificando y moviendo el embudo; este
servicio no la reemplaza ni depende de ella.

Más adelante se puede agregar:

- scoring de leads con IA antes de mapear “calificado”;
- múltiples pixels/event sets por campaña;
- limpieza automática de payloads crudos;
- alertas por Telegram cuando un evento queda `dead`.

---

## 13. Estructura

```
tiktok-kommo-attribution/
├── app/
│   ├── main.py           # rutas, webhook, puente, panel
│   ├── config.py         # variables de entorno y mapa de etapas
│   ├── db.py             # pool asyncpg
│   ├── migrations.py     # aplica migrations/*.sql
│   ├── worker.py         # cola de leads + outbox TikTok
│   ├── kommo.py          # cliente API Kommo
│   ├── tiktok.py         # hashing y Events API 2.0
│   ├── webhook_parse.py  # JSON y form-urlencoded de Kommo
│   ├── attribution.py    # clics y enlace con el lead
│   ├── refcode.py        # REF-XXXXXXXX
│   ├── pipeline.py       # regla de etapa más alta
│   ├── auth.py           # Basic Auth del panel
│   ├── settings_store.py # configuración cifrada en PostgreSQL
│   └── templates/{dashboard,settings}.html
├── migrations/001_init.sql
├── migrations/002_app_settings.sql
├── tests/
├── scripts/smoke.sh
├── install.sh            # instalación guiada en el VPS
├── Dockerfile
├── docker-compose.yml
└── .env.example
```
