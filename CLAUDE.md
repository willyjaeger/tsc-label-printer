# TSC Label Printer — Contexto del proyecto

App local para Windows que imprime etiquetas ZPL en una impresora TSC por Ethernet (TCP 9100),
con integración a la API de MercadoLibre Argentina.

## Stack
- **Backend**: Python + Flask (puerto 5050, `app.py`)
- **Frontend**: HTML/CSS/JS en un solo archivo (`index.html`), sin frameworks
- **Distribución**: PyInstaller `--onefile --noconsole` → `dist/TSC-Label-Printer.exe`
- **Config persistente**: `config.json` (gitignoreado — cada PC tiene el suyo)

## Correr en desarrollo
```bash
python app.py
# Abre http://localhost:5050 automáticamente
```

## Compilar .exe
```bash
build.bat
# O directamente:
python -m PyInstaller --onefile --noconsole --add-data "index.html;." \
  --hidden-import=flask --hidden-import=werkzeug \
  --hidden-import=tkinter --hidden-import=requests \
  --collect-all=requests --name TSC-Label-Printer app.py
```

## Arquitectura

### Paths
```python
# En modo frozen (.exe): archivos bundleados en sys._MEIPASS
# En desarrollo: directorio del script
BUNDLE_DIR = sys._MEIPASS if frozen else os.path.dirname(__file__)
CONFIG_DIR  = os.path.dirname(sys.executable) if frozen else BUNDLE_DIR
```

### Endpoints Flask
| Método | Ruta | Descripción |
|--------|------|-------------|
| GET | `/` | Sirve `index.html` |
| GET/POST | `/config` | Configuración de impresora y credenciales ML |
| POST | `/print` | Imprime ZPL crudo (archivo drag-drop). Devuelve `{ok, labels}` |
| POST | `/calibrate` | Aplica `^MNA`, `^LL`, `^LT` según config |
| POST | `/autocal` | Envía `~JC` (calibración automática TSC) |
| POST | `/retract` | Envía `BACKFEED {dots}` para retroceder papel |
| POST | `/testprint` | Imprime etiqueta de test con medidas |
| GET | `/auth/login` | Inicia OAuth PKCE con ML |
| GET | `/auth/callback` | Recibe el code de ML y guarda tokens |
| GET | `/auth/status` | Estado de login ML |
| POST | `/auth/logout` | Borra tokens |
| GET | `/ml/orders` | Lista pedidos no-Fulfillment (ready_to_ship + paid + shipped de hoy) |
| POST | `/ml/print/<id>` | Imprime etiqueta ML + etiqueta de detalle. Devuelve `{ok, labels}` |
| POST | `/ml/print-all` | Imprime todos los pedidos pendientes. Devuelve `{ok, printed, labels, failed}` |
| GET | `/ml/events` | SSE stream: `poll_status`, `new_orders`, `auto_printed`, `print_error` |
| GET/POST | `/ml/autoprint` | Estado y configuración del polling automático |
| GET | `/ml/zpl/<id>` | Descarga ZPL sin imprimir (diagnóstico) |
| GET | `/ml/debug-orders` | Muestra conteo por estado (diagnóstico) |
| GET | `/tn/auth/status` | Estado de login TiendaNube |
| GET | `/tn/auth/login` | Inicia OAuth TN (no PKCE) |
| GET | `/tn/auth/callback` | Recibe el code de TN y guarda tokens |
| POST | `/tn/auth/logout` | Borra token TN |
| GET | `/tn/orders` | Lista pedidos TN abiertos y pagados |
| POST | `/tn/print/<order_id>` | Imprime etiqueta Andreani de un pedido TN. Retorna `{ok, labels}` |
| POST | `/tn/print-all` | Imprime todos los pedidos TN indicados. Retorna `{ok, printed, labels, failed}` |

### Comandos de impresora
- **ZPL**: `^XA...^XZ` — etiquetas normales
- **`~JC`** — auto-calibración (detecta gap avanzando etiquetas)
- **`^MNA/G/T`** — tipo de media (Gap/Mark/Continuous)
- **`^LL{dots}`** — largo de etiqueta
- **`^LT{dots}`** — backfeed offset estático
- **`BACKFEED {dots}`** — retrocede N dots (comando TSPL nativo TSC)
- **`FORMFEED`** — avanza una etiqueta

### OAuth MercadoLibre (PKCE)
- Redirect URI: `https://willyjaeger.github.io/tsc-label-printer/callback.html`
- `docs/callback.html` en GitHub Pages reenvía al localhost
- PKCE: `code_verifier` almacenado en `_pkce_store[state]` durante el flujo
- Tokens guardados en `config.json`: `ml_access_token`, `ml_refresh_token`, `ml_token_expires_at`
- Auto-refresh cuando quedan < 5 min

