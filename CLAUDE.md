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
- `_build_detail_image()` dibuja el equivalente de `_build_detail_zpl`/`_build_detail_tspl`
  (correlativo, comprador, artículos) con `PIL.ImageDraw` — el código de barras Code128 se genera
  con la librería `python-barcode` (no hay comando de impresora que lo dibuje por su cuenta).
- `_build_a4_page()` compone una hoja A4 a 300dpi (2480×3508px): etiqueta arriba, detalle abajo.
  Para TiendaNube (`order_data=None`) no hay bloque de detalle — la etiqueta de Andreani ya trae
  la dirección impresa por el correo, así que ocupa casi toda la hoja.
- `print_a4_image()` imprime esa imagen vía `win32ui`/`PIL.ImageWin` (modo documento/GDI, no RAW) —
  no pasa por `print_raw()`, que sigue siendo 100% ZPL/TSPL.
- `a4_printer_name` (config) es la impresora Windows elegida para este modo — separada de
  `windows_printer_name` (impresora térmica USB), así no se pisan entre sí al cambiar de modo.
- Con `output_mode == 'a4'`, `/print`, `/lt`, `/calibrate`, `/autocal` y `/testprint` responden
  `400` explícito (son conceptos 100% de impresora térmica: gap, backfeed, ZPL crudo).

### Auto-impresión (SSE + polling)
- Hilo daemon `_poll_worker` corre siempre en background
- Al activar: snapshot inicial (no imprime), luego detecta pedidos nuevos y los imprime sólo
- Eventos SSE en `/ml/events`: `poll_status`, `new_orders`, `auto_printed`, `print_error`
- Frontend: toggle en barra de auto-impresión, beep triple + notificación Windows al detectar nuevo pedido
- Intervalo configurable: 30s / 1min / 2min / 5min

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