#### App compartida de MercadoLibre (`ml_auth_mode`)
Para que el cliente no tenga que crear su propia app en developers.mercadolibre.com.ar —
`ml_auth_mode: 'own_app'` (default, sin cambios) | `'shared'` (usa la app de EnvioBot).
- `ML_SHARED_CLIENT_ID` (app.py): client_id público de la app compartida — embebido sin problema,
  viaja igual en la URL del navegador en cada login.
- `_ml_shared_token_request(payload_extra)`: mismo patrón que `_license_activate`/`_license_validate`
  (`POST {_LICENSE_SERVER}/ml_token.php` con `{secret, key, fingerprint, **payload_extra}`).
- `backend/ml_token.php` (servidor): mismo gate que `validate.php` (licencia activada, fingerprint
  coincide, OWNER siempre pasa) + intermediario real con `POST api.mercadolibre.com/oauth/token`
  usando `ML_SHARED_CLIENT_ID`/`ML_SHARED_CLIENT_SECRET` (esta última SOLO en `config.php` del
  servidor, nunca en el `.exe` — MercadoLibre exige `client_secret` en el intercambio de token
  aunque se use PKCE). Responde `{ok:false, reason:'...'}` si falla el gate de licencia (forma
  distinta a un error de ML, para no confundir "se venció la suscripción" con "hay que reconectar
  la cuenta ML"), o la respuesta de ML tal cual (pass-through) si pasa.
- `auth_login()`/`auth_callback()`/`_refresh_token()` ramifican por `ml_auth_mode` — el camino
  `own_app` de hoy (client_secret local en `config.json`) no se toca.

### Filtrado de pedidos ML
- Se consultan estados: `ready_to_ship`, `paid`, `shipped` (este último solo de hoy)
- Se obtiene `logistic_type` de cada envío en paralelo (`ThreadPoolExecutor`, 8 workers)
- Se filtran los `logistic_type == 'fulfillment'` (Full) — el vendedor no imprime esos
- Tipos visibles: `me2` (Flex), `cross_docking` / `xd_drop_off` (Correo/Colecta)

### Etiquetas ML
Cada pedido imprime **2 etiquetas consecutivas**:
1. **Etiqueta de envío**: ZPL de ML con correlativo `#NNN` inyectado (`_inject_correlative_into_zpl`)
2. **Etiqueta de detalle**: generada localmente (`_build_detail_zpl`) con código de barras, buyer, artículos

El correlativo (`#001`, `#002`...) se reinicia cada día a medianoche — se guarda en `config.json`
como `_correlative` + `_correlative_date`.

### Modo A4 (impresora normal, sin impresora térmica)
Alternativa a todo el camino ZPL/TSPL de arriba, para vendedores sin impresora térmica —
`output_mode: 'a4'` en vez de `'thermal'` (default). Cuando está activo:
- `_fetch_ml_label`/TN piden siempre el label como **PDF** (`force_pdf=True`), nunca ZPL.
- `label_image_for_a4()` renderiza ese PDF a una `PIL.Image` (reutiliza `_pdf_page_to_image`,
  la misma capa que usa `pdf_to_zpl`/`pdf_to_tspl`, sin empaquetar a 1bpp).
- `_build_a4_page()` compone una hoja A4 **vertical** a 300dpi (2480×3508px): la etiqueta se
  **rota 90°** (`Image.ROTATE_90`) antes de pegarla arriba — es angosta y alta por naturaleza, girada
  aprovecha todo el ancho de la hoja y sale mucho más grande. Debajo va el detalle del pedido.
  Para TiendaNube (`order_data=None`) no hay bloque de detalle ni rotación — la etiqueta de Andreani
  ya trae la dirección impresa por el correo, así que se deja sin girar y ocupa casi toda la hoja.
- `_build_detail_image()` dibuja el equivalente de `_build_detail_zpl`/`_build_detail_tspl`
  (correlativo grande, comprador, TODOS los artículos) con `PIL.ImageDraw` — **sin código de barras**
  (se sacó a pedido explícito: la etiqueta de ML de al lado ya tiene su propio QR, duplicarlo no
  aportaba nada). Tipografía grande a propósito (pensada para leerse sin anteojos). Siempre entra en
  **una sola hoja**: si el detalle no entra al tamaño normal, se redibuja más chico (parámetro `scale`,
  piso 70% para no volverse ilegible) en vez de pasar a una segunda hoja o cortar artículos.
- `print_a4_image()` imprime esa imagen vía `win32ui`/`PIL.ImageWin` (modo documento/GDI, no RAW),
  forzando papel A4 + orientación vertical por DEVMODE — no pasa por `print_raw()`, que sigue siendo
  100% ZPL/TSPL.
- `a4_printer_name` (config) es la impresora Windows elegida para este modo — separada de
  `windows_printer_name` (impresora térmica USB), así no se pisan entre sí al cambiar de modo.
- Con `output_mode == 'a4'`, `/print`, `/lt`, `/calibrate`, `/autocal` y `/testprint` responden
  `400` explícito (son conceptos 100% de impresora térmica: gap, backfeed, ZPL crudo).
- La auto-impresión en background (`_poll_worker`) tiene su propio camino de impresión, separado de
  `ml_print`/`ml_print_all` — también respeta `output_mode == 'a4'` (bug real que hubo y se corrigió:
  al principio solo se había ramificado el camino manual, no el automático).

### Auto-impresión (SSE + polling)
- Hilo daemon `_poll_worker` corre siempre en background
- Al activar: snapshot inicial (no imprime), luego detecta pedidos nuevos y los imprime sólo
- Eventos SSE en `/ml/events`: `poll_status`, `new_orders`, `auto_printed`, `print_error`
- Frontend: toggle en barra de auto-impresión, beep triple + notificación Windows al detectar nuevo pedido
- Intervalo configurable: 30s / 1min / 2min / 5min

### Selector de impresora (Configuración)
Antes eran dos decisiones combinadas (`output_mode` + `connection_type`, lenguaje técnico). Ahora es
una sola pregunta en `index.html` con tres opciones en criollo (radio `printerKind`: `a4` /
`thermal_usb` / `thermal_network`) que el JS mapea a los mismos dos campos de config de siempre —
`saveConfig()` deriva `output_mode`/`connection_type` desde `printerKind`, `init()` hace el camino
inverso. `/printer/test` prueba conectividad real para los tres casos (agregado el caso A4, que antes
no existía — solo cubría red/USB térmica).

### Versión de la app + anuncios
Primera versión formal: `APP_VERSION` en `app.py` (empezó en `1.0.0`). Hilo daemon
`_announcement_worker`, **aparte** de `_poll_worker` a propósito (ese no corre sin sesión de ML activa
+ monitoreo prendido, pero un aviso de versión nueva le tiene que llegar a cualquier usuario). Chequea
`backend/announcement.php` (lectura pública, tabla `app_announcement` de una sola fila) cada 1 hora,
cachea el resultado en memoria (`_cached_announcement`) — `/config` expone ese cache en cada pedido
(así el aviso no depende de "pescar" el evento SSE en el momento justo) y además empuja SSE
(`announcement`) + notificación de bandeja la primera vez que ve una versión más nueva en la sesión.
Frontend descarta el aviso por versión vía `localStorage`. Se carga/edita desde una sección nueva en
`admin.php` (`action=save_announcement`), separada de la gestión de licencias existente.

## Config relevante (`config.json`)
```json
{
  "ip": "192.168.1.x",
  "port": 9100,
  "output_mode": "thermal",
  "a4_printer_name": "",
  "label_height_mm": 150,
  "label_width_mm": 100,
  "backfeed_dots": 0,
  "media_type": "gap",
  "dpi": 203,
  "ml_auth_mode": "own_app",
  "ml_client_id": "...",
  "ml_client_secret": "...",
  "ml_access_token": "...",
  "ml_refresh_token": "...",
  "ml_token_expires_at": 0,
  "ml_user_id": "...",
  "_correlative": 5,
  "_correlative_date": "2026-05-22"
}
```

### TiendaNube Integration

#### OAuth TiendaNube
- No PKCE (a diferencia de ML)
- Auth URL: `https://www.tiendanube.com/apps/{client_id}/authorize?redirect_uri=...&state=...`
- Token URL: `https://www.tiendanube.com/apps/authorize/token`
- Redirect URI: `https://willyjaeger.github.io/tsc-label-printer/tn-callback.html`
- El `access_token` de TN **no expira** (no necesita refresh)
- El `user_id` del response OAuth es el `store_id` para la API

#### Flujo de etiqueta Andreani (Envío Nube)
1. `GET /v1/{store_id}/orders/{order_id}/fulfillment-orders` → ULID del fulfillment order
2. `POST https://cirrus.tiendanube.com/nuvem-envio/dispatches` con headers `x-access-token` + `x-store-id` → `{labelUrls: ["https://s3...pdf"]}`
3. Descargar PDF → `pdf_to_zpl()` → imprimir por TCP 9100

#### `pdf_to_zpl()` — Conversión PDF → ZPL
- Requiere `pymupdf` (fitz) + `Pillow`
- Renderiza a alta resolución con `fitz.Matrix`
- Escala preservando aspecto en canvas blanco del tamaño de la etiqueta
- Binariza: pixel < 128 → imprimir (ZPL bit 1)
- Genera `^GFA` con datos en hex
- Optimización: usa `numpy` si está disponible, sino fallback PIL

#### Config TN (`config.json`)
```json
{
  "tn_client_id": "...",
  "tn_client_secret": "...",
  "tn_access_token": "...",
  "tn_store_id": 6865327
}
```

## GitHub
- Repo: `https://github.com/willyjaeger/tsc-label-printer`
- GitHub Pages (`docs/callback.html`): relay HTTPS para el OAuth callback de ML
- Siempre commit + push juntos al terminar cambios
