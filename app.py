from flask import Flask, request, jsonify, send_from_directory, redirect

# Primera vez que la app tiene un número de versión formal — todo lo de antes
# queda sin numerar, no importa. Se usa para avisar "hay una versión nueva"
# (ver _check_announcement) y se muestra en la UI para soporte remoto.
APP_VERSION = '1.0.0'

import socket
import json
import os
import sys
import threading
import webview
import time
import secrets
import hashlib
import base64
import zipfile
import io
from concurrent.futures import ThreadPoolExecutor
import queue as queue_module

try:
    import requests as http
except ImportError:
    http = None

# ── Path resolution ────────────────────────────────────────────────────────────
if getattr(sys, 'frozen', False):
    BUNDLE_DIR = sys._MEIPASS
    CONFIG_DIR = os.path.dirname(sys.executable)
else:
    BUNDLE_DIR = os.path.dirname(os.path.abspath(__file__))
    CONFIG_DIR = BUNDLE_DIR

CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.json')
ORDERS_FILE = os.path.join(CONFIG_DIR, 'orders.json')

# ── Logging a archivo ──────────────────────────────────────────────────────────
# Rastro mínimo para soporte remoto: "mandame el archivo enviobot.log" en vez de
# necesitar acceso a la PC del operador para ver qué pasó.
import logging
from logging.handlers import RotatingFileHandler

logger = logging.getLogger('enviobot')
logger.setLevel(logging.INFO)
_log_handler = RotatingFileHandler(
    os.path.join(CONFIG_DIR, 'enviobot.log'),
    maxBytes=1_000_000, backupCount=2, encoding='utf-8',
)
_log_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
logger.addHandler(_log_handler)

DEFAULT_CONFIG = {
    'ip': '192.168.1.100',
    'port': 9100,
    'connection_type': 'network',   # 'network' (TCP a IP:puerto) | 'usb' (impresora instalada en Windows)
    'windows_printer_name': '',     # nombre exacto tal como figura en Windows, solo si connection_type == 'usb'
    'label_language': 'zpl',        # 'zpl' (default, sin cambios) | 'tspl' (TSC nativo/Xprinter/Godex, camino aparte)
    'output_mode': 'thermal',       # 'thermal' (default, sin cambios) | 'a4' (hoja A4 en impresora normal)
    'a4_printer_name': '',          # impresora Windows para modo A4 — separada de windows_printer_name (USB térmica)
    'label_height_mm': 150,
    'label_width_mm': 100,
    'backfeed_dots': 0,
    'label_gap_mm': 10,
    'media_type': 'gap',
    'dpi': 203,
    'ml_label_type': 'standard',   # 'standard' = 100x150 (2 etiquetas) | 'combo' = 100x190 con troquel
    'ml_die_cut_mm': 40,           # altura del troquel en mm (solo para combo)
    'ml_client_id': '',
    'ml_client_secret': '',
    'tn_client_id': '',
    'tn_client_secret': '',
}

ML_AUTH_URL  = 'https://auth.mercadolibre.com.ar/authorization'
ML_TOKEN_URL = 'https://api.mercadolibre.com/oauth/token'
ML_API       = 'https://api.mercadolibre.com'
REDIRECT_URI = 'https://willyjaeger.github.io/tsc-label-printer/callback.html'

# ── Licencias ──────────────────────────────────────────────────────────────────
_LICENSE_SERVER = 'https://logax.com.ar/enviobot'
_LICENSE_SECRET = 'eb_9k4m2p7n1q8r5t3vw6x'
_LIC_CACHE_TTL  = 3600        # re-validar cada 1 hora
_LIC_GRACE_H    = 48          # horas offline permitidas antes de bloquear

# PKCE: almacena {state: code_verifier} durante el flujo OAuth (en memoria, vida corta)
_pkce_store = {}

# ── TiendaNube constants ───────────────────────────────────────────────────────
TN_API_BASE     = 'https://api.tiendanube.com/v1'
TN_TOKEN_URL    = 'https://www.tiendanube.com/apps/authorize/token'
TN_CIRRUS       = 'https://cirrus.tiendanube.com/nuvem-envio/dispatches'
TN_REDIRECT_URI = 'https://willyjaeger.github.io/tsc-label-printer/tn-callback.html'
TN_USER_AGENT   = 'EnvioBot/1.0 (guillermo.jaeger@gmail.com)'

_tn_state_store = {}   # state → True, para CSRF

# ── SSE / Auto-print state ──────────────────────────────────────────────────────
_sse_clients      = []           # una Queue por cada cliente SSE conectado
_sse_clients_lock = threading.Lock()

_poll = {
    'enabled':     False,
    'auto_print':  False,        # imprimir automáticamente al detectar pedidos nuevos
    'interval':    60,           # segundos entre verificaciones
    'last_check':  0.0,
    'checked_at':  0.0,
    'known_ids':   set(),        # IDs de pedidos ya vistos
    'initialized': False,        # True tras la primera pasada (sin imprimir)
    'status':      'idle',       # 'idle' | 'running' | 'error'
    'error':       '',
}
_poll_lock = threading.Lock()


def _pkce_pair():
    """Genera code_verifier y code_challenge (S256) para PKCE."""
    verifier  = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip('=')
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip('=')
    return verifier, challenge


# ── Config ─────────────────────────────────────────────────────────────────────

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding='utf-8') as f:
                return {**DEFAULT_CONFIG, **json.load(f)}
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def save_config(cfg):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2)


# ── Licencias ──────────────────────────────────────────────────────────────────

def _machine_fingerprint():
    """SHA256 de MAC + número de serie del volumen C:. Identifica la PC de forma única."""
    import ctypes, uuid
    mac = ':'.join(f'{(uuid.getnode() >> i) & 0xff:02x}' for i in range(0, 48, 8))
    serial = 0
    try:
        vol_serial = ctypes.c_ulong()
        ctypes.windll.kernel32.GetVolumeInformationW(
            'C:\\', None, 0, ctypes.byref(vol_serial), None, None, None, 0)
        serial = vol_serial.value
    except Exception:
        pass
    return hashlib.sha256(f'{mac}|{serial}'.encode()).hexdigest()


def _license_validate(force=False):
    """
    Valida la licencia contra el servidor.
    Devuelve (valid: bool, reason: str, lic_type: str).
    Usa caché de 1 hora. Grace period de 48 h si el servidor no responde.
    """
    cfg = load_config()
    key = cfg.get('license_key', '').strip().upper()
    if not key:
        return False, 'no_key', ''

    now = time.time()

    # Usar caché si es reciente y no se fuerza re-validación
    if not force:
        last_check = cfg.get('_lic_checked_at', 0)
        if (now - last_check) < _LIC_CACHE_TTL:
            return (cfg.get('_lic_valid', False),
                    cfg.get('_lic_reason', ''),
                    cfg.get('_lic_type', ''))

    fp = _machine_fingerprint()
    try:
        r = http.post(
            f'{_LICENSE_SERVER}/validate.php',
            json={'secret': _LICENSE_SECRET, 'key': key, 'fingerprint': fp},
            timeout=6,
        )
        data = r.json()
        valid  = bool(data.get('valid', False))
        reason = '' if valid else data.get('reason', 'invalid')
        ltype  = data.get('type', '')

        cfg['_lic_valid']      = valid
        cfg['_lic_reason']     = reason
        cfg['_lic_type']       = ltype
        cfg['_lic_checked_at'] = now
        if valid:
            cfg['_lic_last_ok'] = now
        save_config(cfg)
        return valid, reason, ltype

    except Exception:
        # Sin conexión: aplicar grace period
        last_ok   = cfg.get('_lic_last_ok', 0)
        hours_off = (now - last_ok) / 3600 if last_ok else 9999
        ltype     = cfg.get('_lic_type', '')

        if last_ok and hours_off < _LIC_GRACE_H:
            return True, 'offline_grace', ltype
        return False, 'server_unreachable', ''


def _license_activate(key):
    """
    Activa una license key en esta máquina (primera vez).
    Devuelve (ok: bool, reason: str, lic_type: str).
    """
    fp = _machine_fingerprint()
    try:
        r = http.post(
            f'{_LICENSE_SERVER}/activate.php',
            json={'secret': _LICENSE_SECRET, 'key': key.strip().upper(), 'fingerprint': fp},
            timeout=10,
        )
        data   = r.json()
        ok     = bool(data.get('ok', False))
        reason = data.get('reason', '') if not ok else ''
        ltype  = data.get('type', '')
        if ok:
            cfg = load_config()
            cfg['license_key']     = key.strip().upper()
            cfg['_lic_valid']      = True
            cfg['_lic_reason']     = ''
            cfg['_lic_type']       = ltype
            cfg['_lic_checked_at'] = time.time()
            cfg['_lic_last_ok']    = time.time()
            save_config(cfg)
        return ok, reason, ltype
    except Exception as e:
        return False, 'server_unreachable', ''


def _require_license():
    """Llama desde un endpoint de impresión. Devuelve None si ok, o jsonify de error."""
    valid, reason, ltype = _license_validate()
    if valid:
        return None
    messages = {
        'no_key':            'Ingresá tu licencia en Configuración para imprimir.',
        'expired':           'Tu suscripción EnvioBot venció. Contactá al vendedor para renovar.',
        'revoked':           'Esta licencia fue desactivada. Contactá al vendedor.',
        'wrong_machine':     'Esta licencia está activada en otra PC.',
        'not_found':         'Licencia no encontrada. Verificá la clave.',
        'server_unreachable':'Sin conexión al servidor de licencias (más de 48 h offline).',
    }
    msg = messages.get(reason, f'Licencia inválida ({reason}).')
    return jsonify({'ok': False, 'error': msg, 'license_error': True}), 403


# ── Orders persistence ─────────────────────────────────────────────────────────

def load_orders():
    if os.path.exists(ORDERS_FILE):
        try:
            with open(ORDERS_FILE, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return []

def save_orders(orders):
    with open(ORDERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(orders, f, indent=2, ensure_ascii=False)

def _save_printed_order(order_data):
    """Registra un pedido impreso en orders.json. Si ya existe, actualiza correlativo y fecha."""
    from datetime import datetime, timezone, timedelta
    tz = timezone(timedelta(hours=-3))
    now = datetime.now(tz).isoformat()

    shipment_id = int(order_data.get('shipment_id', 0) or 0)
    if not shipment_id:
        return

    orders = load_orders()
    for o in orders:
        if o.get('shipment_id') == shipment_id:
            o['correlative'] = order_data.get('correlative', o.get('correlative'))
            o['printed_at']  = now
            save_orders(orders)
            return

    orders.append({
        'id':                 int(order_data.get('order_id', 0) or 0),
        'shipment_id':        shipment_id,
        'correlative':        order_data.get('correlative'),
        'buyer':              order_data.get('buyer', ''),
        'address':            order_data.get('address', ''),
        'items':              order_data.get('items', []),
        'logistic_type':      order_data.get('logistic_type', ''),
        'printed_at':         now,
        'shipment_status':    'printed',
        'shipment_substatus': '',
        'status_checked_at':  None,
        'delivered_at':       None,
    })
    save_orders(orders)

def _sync_orders_in_transit(token):
    """Consulta ML para actualizar estados de envío. Purga entregados tras 24h. Devuelve lista."""
    from datetime import datetime, timezone, timedelta
    tz  = timezone(timedelta(hours=-3))
    now = datetime.now(tz)
    now_iso = now.isoformat()

    orders = load_orders()
    if not orders:
        return []

    changed = False

    # Purgar delivered/not_delivered con más de 24 h
    keep = []
    for o in orders:
        if o.get('shipment_status') in ('delivered', 'not_delivered') and o.get('delivered_at'):
            try:
                dt = datetime.fromisoformat(o['delivered_at'])
                if (now - dt).total_seconds() > 86400:
                    changed = True
                    continue
            except Exception:
                pass
        keep.append(o)
    orders = keep

    # Actualizar estados pendientes
    for o in orders:
        if o.get('shipment_status') in ('delivered', 'not_delivered'):
            continue
        sid = o.get('shipment_id')
        if not sid:
            continue
        try:
            r   = ml_get(f'/shipments/{sid}', token)
            d   = r.json()
            new_status    = d.get('status', '') or ''
            new_substatus = d.get('substatus', '') or ''
            if new_status != o.get('shipment_status', '') or new_substatus != o.get('shipment_substatus', ''):
                o['shipment_status']    = new_status
                o['shipment_substatus'] = new_substatus
                o['status_checked_at']  = now_iso
                if new_status in ('delivered', 'not_delivered'):
                    o['delivered_at'] = now_iso
                changed = True
        except Exception:
            pass

    if changed:
        save_orders(orders)

    return orders


# ── Printer ────────────────────────────────────────────────────────────────────

def send_to_printer(ip, port, data, retries=2, retry_delay=0.6):
    """Envía datos crudos a la impresora por TCP. Reintenta ante fallas transitorias
    de conexión (la impresora TSC en red a veces tarda en aceptar una conexión nueva
    justo después de la anterior) antes de dejar propagar la excepción al llamador."""
    if isinstance(data, str):
        data = data.encode('utf-8')
    last_err = None
    for attempt in range(retries + 1):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(10)
        try:
            s.connect((ip, int(port)))
            s.sendall(data)
            return
        except (socket.timeout, ConnectionRefusedError, OSError) as e:
            last_err = e
            if attempt < retries:
                logger.warning('send_to_printer: intento %d/%d falló (%s), reintentando…',
                                attempt + 1, retries + 1, e)
                time.sleep(retry_delay)
        finally:
            s.close()
    logger.error('send_to_printer: sin éxito tras %d intentos — %s', retries + 1, last_err)
    raise last_err


def send_to_printer_usb(printer_name, data, retries=1, retry_delay=0.6):
    """Envía datos crudos (ZPL/TSPL) a una impresora instalada en Windows —
    USB, LPT o de red ya agregada como impresora de Windows — usando el
    spooler en modo RAW: el driver reenvía los bytes tal cual, sin
    interpretarlos ni renderizarlos, igual que send_to_printer por TCP."""
    import win32print
    if isinstance(data, str):
        data = data.encode('utf-8')
    last_err = None
    for attempt in range(retries + 1):
        try:
            hPrinter = win32print.OpenPrinter(printer_name)
            try:
                hJob = win32print.StartDocPrinter(hPrinter, 1, ('EnvioBot', None, 'RAW'))
                try:
                    win32print.StartPagePrinter(hPrinter)
                    win32print.WritePrinter(hPrinter, data)
                    win32print.EndPagePrinter(hPrinter)
                finally:
                    win32print.EndDocPrinter(hPrinter)
            finally:
                win32print.ClosePrinter(hPrinter)
            return
        except Exception as e:
            last_err = e
            if attempt < retries:
                logger.warning('send_to_printer_usb: intento %d/%d falló (%s), reintentando…',
                                attempt + 1, retries + 1, e)
                time.sleep(retry_delay)
    logger.error('send_to_printer_usb: sin éxito tras %d intentos — %s', retries + 1, last_err)
    raise last_err


def print_raw(cfg, data):
    """Punto único de impresión: decide red (TCP a IP:puerto) o USB/impresora
    instalada en Windows según connection_type. Todo el pipeline de impresión
    (auto-print, manual, ML, TiendaNube) pasa siempre por acá — la rama de
    red es exactamente send_to_printer de siempre, sin cambios."""
    if cfg.get('connection_type') == 'usb':
        name = cfg.get('windows_printer_name', '')
        if not name:
            raise RuntimeError('No hay impresora USB seleccionada en Configuración.')
        send_to_printer_usb(name, data)
    else:
        send_to_printer(cfg['ip'], cfg['port'], data)


def query_printer(ip, port, cmd, read_bytes=512, timeout=5, retries=1, retry_delay=0.5):
    """Envía un comando y lee la respuesta del printer. Devuelve bytes o None
    (None = no se pudo conectar/leer nada; se deja registro en el log de la causa
    real para poder diferenciar impresora apagada de timeout de respuesta vacía)."""
    for attempt in range(retries + 1):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect((ip, int(port)))
            if isinstance(cmd, str):
                cmd = cmd.encode('utf-8')
            s.sendall(cmd)
            s.settimeout(2)
            chunks = []
            try:
                while True:
                    chunk = s.recv(read_bytes)
                    if not chunk:
                        break
                    chunks.append(chunk)
            except socket.timeout:
                pass
            return b''.join(chunks) if chunks else None
        except Exception as e:
            logger.warning('query_printer: intento %d/%d falló (%s)',
                            attempt + 1, retries + 1, e)
            if attempt < retries:
                time.sleep(retry_delay)
        finally:
            s.close()
    return None


def parse_hs_dimensions(hs_response, dpi=203):
    """
    Parsea la respuesta ASCII de ~HS del TSC (paquetes STX...ETX con campos CSV).
    Paquete 1, campo 3 (0-idx) = pitch total (etiqueta+gap) en dots.
    Paquete 2, campo 4 (0-idx) = gap en mm.
    Devuelve (height_mm, gap_mm); cada uno puede ser None.
    """
    if not hs_response:
        return None, None
    import re
    try:
        text    = hs_response.decode('ascii', errors='ignore')
        packets = re.findall(r'\x02([^\x03]*)\x03', text)
        if not packets:
            return None, None

        height_mm = None
        gap_mm    = None

        # Paquete 1, campo 3 = pitch total en dots
        pkt1 = packets[0].split(',')
        if len(pkt1) >= 4:
            pitch_dots = int(pkt1[3].strip())
            if pitch_dots > 0:
                pitch_mm = pitch_dots * 25.4 / dpi
                height_mm = pitch_mm   # se ajusta restando gap abajo

        # Paquete 2, campo 4 = gap en mm
        if len(packets) >= 2:
            pkt2 = packets[1].split(',')
            if len(pkt2) >= 5:
                g = float(pkt2[4].strip())
                if 1.0 <= g <= 20.0:
                    gap_mm = round(g, 1)

        # Largo etiqueta = pitch total − gap
        if height_mm is not None:
            height_mm = round(height_mm - (gap_mm or 0), 1)
            if not (20.0 <= height_mm <= 400.0):
                height_mm = None

        return height_mm, gap_mm
    except Exception:
        return None, None

def parse_hs_gap(hs_response):
    _, gap = parse_hs_dimensions(hs_response)
    return gap


# ── ML Auth helpers ────────────────────────────────────────────────────────────

# Estado del token ML en memoria: distingue "hay que reconectar la cuenta"
# (refresh token revocado/inválido) de "problema de red transitorio" — ambos
# casos antes colapsaban en el mismo None sin ninguna pista de la causa.
_ml_token_state = {'needs_reauth': False}


def get_valid_token():
    """Devuelve un access_token válido, refrescando si es necesario."""
    cfg = load_config()
    if not cfg.get('ml_access_token'):
        return None
    if time.time() > cfg.get('ml_token_expires_at', 0) - 300:
        return _refresh_token(cfg)
    return cfg['ml_access_token']


def _refresh_token(cfg):
    if not http:
        return None
    try:
        r = http.post(ML_TOKEN_URL, data={
            'grant_type':    'refresh_token',
            'client_id':     cfg.get('ml_client_id', ''),
            'client_secret': cfg.get('ml_client_secret', ''),
            'refresh_token': cfg.get('ml_refresh_token', ''),
        }, timeout=15)
        data = r.json()
        if 'access_token' not in data:
            error = data.get('error', '')
            if error in ('invalid_grant', 'invalid_client', 'unauthorized_client'):
                _ml_token_state['needs_reauth'] = True
                logger.warning('ML refresh_token rechazado (%s) — la cuenta necesita reconectarse', error)
            else:
                logger.warning('ML refresh_token: respuesta sin access_token: %s', data)
            return None
        _ml_token_state['needs_reauth'] = False
        cfg['ml_access_token']    = data['access_token']
        cfg['ml_refresh_token']   = data.get('refresh_token', cfg.get('ml_refresh_token'))
        cfg['ml_token_expires_at'] = time.time() + data.get('expires_in', 21600)
        save_config(cfg)
        return cfg['ml_access_token']
    except Exception as e:
        logger.warning('ML refresh_token: error de red — %s', e)
        return None


def ml_get(path, token, **kwargs):
    """GET a ML API con token. Lanza excepción en error."""
    return http.get(
        ML_API + path,
        headers={'Authorization': f'Bearer {token}'},
        timeout=15,
        **kwargs
    )


# ── SSE helpers ───────────────────────────────────────────────────────────────

def _push_event(event_type, data):
    """Envía un evento SSE a todos los clientes conectados."""
    msg = f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    with _sse_clients_lock:
        for q in list(_sse_clients):
            try:
                q.put_nowait(msg)
            except queue_module.Full:
                pass


# ── Anuncios (versión nueva / mensajes a todos los clientes) ───────────────────
# Hilo aparte del polling de ML a propósito: _poll_worker no hace nada sin
# sesión de ML activa Y monitoreo prendido, pero un aviso de "hay versión
# nueva" le tiene que llegar a CUALQUIER usuario, tenga o no ML conectado.

_ANNOUNCE_CHECK_INTERVAL = 3600  # 1 hora

# Cache en memoria del anuncio activo — así /config no le pega a la red en
# cada pedido, solo lee lo que este hilo actualizó. None = no hay nada
# activo (o la versión del servidor no es más nueva que la instalada).
_cached_announcement = None
_announce_notified_version = None  # para no repetir el toast/bandeja en la misma sesión


def _version_gt(a, b):
    """Compara dos versiones tipo '1.2.0' — True si a > b."""
    def parts(v):
        return tuple(int(x) if x.isdigit() else 0 for x in str(v).split('.'))
    return parts(a) > parts(b)


def _refresh_announcement_cache():
    """Actualiza el cache en memoria del anuncio activo desde el servidor.
    /config expone este cache en cada pedido — así el aviso no depende de
    "pescar" el evento SSE en el momento justo: si la app se cerró y se
    volvió a abrir, lo ve apenas carga. Además, la primera vez que aparece
    una versión nueva durante esta sesión, empuja SSE + notificación de
    bandeja para no depender de que el usuario recargue la página."""
    global _cached_announcement, _announce_notified_version
    if not http:
        return
    try:
        r = http.post(f'{_LICENSE_SERVER}/announcement.php',
                       json={'secret': _LICENSE_SECRET}, timeout=8)
        data = r.json()
    except Exception:
        return
    if not data.get('ok'):
        return

    latest = data.get('latest_version') or APP_VERSION
    active = bool(data.get('active')) and _version_gt(latest, APP_VERSION)

    if not active:
        _cached_announcement = None
        return

    _cached_announcement = {
        'version':      latest,
        'message':      data.get('message') or f'Hay una nueva versión de EnvioBot ({latest}) disponible.',
        'download_url': data.get('download_url') or '',
    }

    if _announce_notified_version != latest:
        _announce_notified_version = latest
        _push_event('announcement', _cached_announcement)
        tray_notify('EnvioBot — Nueva versión disponible', _cached_announcement['message'])


def _announcement_worker():
    """Hilo daemon independiente del polling de ML — chequea periódicamente
    sin depender de si hay sesión de MercadoLibre ni de si el monitoreo de
    auto-impresión está activado."""
    while True:
        try:
            _refresh_announcement_cache()
        except Exception:
            logger.exception('_announcement_worker: fallo en el chequeo')
        time.sleep(_ANNOUNCE_CHECK_INTERVAL)


# ── Background polling worker ─────────────────────────────────────────────────

def _poll_worker():
    """Hilo daemon: verifica pedidos nuevos y los imprime automáticamente."""
    while True:
        time.sleep(1)

        with _poll_lock:
            if not _poll['enabled']:
                continue
            interval   = _poll['interval']
            last_check = _poll['last_check']

        if time.time() - last_check < interval:
            continue

        # ── Hora de verificar ──────────────────────────────────────────────
        token = get_valid_token()
        if not token:
            now = time.time()
            msg = ('Tu sesión de MercadoLibre expiró — reconectá la cuenta en la pestaña ML.'
                   if _ml_token_state.get('needs_reauth')
                   else 'Sin conexión con MercadoLibre, reintentando…')
            with _poll_lock:
                _poll['status']     = 'error'
                _poll['error']      = msg
                _poll['last_check'] = now
                _poll['checked_at'] = now
            _push_event('poll_status', {'status': 'error',
                                        'error':  msg,
                                        'checked_at': now})
            continue

        with _poll_lock:
            _poll['status'] = 'running'
        _push_event('poll_status', {'status': 'running', 'checked_at': time.time()})

        try:
            cfg     = load_config()
            user_id = cfg.get('ml_user_id')
            if not user_id:
                r = http.get(ML_API + '/users/me',
                             headers={'Authorization': f'Bearer {token}'}, timeout=10)
                user_id = r.json().get('id')
                cfg['ml_user_id'] = user_id
                save_config(cfg)

            from datetime import datetime, timezone, timedelta
            tz_arg = timezone(timedelta(hours=-3))

            all_orders, seen = [], set()
            for status in ('ready_to_ship', 'paid'):
                r = http.get(ML_API + '/orders/search',
                             headers={'Authorization': f'Bearer {token}'},
                             params={'seller': user_id, 'order.status': status,
                                     'sort': 'date_desc', 'limit': 50},
                             timeout=15)
                for o in r.json().get('results', []):
                    if o['id'] not in seen:
                        o['_status_label'] = status
                        all_orders.append(o)
                        seen.add(o['id'])

            # Detalles de envío en paralelo + filtrar fulfillment
            ship_ids = [o.get('shipping', {}).get('id') for o in all_orders]
            ship_ids = [s for s in ship_ids if s]
            shipment_data = {}
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = {pool.submit(_fetch_shipment, sid, token): sid for sid in ship_ids}
                for fut in futures:
                    sid, data = fut.result()
                    shipment_data[sid] = data

            printable = []
            for o in all_orders:
                if o.get('cancel_detail'):
                    continue
                status_detail = o.get('status_detail', '') or ''
                if 'cancel' in status_detail.lower():
                    continue
                sid  = o.get('shipping', {}).get('id')
                info = shipment_data.get(sid, {})
                if info.get('logistic_type') == 'fulfillment':
                    continue
                o['_shipment'] = info
                printable.append(o)

            # Agrupar por shipment_id (mismo fix que en /ml/orders)
            from collections import defaultdict as _dd2
            _sg = _dd2(list)
            for o in printable:
                _k = str(o.get('shipping', {}).get('id') or f'_ns_{o["id"]}')
                _sg[_k].append(o)
            _merged = []
            for _k, _grp in _sg.items():
                if len(_grp) == 1:
                    _merged.append(_grp[0])
                else:
                    _base = dict(_grp[0])
                    _base['order_items'] = [i for o in _grp for i in (o.get('order_items') or [])]
                    _base['_merged_order_ids'] = [o['id'] for o in _grp]
                    _merged.append(_base)
            printable = _merged

            current_ids = {o['id'] for o in printable}
            now = time.time()

            with _poll_lock:
                was_initialized  = _poll['initialized']
                known_ids        = _poll['known_ids'].copy()
                _poll['known_ids']   = current_ids
                _poll['initialized'] = True
                _poll['last_check']  = now
                _poll['checked_at']  = now
                _poll['status']      = 'idle'
                _poll['error']       = ''

            # IDs de shipments ya impresos (persiste entre reinicios del servidor)
            printed_ship_ids = {str(o.get('shipment_id')) for o in load_orders()}
            new_orders = [
                o for o in printable
                if o['id'] not in known_ids
                and str(o.get('shipping', {}).get('id', '')) not in printed_ship_ids
            ]

            # Detectar pedidos impresos que desaparecieron (posible cancelación)
            if was_initialized:
                disappeared = known_ids - current_ids
                if disappeared:
                    local = load_orders()
                    for saved in local:
                        oid = saved.get('order_id')
                        if oid and int(oid) in disappeared:
                            ship_st = saved.get('shipment_status', '')
                            if ship_st not in ('shipped', 'delivered', 'not_delivered', 'handling'):
                                _push_event('possible_cancel', {
                                    'order_id':    str(oid),
                                    'shipment_id': saved.get('shipment_id', ''),
                                    'buyer':       saved.get('buyer', ''),
                                })
                                tray_notify(
                                    '⚠️ Posible cancelación',
                                    f'Pedido #{oid} ({saved.get("buyer","")}) ya no está en ML — NO despachar',
                                )

            with _poll_lock:
                do_auto_print = _poll['auto_print']

            if not was_initialized:
                # Primera pasada: solo registrar IDs existentes, no imprimir
                _push_event('poll_status', {
                    'status': 'idle', 'checked_at': now,
                    'initialized': True, 'count': len(printable),
                })
            elif new_orders:
                # Pedidos nuevos detectados → siempre notificar (SSE + tray)
                _push_event('new_orders', {
                    'count':      len(new_orders),
                    'checked_at': now,
                    'auto_print': do_auto_print,
                    'orders': [{'id': o['id'],
                                'shipment_id': o.get('shipping', {}).get('id')}
                               for o in new_orders],
                })
                n = len(new_orders)
                buyer_preview = ''
                first_ship = new_orders[0].get('_shipment') or {}
                buyer_preview = first_ship.get('receiver_name') or \
                                (new_orders[0].get('buyer') or {}).get('nickname', '')
                msg = f'{buyer_preview}' if n == 1 else f'{n} pedidos nuevos'
                tray_notify('Pedido nuevo en ML', msg + ' — Hacé click para ver')
                if do_auto_print:
                    want_a4 = cfg.get('output_mode') == 'a4'
                    for o in new_orders:
                        sid = o.get('shipping', {}).get('id')
                        if not sid:
                            continue
                        buyer_name = (o.get('_shipment') or {}).get('receiver_name') \
                                     or (o.get('buyer') or {}).get('nickname', '')
                        try:
                            zpl, is_zpl, err = _fetch_ml_label(sid, token, cfg, force_pdf=want_a4)
                            if err:
                                logger.warning('auto-print: shipment %s (%s) — %s', sid, buyer_name, err)
                                _push_event('print_error', {
                                    'shipment_id': sid, 'order_id': o['id'],
                                    'buyer': buyer_name, 'error': err,
                                })
                                tray_notify(
                                    '⚠️ Auto-impresión falló',
                                    f'Pedido {buyer_name or ("#" + str(o["id"]))} no se pudo imprimir: {err}',
                                )
                                continue
                            order_data = {
                                'order_id':    o['id'],
                                'shipment_id': str(sid),
                                'buyer':       buyer_name,
                                'items':       [
                                    {'qty': i['quantity'],
                                     'title': (i.get('item') or {}).get('title', '')}
                                    for i in o.get('order_items', [])
                                ],
                            }
                            corr = next_correlative()
                            order_data['correlative'] = corr
                            if want_a4:
                                label_img = label_image_for_a4(
                                    zpl, width_mm=float(cfg.get('label_width_mm', 100)),
                                    height_mm=float(cfg.get('label_height_mm', 150)),
                                    dpi=int(cfg.get('dpi', 203)))
                                page = _build_a4_page(label_img, order_data, cfg)
                                print_a4_image(cfg.get('a4_printer_name'), page)
                            else:
                                payload, _ = _print_ml_order(zpl, is_zpl, order_data, cfg)
                                print_raw(cfg, payload)
                            _save_printed_order(order_data)
                            _push_event('auto_printed', {
                                'shipment_id': sid,
                                'order_id':    o['id'],
                                'buyer':       order_data['buyer'],
                            })
                        except Exception as pe:
                            logger.exception('auto-print: excepción imprimiendo shipment %s (%s)', sid, buyer_name)
                            _push_event('print_error', {
                                'shipment_id': sid, 'order_id': o['id'],
                                'buyer': buyer_name, 'error': str(pe),
                            })
                            tray_notify(
                                '⚠️ Auto-impresión falló',
                                f'Pedido {buyer_name or ("#" + str(o["id"]))} no se pudo imprimir: {pe}',
                            )

                _push_event('poll_status', {
                    'status': 'idle', 'checked_at': now, 'count': len(printable),
                })
            else:
                _push_event('poll_status', {
                    'status': 'idle', 'checked_at': now, 'count': len(printable),
                })

            # Actualizar estados de envíos en tránsito
            try:
                updated = _sync_orders_in_transit(token)
                _push_event('orders_sync', {'orders': updated})
            except Exception:
                pass

        except Exception as e:
            now = time.time()
            logger.exception('_poll_worker: fallo en el ciclo de polling')
            with _poll_lock:
                _poll['status']     = 'error'
                _poll['error']      = str(e)
                _poll['last_check'] = now
                _poll['checked_at'] = now
            _push_event('poll_status', {
                'status': 'error', 'error': str(e), 'checked_at': now,
            })


# ── Flask ──────────────────────────────────────────────────────────────────────

app = Flask(__name__)


# ── Static ─────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    resp = send_from_directory(BUNDLE_DIR, 'index.html')
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


@app.route('/internal/show', methods=['POST'])
def internal_show():
    """Solo accesible en 127.0.0.1: le pide a ESTA instancia que muestre su
    ventana. Lo usa un segundo intento de abrir el .exe para no duplicar la
    app — en vez de arrancar un proceso nuevo, hace que el que ya está
    corriendo (monitoreando o no) se muestre, igual que tocar el ícono de
    la bandeja."""
    if _webview_window:
        _webview_window.show()
    return jsonify({'ok': True})


# ── Printer config ─────────────────────────────────────────────────────────────

@app.route('/config', methods=['GET'])
def get_config():
    cfg = load_config()
    # No exponer tokens ni campos internos al frontend
    _hidden = {'ml_access_token', 'ml_refresh_token', 'ml_token_expires_at', 'tn_access_token', 'tn_store_id'}
    safe = {k: v for k, v in cfg.items() if k not in _hidden and not k.startswith('_')}
    safe['app_version'] = APP_VERSION
    safe['announcement'] = _cached_announcement
    return jsonify(safe)


@app.route('/config', methods=['POST'])
def post_config():
    try:
        cfg = load_config()
        cfg.update(request.get_json())
        save_config(cfg)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/printers/list')
def printers_list():
    """Impresoras instaladas en Windows (para elegir modo USB) — cualquiera
    que Windows vea, sea por USB, LPT o red agregada como impresora local."""
    try:
        import win32print
        printers = win32print.EnumPrinters(
            win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS)
        names = sorted({p[2] for p in printers})
        try:
            default = win32print.GetDefaultPrinter()
        except Exception:
            default = None
        return jsonify({'ok': True, 'printers': names, 'default': default})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/printer/test', methods=['POST'])
def printer_test():
    """Prueba de conectividad real, sin imprimir nada — para que 'Guardar' en
    Configuración deje de ser un acto de fe. Usa los valores del formulario
    tal como están (aunque todavía no se hayan guardado), con la config ya
    guardada como respaldo si no vienen en el pedido."""
    body = request.get_json(silent=True) or {}
    cfg = load_config()
    output_mode = body.get('output_mode') or cfg.get('output_mode', 'thermal')

    if output_mode == 'a4':
        name = body.get('a4_printer_name') or cfg.get('a4_printer_name', '')
        if not name:
            return jsonify({'ok': False, 'error': 'Elegí una impresora de la lista primero.'})
        try:
            import win32print
            h = win32print.OpenPrinter(name)
            win32print.ClosePrinter(h)
            return jsonify({'ok': True, 'detail': f'"{name}" responde.'})
        except Exception as e:
            return jsonify({'ok': False, 'error': f'No se pudo abrir "{name}": {e}'})

    conn_type = body.get('connection_type') or cfg.get('connection_type', 'network')

    if conn_type == 'usb':
        name = body.get('windows_printer_name') or cfg.get('windows_printer_name', '')
        if not name:
            return jsonify({'ok': False, 'error': 'Elegí una impresora de la lista primero.'})
        try:
            import win32print
            h = win32print.OpenPrinter(name)
            win32print.ClosePrinter(h)
            return jsonify({'ok': True, 'detail': f'"{name}" responde.'})
        except Exception as e:
            return jsonify({'ok': False, 'error': f'No se pudo abrir "{name}": {e}'})

    ip   = (body.get('ip') or cfg.get('ip', '')).strip()
    port = int(body.get('port') or cfg.get('port', 9100))
    if not ip:
        return jsonify({'ok': False, 'error': 'Ingresá una IP primero.'})
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3)
    try:
        s.connect((ip, port))
        return jsonify({'ok': True, 'detail': f'{ip}:{port} responde.'})
    except socket.timeout:
        return jsonify({'ok': False, 'error': f'{ip}:{port} no respondió (timeout) — ¿está prendida y en la misma red?'})
    except ConnectionRefusedError:
        return jsonify({'ok': False, 'error': f'{ip}:{port} rechazó la conexión — revisá el puerto.'})
    except Exception as e:
        return jsonify({'ok': False, 'error': f'{ip}:{port} — {e}'})
    finally:
        s.close()


# ── Endpoints de licencia ──────────────────────────────────────────────────────

@app.route('/license/status')
def license_status():
    cfg   = load_config()
    key   = cfg.get('license_key', '').strip()
    valid, reason, ltype = _license_validate()
    return jsonify({
        'key':    key[:4] + '-****-****-' + key[-4:] if len(key) == 19 else '',
        'valid':  valid,
        'reason': reason,
        'type':   ltype,
    })


@app.route('/license/activate', methods=['POST'])
def license_activate():
    data = request.get_json(silent=True) or {}
    key  = data.get('key', '').strip()
    if not key:
        return jsonify({'ok': False, 'error': 'Ingresá una clave de licencia.'}), 400
    ok, reason, ltype = _license_activate(key)
    if ok:
        return jsonify({'ok': True, 'type': ltype})
    messages = {
        'not_found':     'Clave no encontrada. Verificá que esté bien escrita.',
        'revoked':       'Esta licencia fue desactivada.',
        'expired':       'Esta licencia está vencida.',
        'wrong_machine': 'Esta licencia ya está activada en otra PC. Contactá al vendedor.',
        'server_unreachable': 'No se pudo conectar al servidor. Verificá tu internet.',
    }
    return jsonify({'ok': False, 'error': messages.get(reason, f'Error: {reason}')}), 400


# ── Print (archivo manual) ─────────────────────────────────────────────────────

@app.route('/print', methods=['POST'])
def print_label():
    err = _require_license()
    if err: return err
    cfg = load_config()
    if cfg.get('output_mode') == 'a4':
        return jsonify({'ok': False, 'error':
            'No disponible en modo Hoja A4 — subir ZPL/TSPL crudo requiere impresora térmica.'}), 400
    raw = request.get_data()
    if not raw:
        return jsonify({'ok': False, 'error': 'Archivo vacío'}), 400

    # Si es un ZIP (magic bytes PK), extraer el primer archivo
    if raw[:2] == b'PK':
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                names = zf.namelist()
                if not names:
                    return jsonify({'ok': False, 'error': 'El ZIP está vacío'}), 400
                raw = zf.read(names[0])
        except zipfile.BadZipFile:
            return jsonify({'ok': False, 'error': 'Archivo ZIP inválido'}), 400

    n_labels = count_labels(raw)
    try:
        print_raw(cfg, raw)
        return jsonify({'ok': True, 'labels': n_labels})
    except socket.timeout:
        return jsonify({'ok': False, 'error': f"Timeout: no se pudo conectar a {cfg['ip']}:{cfg['port']}"}), 500
    except ConnectionRefusedError:
        return jsonify({'ok': False, 'error': 'Conexión rechazada: verificar que la impresora esté encendida.'}), 500
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── Calibración ────────────────────────────────────────────────────────────────

@app.route('/lt', methods=['POST'])
def set_lt():
    """Aplica ^LT (corrección de posición) en tiempo real y guarda en config."""
    cfg  = load_config()
    if cfg.get('output_mode') == 'a4':
        return jsonify({'ok': False, 'error': 'No aplica en modo Hoja A4.'}), 400
    body = request.get_json(silent=True) or {}
    value = int(body.get('value', 0))
    cfg['backfeed_dots'] = value
    save_config(cfg)
    try:
        print_raw(cfg, f'^XA^LT{value}^XZ')
        return jsonify({'ok': True, 'value': value})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/calibrate', methods=['POST'])
def calibrate():
    cfg = load_config()
    if cfg.get('output_mode') == 'a4':
        return jsonify({'ok': False, 'error': 'No aplica en modo Hoja A4 — no hay gap/backfeed que calibrar.'}), 400
    params = request.get_json() or {}
    cfg.update({k: v for k, v in params.items() if v is not None})
    save_config(cfg)

    dpi = int(cfg.get('dpi', 203))
    dots_per_mm   = dpi / 25.4
    height_dots   = round(float(cfg.get('label_height_mm', 150)) * dots_per_mm)
    backfeed_dots = int(cfg.get('backfeed_dots', 0))
    media_char    = {'gap': 'G', 'continuous': 'N', 'mark': 'T'}.get(cfg.get('media_type', 'gap'), 'G')

    zpl = f'^XA\r\n^MN{media_char}\r\n^LL{height_dots}\r\n^LT{backfeed_dots}\r\n^XZ\r\n'
    try:
        print_raw(cfg, zpl)
        return jsonify({'ok': True, 'zpl': zpl, 'dots': {'height': height_dots, 'backfeed': backfeed_dots}})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


def _query_size(ip, port):
    """
    Consulta el tamaño de etiqueta y gap configurados en la impresora TSC.
    Intenta QUERY SIZE (TSPL) y ~HS (ZPL). Devuelve (width_mm, height_mm, gap_mm) o Nones.
    """
    import re

    # Intento 1: QUERY SIZE (TSPL — respuesta texto)
    raw = query_printer(ip, port, 'QUERY SIZE\r\n', read_bytes=128, timeout=3)
    if raw:
        text = raw.decode('ascii', errors='ignore').strip()
        # Formatos posibles: "4.00 5.91 0.12" o "4.00 5.91" (pulgadas)
        # o "101.6 mm, 152.4 mm, 3.0 mm" (mm)
        nums_mm = re.findall(r'(\d+\.?\d*)\s*mm', text, re.IGNORECASE)
        if len(nums_mm) >= 2:
            w = round(float(nums_mm[0]), 1)
            h = round(float(nums_mm[1]), 1)
            g = round(float(nums_mm[2]), 1) if len(nums_mm) >= 3 else None
            return w, h, g, text
        nums = re.findall(r'\d+\.?\d+', text)
        if len(nums) >= 2:
            # Asumimos pulgadas si los valores son < 30
            vals = [float(n) for n in nums[:3]]
            if vals[0] < 30:  # pulgadas
                w = round(vals[0] * 25.4, 1)
                h = round(vals[1] * 25.4, 1)
                g = round(vals[2] * 25.4, 1) if len(vals) >= 3 else None
            else:             # ya en mm
                w = round(vals[0], 1)
                h = round(vals[1], 1)
                g = round(vals[2], 1) if len(vals) >= 3 else None
            return w, h, g, text

    return None, None, None, None


@app.route('/printer/hs')
def printer_hs():
    """Diagnóstico: muestra respuesta cruda de ~HS. Requiere red Y ZPL — por
    USB no hay forma confiable de leer la respuesta de vuelta desde el
    spooler, y ~HS es un comando de la familia ZPL que no se probó (ni hay
    garantía de que funcione) con una impresora en modo TSPL."""
    cfg = load_config()
    if cfg.get('connection_type') == 'usb':
        return jsonify({'ok': False, 'error':
            'No disponible con impresora USB — esta lectura necesita conexión de red.'})
    if cfg.get('label_language') == 'tspl':
        return jsonify({'ok': False, 'error':
            'No disponible en modo TSPL — ~HS es un comando ZPL, no se probó contra impresoras TSPL.'})
    try:
        ip, port = cfg.get('ip', ''), int(cfg.get('port', 9100))
        hs_raw = query_printer(ip, port, '~HS', read_bytes=512, timeout=4)
        dpi = int(cfg.get('dpi', 203))
        height_mm, gap_mm = parse_hs_dimensions(hs_raw, dpi=dpi)
        def hex_dump(b):
            return ' '.join(f'{x:02x}' for x in b) if b else '(sin respuesta)'
        return jsonify({
            'ok':        True,
            'hs_hex':    hex_dump(hs_raw),
            'hs_len':    len(hs_raw) if hs_raw else 0,
            'hs_parsed': {'height_mm': height_mm, 'gap_mm': gap_mm} if (height_mm or gap_mm) else None,
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/autocal', methods=['POST'])
def autocal():
    cfg = load_config()
    if cfg.get('output_mode') == 'a4':
        return jsonify({'ok': False, 'error':
            'No disponible en modo Hoja A4 — no aplica calibración de gap en impresora normal.'}), 400
    if cfg.get('connection_type') == 'usb':
        return jsonify({'ok': False, 'error':
            'No disponible con impresora USB — calibrá con los botones físicos de la impresora.'}), 400
    if cfg.get('label_language') == 'tspl':
        return jsonify({'ok': False, 'error':
            'No disponible en modo TSPL — ~JC/~HS son comandos ZPL, no se probaron contra impresoras TSPL. Calibrá con los botones físicos de la impresora.'}), 400
    try:
        dpi         = int(cfg.get('dpi', 203))
        height_mm   = float(cfg.get('label_height_mm', 150))
        height_dots = round(height_mm * dpi / 25.4)

        send_to_printer(cfg['ip'], cfg['port'], '~JC')

        # Esperar calibración: reintenta cada 2s hasta que ~HS devuelva datos válidos (máx 20s)
        hs = None
        for attempt in range(10):
            time.sleep(2)
            hs = query_printer(cfg['ip'], cfg['port'], '~HS', read_bytes=512, timeout=4)
            if hs:
                h, g = parse_hs_dimensions(hs, dpi=dpi)
                if h is not None or g is not None:
                    break  # tenemos datos válidos

        hs_hex = ' '.join(f'{b:02x}' for b in hs) if hs else '(sin respuesta)'
        height_mm_read, gap_mm_read = parse_hs_dimensions(hs, dpi=dpi)
        width_mm_read = None   # ~HS no reporta ancho

        if gap_mm_read is not None:
            gap_mm     = gap_mm_read
            gap_source = 'medido'
            cfg['label_gap_mm'] = gap_mm
        else:
            gap_mm     = float(cfg.get('label_gap_mm', 10))
            gap_source = 'config'

        if height_mm_read is not None:
            height_mm   = height_mm_read
            height_dots = round(height_mm * dpi / 25.4)
            cfg['label_height_mm'] = height_mm

        if width_mm_read is not None:
            cfg['label_width_mm'] = width_mm_read

        save_config(cfg)

        return jsonify({
            'ok':         True,
            'gap_mm':     gap_mm,
            'gap_source': gap_source,
            'height_mm':  height_mm_read,
            'width_mm':   width_mm_read,
            'hs_hex':     hs_hex,
        })
    except socket.timeout:
        return jsonify({'ok': False, 'error': f"Timeout: no se pudo conectar a {cfg['ip']}:{cfg['port']}"}), 500
    except ConnectionRefusedError:
        return jsonify({'ok': False, 'error': f"Conexión rechazada en {cfg['ip']}:{cfg['port']} — ¿está encendida?"}), 500
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/testprint', methods=['POST'])
def testprint():
    cfg = load_config()
    if cfg.get('output_mode') == 'a4':
        return jsonify({'ok': False, 'error': 'No disponible en modo Hoja A4.'}), 400
    dpi = int(cfg.get('dpi', 203))
    dpm = dpi / 25.4
    h = round(float(cfg.get('label_height_mm', 150)) * dpm)
    w = round(float(cfg.get('label_width_mm', 100)) * dpm)
    cx = w // 2
    zpl = (f'^XA\r\n^PW{w}\r\n^LL{h}\r\n'
           f'^FO{cx-200},{h//2-50}^ADN,36,20^FDTEST CALIBRACION^FS\r\n'
           f'^FO{cx-150},{h//2+10}^ADN,20,10^FD{cfg["label_height_mm"]}mm x {cfg["label_width_mm"]}mm  {dpi}dpi^FS\r\n'
           f'^XZ\r\n')
    try:
        print_raw(cfg, zpl)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── ML OAuth ───────────────────────────────────────────────────────────────────

@app.route('/auth/login')
def auth_login():
    cfg = load_config()
    if not cfg.get('ml_client_id'):
        return redirect('/?error=Configurar+App+ID+primero')
    state = secrets.token_hex(16)
    verifier, challenge = _pkce_pair()
    _pkce_store[state] = verifier
    url = (f"{ML_AUTH_URL}?response_type=code"
           f"&client_id={cfg['ml_client_id']}"
           f"&redirect_uri={REDIRECT_URI}"
           f"&state={state}"
           f"&code_challenge={challenge}"
           f"&code_challenge_method=S256")
    return redirect(url)


@app.route('/auth/callback')
def auth_callback():
    code  = request.args.get('code')
    state = request.args.get('state', '')
    error = request.args.get('error')
    if error or not code:
        return redirect(f'/?tab=orders&error={error or "sin_codigo"}')

    verifier = _pkce_store.pop(state, None)
    cfg = load_config()
    try:
        payload = {
            'grant_type':    'authorization_code',
            'client_id':     cfg['ml_client_id'],
            'client_secret': cfg['ml_client_secret'],
            'code':          code,
            'redirect_uri':  REDIRECT_URI,
        }
        if verifier:
            payload['code_verifier'] = verifier

        r = http.post(ML_TOKEN_URL, data=payload, timeout=15)
        data = r.json()
        if 'access_token' not in data:
            return redirect('/?tab=orders&error=token_error')
        cfg['ml_access_token']     = data['access_token']
        cfg['ml_refresh_token']    = data.get('refresh_token')
        cfg['ml_token_expires_at'] = time.time() + data.get('expires_in', 21600)
        cfg['ml_user_id']          = data.get('user_id')
        save_config(cfg)
        return redirect('/?tab=orders')
    except Exception as e:
        return redirect(f'/?tab=orders&error={str(e)}')


@app.route('/auth/status')
def auth_status():
    token = get_valid_token()
    if not token:
        return jsonify({'logged_in': False})
    try:
        r = ml_get('/users/me', token)
        u = r.json()
        return jsonify({'logged_in': True, 'nickname': u.get('nickname'), 'user_id': u.get('id')})
    except Exception:
        cfg = load_config()
        return jsonify({'logged_in': True, 'user_id': cfg.get('ml_user_id')})


@app.route('/auth/logout', methods=['POST'])
def auth_logout():
    cfg = load_config()
    for k in ('ml_access_token', 'ml_refresh_token', 'ml_token_expires_at', 'ml_user_id'):
        cfg.pop(k, None)
    save_config(cfg)
    return jsonify({'ok': True})


def _fetch_shipment(shipment_id, token):
    """Trae logistic_type + dirección del destinatario para un envío."""
    try:
        r = http.get(
            f'{ML_API}/shipments/{shipment_id}',
            headers={'Authorization': f'Bearer {token}'},
            timeout=8,
        )
        d = r.json()
        addr = d.get('receiver_address', {})
        return shipment_id, {
            'logistic_type':    d.get('logistic_type', ''),
            'status':           d.get('status', ''),
            # Fuente de verdad de si la etiqueta ya se imprimió: la marca ML,
            # no el estado local del navegador (que puede estar desactualizado
            # si se imprimió desde otro dispositivo/sesión).
            'substatus':        d.get('substatus', ''),
            'receiver_name':    addr.get('receiver_name', ''),
            'street':           f"{addr.get('street_name','')} {addr.get('street_number','')}".strip(),
            'city':             addr.get('city', {}).get('name', ''),
            'state':            addr.get('state', {}).get('name', ''),
            'zip_code':         addr.get('zip_code', ''),
            'comment':          addr.get('comment', ''),
        }
    except Exception:
        return shipment_id, {'logistic_type': ''}


# ── ML Orders ──────────────────────────────────────────────────────────────────

@app.route('/ml/orders')
def ml_orders():
    token = get_valid_token()
    if not token:
        return jsonify({'ok': False, 'need_login': True}), 401

    cfg = load_config()
    user_id = cfg.get('ml_user_id')
    if not user_id:
        try:
            r = ml_get('/users/me', token)
            user_id = r.json().get('id')
            cfg['ml_user_id'] = user_id
            save_config(cfg)
        except Exception as e:
            return jsonify({'ok': False, 'error': str(e)}), 500

    try:
        # 1. Traer órdenes: pendientes + las ya despachadas de hoy
        from datetime import datetime, timezone, timedelta
        tz_arg  = timezone(timedelta(hours=-3))  # Argentina UTC-3
        today   = datetime.now(tz_arg).date()

        all_orders = []
        seen_ids   = set()
        for status in ('ready_to_ship', 'paid', 'shipped'):
            r = ml_get('/orders/search', token, params={
                'seller':       user_id,
                'order.status': status,
                'sort':         'date_desc',
                'limit':        50,
            })
            for o in r.json().get('results', []):
                if o['id'] in seen_ids:
                    continue
                # Para "shipped": filtrar solo las actualizadas hoy (evitar historial)
                if status == 'shipped':
                    last_update = o.get('last_updated') or o.get('date_closed') or ''
                    try:
                        upd_date = datetime.fromisoformat(last_update.replace('Z', '+00:00')).astimezone(tz_arg).date()
                        if upd_date != today:
                            continue
                    except Exception:
                        continue
                o['_status_label'] = status
                all_orders.append(o)
                seen_ids.add(o['id'])

        # 2. Traer detalle de envíos en paralelo (logistic_type + dirección)
        ship_ids = [(o.get('shipping', {}).get('id')) for o in all_orders]
        ship_ids = [sid for sid in ship_ids if sid]

        shipment_data = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_fetch_shipment, sid, token): sid for sid in ship_ids}
            for fut in futures:
                sid, data = fut.result()
                shipment_data[sid] = data

        # 3. Filtrar Full, cancelados y enriquecer con datos de envío
        printable = []
        for o in all_orders:
            if o.get('cancel_detail'):
                continue   # cancelación solicitada o confirmada
            status_detail = o.get('status_detail', '') or ''
            if 'cancel' in status_detail.lower():
                continue
            sid  = o.get('shipping', {}).get('id')
            info = shipment_data.get(sid, {})
            if info.get('logistic_type') == 'fulfillment':
                continue   # ML maneja estos, el vendedor no imprime
            o['_shipment'] = info
            printable.append(o)

        # Agrupar por shipment_id: ML crea un "pedido" por ítem pero comparten envío
        # → combinar en una sola tarjeta con todos los ítems
        from collections import defaultdict as _dd
        ship_groups = _dd(list)
        for o in printable:
            sid = str(o.get('shipping', {}).get('id') or f'_ns_{o["id"]}')
            ship_groups[sid].append(o)

        merged = []
        for sid_key, group in ship_groups.items():
            if len(group) == 1:
                merged.append(group[0])
            else:
                # Tomar el primer pedido como base y combinar todos los order_items
                base = dict(group[0])
                combined_items = []
                for o in group:
                    combined_items.extend(o.get('order_items') or [])
                base['order_items'] = combined_items
                base['_merged_order_ids'] = [o['id'] for o in group]
                merged.append(base)

        return jsonify({'ok': True, 'orders': merged, 'total': len(merged)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


def _extract_zpl(content, status_code, response_text, content_type):
    """
    Extrae el ZPL de la respuesta de ML. Retorna (zpl_bytes, error_msg).
    ML puede devolver el ZPL directo o dentro de un ZIP.
    """
    if status_code != 200:
        return None, f'ML devolvió {status_code}: {response_text[:300]}'
    if 'html' in content_type.lower():
        return None, 'ML devolvió una página HTML. El envío puede no tener etiqueta disponible aún o el token expiró.'
    if not content:
        return None, 'ML devolvió contenido vacío.'

    # ZIP: magic bytes PK (0x50 0x4B)
    if content[:2] == b'PK':
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                names = zf.namelist()
                if not names:
                    return None, 'El ZIP de ML está vacío.'
                zpl = zf.read(names[0])
                return zpl, None
        except zipfile.BadZipFile as e:
            return None, f'ML devolvió un ZIP inválido: {e}'

    # ZPL directo
    stripped = content.strip()
    if not stripped.upper().startswith(b'^XA'):
        preview = stripped[:120].decode('utf-8', errors='replace')
        return None, f'ML no devolvió ZPL válido. Respuesta: {preview}'
    return content, None


def _extract_pdf(content, status_code, response_text, content_type):
    """Igual que _extract_zpl pero para response_type=pdf (modo TSPL) — el
    PDF puede venir directo o dentro de un ZIP. NOTA: la forma exacta de esta
    respuesta no se pudo confirmar contra la API real de ML (no había ningún
    pedido pendiente disponible al momento de escribir esto), así que se
    maneja de la forma más flexible posible en vez de asumir un único formato."""
    if status_code != 200:
        return None, f'ML devolvió {status_code}: {response_text[:300]}'
    if 'html' in content_type.lower():
        return None, 'ML devolvió una página HTML. El envío puede no tener etiqueta disponible aún o el token expiró.'
    if not content:
        return None, 'ML devolvió contenido vacío.'

    if content[:2] == b'PK':  # ZIP
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                names = zf.namelist()
                if not names:
                    return None, 'El ZIP de ML está vacío.'
                pdf_names = [n for n in names if n.lower().endswith('.pdf')]
                target = pdf_names[0] if pdf_names else names[0]
                return zf.read(target), None
        except zipfile.BadZipFile as e:
            return None, f'ML devolvió un ZIP inválido: {e}'

    if content[:4] == b'%PDF':
        return content, None

    preview = content[:120].decode('utf-8', errors='replace')
    return None, f'ML no devolvió un PDF válido. Respuesta: {preview}'


def _fetch_ml_label(sid, token, cfg, force_pdf=False):
    """Pide la etiqueta de un envío a ML — ZPL2 (default) o PDF si
    label_language es 'tspl' o si force_pdf=True (modo A4, que siempre
    necesita el PDF para poder renderizarlo como imagen). Punto único que
    decide qué formato pedir, para no repetir esta rama en cada lugar que
    imprime. Devuelve (payload_bytes, is_zpl, error_msg)."""
    want_tspl = force_pdf or cfg.get('label_language') == 'tspl'
    r = http.get(
        f'{ML_API}/shipment_labels',
        params={'shipment_ids': sid, 'response_type': 'pdf' if want_tspl else 'zpl2'},
        headers={'Authorization': f'Bearer {token}'},
        timeout=20,
    )
    if want_tspl:
        payload, err = _extract_pdf(r.content, r.status_code, r.text, r.headers.get('content-type', ''))
        return payload, False, err
    payload, err = _extract_zpl(r.content, r.status_code, r.text, r.headers.get('content-type', ''))
    return payload, True, err


def count_labels(data: bytes) -> int:
    """Cuenta etiquetas en un bloque ZPL contando ocurrencias de ^XA."""
    import re as _re
    return max(1, len(_re.findall(rb'\^XA', data, _re.IGNORECASE)))


def _pdf_page_to_image(pdf_bytes: bytes, width_mm: float = 100.0,
                        height_mm: float = 150.0, dpi: int = 203,
                        overlay_text: str = None):
    """Renderiza la primera página de un PDF a una PIL.Image en gris,
    escalada y centrada sobre un canvas blanco del tamaño pedido. Capa de
    renderizado compartida por _pdf_page_to_bitmap (empaqueta a 1bpp para
    ZPL/TSPL) y label_image_for_a4 (la deja como imagen para modo A4).
    Si se pasa overlay_text, se dibuja en la esquina inferior-derecha (mismo
    lugar donde _inject_correlative_into_zpl pone el correlativo en ZPL).
    Devuelve (canvas: PIL.Image, target_w, target_h)."""
    try:
        import fitz  # pymupdf
    except ImportError:
        raise RuntimeError('pymupdf no instalado. Ejecutar: pip install pymupdf')
    try:
        from PIL import Image
    except ImportError:
        raise RuntimeError('Pillow no instalado. Ejecutar: pip install Pillow')

    target_w = max(1, int(width_mm  / 25.4 * dpi))
    target_h = max(1, int(height_mm / 25.4 * dpi))

    # Abrir PDF
    try:
        doc = fitz.open(stream=pdf_bytes, filetype='pdf')
    except Exception as e:
        raise RuntimeError(f'No se pudo abrir el PDF: {e}')
    if doc.page_count == 0:
        raise RuntimeError('El PDF está vacío (0 páginas)')

    page = doc[0]

    # Renderizar con alta resolución y luego reescalar
    render_scale = max(dpi, 300) / 72.0
    mat = fitz.Matrix(render_scale, render_scale)
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY)
    img = Image.frombytes('L', (pix.width, pix.height), pix.samples)

    # Escalar preservando relación de aspecto → centrar en canvas blanco
    scale  = min(target_w / img.width, target_h / img.height)
    new_w  = max(1, int(img.width  * scale))
    new_h  = max(1, int(img.height * scale))
    img    = img.resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new('L', (target_w, target_h), 255)
    canvas.paste(img, ((target_w - new_w) // 2, (target_h - new_h) // 2))

    if overlay_text:
        from PIL import ImageDraw, ImageFont
        draw = ImageDraw.Draw(canvas)
        try:
            font = ImageFont.truetype('arial.ttf', size=max(16, int(target_h * 0.045)))
        except Exception:
            font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), overlay_text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        pad = 10
        x = target_w - tw - pad - 6
        y = target_h - th - pad - 6
        draw.rectangle([x - 6, y - 4, x + tw + 6, y + th + 8], fill=255)
        draw.text((x, y), overlay_text, fill=0, font=font)

    return canvas, target_w, target_h


def label_image_for_a4(pdf_bytes: bytes, width_mm: float = 100.0,
                        height_mm: float = 150.0, dpi: int = 203):
    """Etiqueta de envío como PIL.Image plana, para pegar en la hoja A4
    (modo output_mode == 'a4'). No empaqueta a 1bpp — eso es solo para
    comandos de impresora térmica."""
    canvas, _, _ = _pdf_page_to_image(pdf_bytes, width_mm, height_mm, dpi)
    return canvas


def _pdf_page_to_bitmap(pdf_bytes: bytes, width_mm: float = 100.0,
                         height_mm: float = 150.0, dpi: int = 203,
                         overlay_text: str = None):
    """Renderiza la primera página de un PDF a un bitmap 1bpp (1=imprimir,
    0=blanco), empaquetado MSB-first — el mismo formato de datos crudos que
    usan tanto ^GFA (ZPL) como BITMAP (TSPL), solo cambia el "envoltorio" de
    comando alrededor. Envoltorio fino sobre _pdf_page_to_image + empaquetado
    (sin cambios de comportamiento respecto a la versión anterior).
    Devuelve (bitmap_bytes, target_w, target_h, bytes_per_row)."""
    try:
        from PIL import Image
    except ImportError:
        raise RuntimeError('Pillow no instalado. Ejecutar: pip install Pillow')

    canvas, target_w, target_h = _pdf_page_to_image(
        pdf_bytes, width_mm, height_mm, dpi, overlay_text=overlay_text)

    bytes_per_row = (target_w + 7) // 8
    total_bytes   = bytes_per_row * target_h

    # Intentar conversión rápida vía numpy
    try:
        import numpy as np
        arr  = np.asarray(canvas, dtype=np.uint8)           # (H, W)
        dark = (arr < 128).astype(np.uint8)                 # 1=imprimir, 0=blanco
        # Pad al multiplo de byte
        pad_w = bytes_per_row * 8
        if pad_w > target_w:
            dark = np.pad(dark, ((0, 0), (0, pad_w - target_w)))
        w8 = np.array([128, 64, 32, 16, 8, 4, 2, 1], dtype=np.uint16)
        packed = dark.reshape(target_h, bytes_per_row, 8)
        bitmap = (packed * w8).sum(axis=2).astype(np.uint8).tobytes()
    except ImportError:
        # Fallback PIL: convert('1') + XOR
        try:
            dith = Image.Dither.NONE
        except AttributeError:
            dith = Image.NONE  # Pillow < 9.1
        img1 = canvas.convert('1', dither=dith)
        raw  = img1.tobytes()
        # PIL '1': bit 0 = negro, bit 1 = blanco → destino: bit 1 = imprimir → invertir
        if len(raw) == total_bytes:
            bitmap = bytes(b ^ 0xFF for b in raw)
        else:
            # Fallback pixel a pixel (más lento pero garantizado)
            bmp = bytearray(total_bytes)
            px  = img1.load()
            for y in range(target_h):
                rb = y * bytes_per_row
                for x in range(target_w):
                    if px[x, y] == 0:   # negro → imprimir
                        bmp[rb + x // 8] |= (0x80 >> (x % 8))
            bitmap = bytes(bmp)

    return bitmap, target_w, target_h, bytes_per_row


def pdf_to_zpl(pdf_bytes: bytes, width_mm: float = 100.0,
               height_mm: float = 150.0, dpi: int = 203) -> bytes:
    """Convierte la primera página de un PDF a ZPL ^GFA listo para imprimir.
    Requiere pymupdf (fitz) y Pillow."""
    bitmap, target_w, target_h, bytes_per_row = _pdf_page_to_bitmap(
        pdf_bytes, width_mm, height_mm, dpi)
    total_bytes = bytes_per_row * target_h
    hex_data = bitmap.hex().upper()
    return (f'^XA\r\n^PW{target_w}\r\n^LL{target_h}\r\n^FO0,0\r\n'
            f'^GFA,{total_bytes},{total_bytes},{bytes_per_row},{hex_data}\r\n'
            f'^XZ\r\n').encode('ascii')


def pdf_to_tspl(pdf_bytes: bytes, width_mm: float = 100.0,
                 height_mm: float = 150.0, dpi: int = 203,
                 overlay_text: str = None) -> bytes:
    """Convierte la primera página de un PDF a TSPL (comando BITMAP) —
    equivalente a pdf_to_zpl pero para impresoras que hablan TSPL nativo
    (TSC, Xprinter, Godex) en vez de ZPL. Mismo renderizado, distinto
    'envoltorio' de comando. Requiere pymupdf (fitz) y Pillow.
    NOTA: sin validar todavía contra una impresora TSPL real."""
    bitmap, target_w, target_h, bytes_per_row = _pdf_page_to_bitmap(
        pdf_bytes, width_mm, height_mm, dpi, overlay_text=overlay_text)
    header = b'CLS\r\n'
    cmd    = f'BITMAP 0,0,{bytes_per_row},{target_h},0,'.encode('ascii')
    footer = b'\r\nPRINT 1,1\r\n'
    return header + cmd + bitmap + footer


def _pdf_to_label(pdf_bytes, cfg, width_mm=100.0, height_mm=150.0):
    """Convierte un PDF a la etiqueta lista para imprimir en el idioma
    configurado — ZPL (default) o TSPL si label_language == 'tspl'. Usado
    hoy por TiendaNube/Andreani, que ya trabaja con PDF."""
    dpi = int(cfg.get('dpi', 203))
    if cfg.get('label_language') == 'tspl':
        return pdf_to_tspl(pdf_bytes, width_mm=width_mm, height_mm=height_mm, dpi=dpi)
    return pdf_to_zpl(pdf_bytes, width_mm=width_mm, height_mm=height_mm, dpi=dpi)


def _ascii_zpl(text):
    # Preserva acentos latinos (á é í ó ú ñ ü etc.) — están en latin-1.
    # Solo reemplaza caracteres fuera de latin-1 (emoji, chino, etc.) con '?'.
    return str(text).encode('latin-1', errors='replace').decode('latin-1')


def next_correlative():
    """Número secuencial del día. Se reinicia automáticamente cada jornada."""
    from datetime import date
    today = date.today().isoformat()
    cfg = load_config()
    if cfg.get('_correlative_date') != today:
        cfg['_correlative'] = 0
        cfg['_correlative_date'] = today
    n = int(cfg.get('_correlative', 0)) + 1
    cfg['_correlative'] = n
    save_config(cfg)
    return n


def _inject_correlative_into_zpl(zpl_bytes, number):
    """Inyecta #NNN en la primera etiqueta del ZPL de ML, cerca de la zona de dirección.

    Usa ^A0 (CG Triumvirate) — fuente limpia y moderna.
    Posición: esquina inferior-derecha, donde ML deja espacio libre.
    """
    num_str = f'#{number:03d}'
    # ^A0N,h,w → fuente A0 (sans-serif moderna), orientación normal
    # ^FB760,1,0,R,0 = bloque de 760 dots, 1 línea, alineado a la DERECHA
    field = f'^FO15,880^FB760,1,0,R,0^A0N,100,50^FD{num_str}^FS\r\n'.encode('latin-1')
    idx = zpl_bytes.upper().find(b'^XZ')
    if idx == -1:
        return zpl_bytes
    return zpl_bytes[:idx] + field + zpl_bytes[idx:]


def _build_detail_zpl(order_data, cfg):
    """Etiqueta de detalle: correlativo grande arriba, código de barras, artículos con separadores."""
    dpi = int(cfg.get('dpi', 203))
    dpm = dpi / 25.4
    w   = round(float(cfg.get('label_width_mm',  100)) * dpm)
    h   = round(float(cfg.get('label_height_mm', 150)) * dpm)

    buyer       = _ascii_zpl(str(order_data.get('buyer',       '')))[:38]
    order_id    = str(order_data.get('order_id',    ''))
    shipment_id = str(order_data.get('shipment_id', ''))
    correlative = order_data.get('correlative')
    items       = order_data.get('items', [])

    m = 25
    y = 12

    lines = ['^XA', f'^PW{w}', f'^LL{h}']

    def txt(fh, fw, content, indent=0):
        nonlocal y
        lines.append(f'^FO{m + indent},{y}^ADN,{fh},{fw}^FD{str(content)[:60]}^FS')
        y += fh + 10

    def hsep(thick=1):
        nonlocal y
        lines.append(f'^FO{m},{y}^GB{w - m * 2},{thick},{thick}^FS')
        y += thick + 7

    # ── Correlativo grande ───────────────────────────────────────────────────
    if correlative is not None:
        lines.append(f'^FO{m},{y}^A0N,95,48^FD#{correlative:03d}^FS')
        y += 108

    # ── Código de barras del envío ───────────────────────────────────────────
    if shipment_id:
        lines.append('^BY3')
        lines.append(f'^FO{m},{y}^BCN,90,Y,N,N^FD{shipment_id}^FS')
        y += 124

    # ── Datos del pedido ─────────────────────────────────────────────────────
    hsep(2)
    if order_id:
        txt(20, 10, f'Pedido # {order_id}')
    if buyer:
        txt(23, 11, buyer)
    hsep(2)

    # ── Artículos: viñeta + word wrap automático ─────────────────────────────
    # Fuente ADN,30,13 (narrow). cpl usa font_w para no subestimar líneas necesarias.
    fld_w  = w - m - 22 - m
    font_h = 30
    font_w = 13
    line_h = font_h + 8       # espacio real por línea dentro del bloque
    cpl    = max(10, fld_w // font_w)  # chars por línea con este ancho de fuente
    first  = True
    for item in items:
        if y > h - 80:
            lines.append(f'^FO{m},{y}^ADN,24,10^FD... y mas articulos^FS')
            break
        if not first:
            hsep(1)
        first = False
        qty   = item.get('qty', 1)
        title = _ascii_zpl(str(item.get('title', '')))
        label = f'{qty}  {title}'
        nlines = max(1, min(6, (len(label) + cpl - 1) // cpl))
        lines.append(f'^FO{m},{y + 8}^GB14,14,14^FS')
        lines.append(f'^FO{m + 22},{y}^ADN,{font_h},{font_w}^FB{fld_w},{nlines},6,L,0^FD{label}^FS')
        y += nlines * line_h + 14

    lines.append('^XZ')
    return ('\r\n'.join(lines) + '\r\n').encode('latin-1', errors='replace')


def _build_detail_tspl(order_data, cfg):
    """Equivalente en TSPL de _build_detail_zpl: mismo contenido (correlativo,
    código de barras, comprador, artículos con salto de línea manual), pero
    con comandos TSPL (TEXT/BARCODE/BAR) en vez de ZPL. Las fuentes de TSPL
    no son 1:1 con las de ZPL — los tamaños son una aproximación funcional,
    no una réplica pixel a pixel. NOTA: sin validar todavía contra una
    impresora TSPL real, puede necesitar ajuste de tamaños de fuente."""
    dpi = int(cfg.get('dpi', 203))
    dpm = dpi / 25.4
    w   = round(float(cfg.get('label_width_mm',  100)) * dpm)
    h   = round(float(cfg.get('label_height_mm', 150)) * dpm)

    def q(text):
        # TSPL delimita strings con comillas dobles — evitar romper el comando
        return str(text).replace('"', "'").replace('\\', '/')

    buyer       = q(order_data.get('buyer', ''))[:38]
    order_id    = str(order_data.get('order_id', ''))
    shipment_id = str(order_data.get('shipment_id', ''))
    correlative = order_data.get('correlative')
    items       = order_data.get('items', [])

    m = 25
    y = 12
    lines = ['CLS']

    def txt(mult, content, indent=0):
        nonlocal y
        lines.append(f'TEXT {m + indent},{y},"3",0,{mult},{mult},"{q(content)[:60]}"')
        y += 20 * mult + 14

    def hsep(thick=2):
        nonlocal y
        lines.append(f'BAR {m},{y},{w - m * 2},{thick}')
        y += thick + 7

    # ── Correlativo grande ───────────────────────────────────────────────────
    if correlative is not None:
        lines.append(f'TEXT {m},{y},"3",0,3,3,"#{correlative:03d}"')
        y += 116

    # ── Código de barras del envío ───────────────────────────────────────────
    if shipment_id:
        lines.append(f'BARCODE {m},{y},"128",90,1,0,2,4,"{q(shipment_id)}"')
        y += 124

    # ── Datos del pedido ─────────────────────────────────────────────────────
    hsep()
    if order_id:
        txt(1, f'Pedido # {order_id}')
    if buyer:
        txt(1, buyer)
    hsep()

    # ── Artículos: salto de línea manual (TSPL no tiene bloque con wrap) ─────
    fld_w   = w - m - 22 - m
    char_w  = 13  # ancho aprox. por caracter con fuente "3" x1
    cpl     = max(10, fld_w // char_w)
    first   = True
    for item in items:
        if y > h - 80:
            lines.append(f'TEXT {m},{y},"3",0,1,1,"... y mas articulos"')
            break
        if not first:
            hsep(1)
        first = False
        qty   = item.get('qty', 1)
        title = q(item.get('title', ''))
        label = f'{qty}  {title}'
        wrapped = [label[i:i + cpl] for i in range(0, len(label), cpl)][:6] or ['']
        lines.append(f'BAR {m},{y + 6},14,14')
        for i, wline in enumerate(wrapped):
            lines.append(f'TEXT {m + 22},{y},"3",0,1,1,"{wline}"')
            y += 32
        y += 12

    lines.append('PRINT 1,1')
    return ('\r\n'.join(lines) + '\r\n').encode('latin-1', errors='replace')


def _build_detail_image(order_data, cfg, width_px, scale=1.0):
    """Equivalente en imagen de _build_detail_zpl/_build_detail_tspl: mismo
    contenido (correlativo, comprador, artículos), pero dibujado con
    PIL.ImageDraw para pegar en la hoja A4 (modo A4). Sin código de barras —
    la etiqueta de ML de al lado ya tiene su propio QR, no hace falta
    duplicar. Tipografía grande a propósito, pensada para leerse cómodo sin
    anteojos. `scale` achica fuentes/espaciados de forma proporcional — lo
    usa _build_a4_page para que un pedido con muchos artículos siga entrando
    en una sola hoja, con un piso que nunca la hace ilegible. Devuelve una
    PIL.Image recortada a su alto real de contenido."""
    from PIL import Image, ImageDraw, ImageFont
    import textwrap

    def px(n):
        return max(1, int(round(n * scale)))

    def font(size, bold=False):
        name = 'arialbd.ttf' if bold else 'arial.ttf'
        try:
            return ImageFont.truetype(name, size=size)
        except Exception:
            return ImageFont.load_default()

    f_big   = font(px(110), bold=True)   # correlativo
    f_h     = font(px(52),  bold=True)   # "Pedido # ..."
    f_body  = font(px(46))               # comprador
    f_item  = font(px(42))               # artículos
    f_small = font(px(30))               # referencia de envío

    buyer       = str(order_data.get('buyer',       ''))[:60]
    order_id    = str(order_data.get('order_id',    ''))
    shipment_id = str(order_data.get('shipment_id', ''))
    correlative = order_data.get('correlative')
    items       = order_data.get('items', [])

    m       = px(36)
    # Generoso a propósito — no queremos cortar artículos silenciosamente
    # (se recorta al alto real de contenido al final, esto es solo un techo).
    max_h   = px(500) + max(1, len(items)) * px(260)
    canvas  = Image.new('L', (width_px, max_h), 255)
    draw    = ImageDraw.Draw(canvas)
    y       = m

    if correlative is not None:
        draw.text((m, y), f'#{correlative:03d}', font=f_big, fill=0)
        y += px(140)

    def hsep(thick=None):
        nonlocal y
        thick = px(4) if thick is None else thick
        draw.line([(m, y), (width_px - m, y)], fill=0, width=thick)
        y += thick + px(22)

    hsep()
    if order_id:
        draw.text((m, y), f'Pedido # {order_id}', font=f_h, fill=0)
        y += px(66)
    if buyer:
        draw.text((m, y), buyer, font=f_body, fill=0)
        y += px(58)
    if shipment_id:
        draw.text((m, y), f'Envío: {shipment_id}', font=f_small, fill=0)
        y += px(44)
    hsep()

    cpl = max(8, (width_px - 2 * m) // px(24))
    for item in items:
        qty   = item.get('qty', 1)
        title = str(item.get('title', ''))
        for line in (textwrap.wrap(f'{qty}  {title}', width=cpl)[:6] or ['']):
            draw.text((m, y), line, font=f_item, fill=0)
            y += px(52)
        y += px(18)
        draw.line([(m, y), (width_px - m, y)], fill=0, width=px(2))
        y += px(18)

    return canvas.crop((0, 0, width_px, min(y + m, max_h)))


def _build_a4_page(label_img, order_data, cfg):
    """Compone la hoja A4 vertical (normal, retrato) para modo
    output_mode == 'a4' — SIEMPRE una sola hoja: etiqueta de envío arriba,
    ROTADA 90° (la etiqueta de ML es angosta y alta — girada de costado
    aprovecha todo el ancho de la hoja y sale bastante más grande), y debajo
    el detalle del pedido (comprador, TODOS los artículos, tipografía
    grande). Si el detalle a tamaño normal no entra en lo que queda de hoja,
    se redibuja más chico (parámetro `scale` de _build_detail_image) hasta
    que entre — nunca se pasa a una segunda hoja ni se recorta un artículo
    mientras el achique razonable (piso 70% del tamaño normal, para que siga
    siendo legible sin anteojos) alcance. Lienzo a 300dpi, 2480×3508px
    (210×297mm). Si order_data viene vacío/None (hoy pasa con TiendaNube,
    que no arma un detalle local — la etiqueta de Andreani ya trae la
    dirección impresa por el correo), se omite el bloque de detalle, la
    etiqueta se deja SIN rotar (se lee de igual como la imprimiría cualquier
    label normal) y ocupa casi toda la hoja. Devuelve una lista de un solo
    PIL.Image (lista por compatibilidad con print_a4_image)."""
    from PIL import Image, ImageDraw

    page_w, page_h = 2480, 3508
    margin = 60
    avail_w = page_w - 2 * margin

    page = Image.new('L', (page_w, page_h), 255)

    if not order_data:
        avail_h = page_h - 2 * margin
        lw, lh  = label_img.size
        scale   = min(avail_w / lw, avail_h / lh)
        new_w, new_h = max(1, int(lw * scale)), max(1, int(lh * scale))
        resized = label_img.resize((new_w, new_h), Image.LANCZOS)
        page.paste(resized, ((page_w - new_w) // 2, margin))
        return [page]

    # Etiqueta girada 90° (angosta-y-alta -> ancha-y-baja): usa todo el ancho
    # de la hoja, hasta 48% de alto — sale bastante más grande que sin girar.
    label_img = label_img.transpose(Image.ROTATE_90)
    max_label_h = int(page_h * 0.48) - margin
    lw, lh = label_img.size
    label_scale = min(avail_w / lw, max_label_h / lh)
    new_w, new_h = max(1, int(lw * label_scale)), max(1, int(lh * label_scale))
    resized = label_img.resize((new_w, new_h), Image.LANCZOS)
    page.paste(resized, ((page_w - new_w) // 2, margin))

    label_bottom   = margin + new_h
    detail_top     = label_bottom + 70
    avail_detail_h = page_h - margin - detail_top

    detail_img = _build_detail_image(order_data, cfg, width_px=avail_w, scale=1.0)
    if detail_img.height > avail_detail_h:
        # Achicar proporcionalmente al espacio real disponible, con un piso
        # de legibilidad (70% del tamaño normal — tiene que poder leerse sin
        # anteojos). Pedidos con MUCHOS artículos son un caso raro; por debajo
        # de este piso preferimos que el texto quede apretado/recortado al
        # final antes que hacerlo ilegiblemente chico.
        detail_scale = max(0.7, avail_detail_h / detail_img.height)
        detail_img = _build_detail_image(order_data, cfg, width_px=avail_w, scale=detail_scale)
        if detail_img.height > avail_detail_h:
            # Caso extremo (pedido con muchísimos artículos): último recurso,
            # recortar — pero ya redujimos todo lo razonable antes de llegar acá.
            detail_img = detail_img.crop((0, 0, avail_w, avail_detail_h))

    draw = ImageDraw.Draw(page)
    draw.line([(margin, label_bottom + 20), (page_w - margin, label_bottom + 20)], fill=0, width=4)
    page.paste(detail_img, (margin, detail_top))

    return [page]


def print_a4_image(printer_name, pil_images, retries=1, retry_delay=0.6):
    """Imprime una o varias PIL.Image como UN solo trabajo de impresión en una
    impresora Windows normal — modo documento (GDI/StartDoc), no RAW: es el
    equivalente de send_to_printer_usb pero para output_mode == 'a4'.
    Silencioso, sin diálogo de impresión. Acepta una imagen sola o una lista
    (compatibilidad — hoy _build_a4_page siempre arma una sola hoja) — todas
    salen del mismo StartDoc/EndDoc, una hoja física por imagen. Fuerza papel
    A4 + orientación vertical vía DEVMODE — si no se fija esto, la impresora
    usa lo que tenga configurado por default (a veces Carta/Letter, otra
    proporción, o apaisada), y el lienzo fijo de 2480×3508 se estira para
    llenar esa hoja distinta, deformando la etiqueta."""
    if not printer_name:
        raise RuntimeError('No hay impresora seleccionada para modo Hoja A4 en Configuración.')
    if not isinstance(pil_images, (list, tuple)):
        pil_images = [pil_images]
    try:
        import win32ui
        import win32print
        import win32con
        import win32gui
        from PIL import ImageWin
    except ImportError:
        raise RuntimeError('pywin32 no instalado — no se puede imprimir en modo Hoja A4.')

    HORZRES, VERTRES = 8, 10
    last_err = None
    for attempt in range(retries + 1):
        try:
            hprinter = win32print.OpenPrinter(printer_name)
            try:
                devmode = win32print.GetPrinter(hprinter, 2)['pDevMode']
                devmode.PaperSize   = win32con.DMPAPER_A4
                devmode.Orientation = win32con.DMORIENT_PORTRAIT
                devmode.Fields |= win32con.DM_PAPERSIZE | win32con.DM_ORIENTATION
                win32print.DocumentProperties(
                    0, hprinter, printer_name, devmode, devmode,
                    win32con.DM_IN_BUFFER | win32con.DM_OUT_BUFFER)
            finally:
                win32print.ClosePrinter(hprinter)

            hdc = win32ui.CreateDCFromHandle(win32gui.CreateDC('WINSPOOL', printer_name, devmode))
            try:
                hdc.StartDoc('EnvioBot - Etiqueta A4')
                w = hdc.GetDeviceCaps(HORZRES)
                h = hdc.GetDeviceCaps(VERTRES)
                for pil_image in pil_images:
                    hdc.StartPage()
                    dib = ImageWin.Dib(pil_image.convert('RGB'))
                    dib.draw(hdc.GetHandleOutput(), (0, 0, w, h))
                    hdc.EndPage()
                hdc.EndDoc()
            finally:
                hdc.DeleteDC()
            return
        except Exception as e:
            last_err = e
            if attempt < retries:
                logger.warning('print_a4_image: intento %d/%d falló (%s), reintentando…',
                                attempt + 1, retries + 1, e)
                time.sleep(retry_delay)
    logger.error('print_a4_image: sin éxito tras %d intentos — %s', retries + 1, last_err)
    raise last_err


def _build_combo_zpl(order_data, ml_zpl_bytes, cfg):
    """
    Etiqueta combo 100×190 mm con troquel:
      Layout físico: troquel ARRIBA (sale primero), envío ABAJO.
      - y=0..die_dots        : items del pedido (troquel, 40 mm)
      - y=die_dots..total    : ZPL de ML (sección envío, 150 mm)
    """
    import re

    dpi        = int(cfg.get('dpi', 203))
    dpm        = dpi / 25.4
    w          = round(float(cfg.get('label_width_mm', 100)) * dpm)
    total_mm   = float(cfg.get('label_height_mm', 190))
    die_cut_mm = float(cfg.get('ml_die_cut_mm', 40))
    ship_h_mm  = total_mm - die_cut_mm
    die_dots   = round(die_cut_mm * dpm)
    total_dots = round(total_mm * dpm)

    # ── Items en el troquel (parte superior) ─────────────────────────────────
    items  = order_data.get('items', [])
    margin = 20
    y      = 20
    detail = []

    # Misma fuente y enfoque que _build_detail_zpl (100×150): ^ADN,30,13 + ^FB
    font_h  = 30
    font_w  = 13
    line_h  = font_h + 8
    fld_w   = w - margin - 22 - margin   # igual que detail
    cpl     = max(10, fld_w // font_w)

    for item in items:
        if y > die_dots - line_h - 8:
            detail.append(f'^FO{margin},{y}^ADN,22,10^FD...^FS')
            break
        qty   = item.get('qty', 1)
        title = _ascii_zpl(str(item.get('title', '')))
        label = f'{qty}  {title}'
        nl    = max(1, min(4, (len(label) + cpl - 1) // cpl))
        nl    = min(nl, max(1, (die_dots - y - 12) // line_h))
        detail.append(f'^FO{margin},{y + 6}^GB14,14,14^FS')
        detail.append(f'^FO{margin + 22},{y}^ADN,{font_h},{font_w}^FB{fld_w},{nl},6,L,0^FD{label}^FS')
        y += nl * line_h + 6

    # ── Procesar ZPL de ML → sección inferior (envío) ───────────────────────
    ml_str     = ml_zpl_bytes.decode('latin-1', errors='replace')
    body_match = re.search(r'\^XA(.*?)\^XZ', ml_str, re.DOTALL | re.IGNORECASE)
    ml_body    = body_match.group(1) if body_match else ml_str

    for pat in (r'\^PW\d+', r'\^LL\d+', r'\^LT-?\d+', r'\^MN[A-Z]', r'\^LH\d+,\d+'):
        ml_body = re.sub(pat, '', ml_body, flags=re.IGNORECASE)
    # NO stripear ^CI del body de ML — el ML usa UTF-8 (^CI28) para sus textos (ej. "Envío Flex")

    # Eliminar margen superior interno del ZPL de ML y arrancar justo debajo del separador
    ml_y_vals    = [int(m.group(1)) for m in re.finditer(r'\^FO\d+,(\d+)', ml_body)]
    ml_top_strip = min(ml_y_vals) if ml_y_vals else 0
    ml_offset    = die_dots + round(5 * dpm)   # 5mm de gap entre troquel y envío

    # Escalar sección de envío para que entre con margen inferior (~8 mm)
    bottom_margin_dots = round(8 * dpm)
    available_dots = total_dots - ml_offset - bottom_margin_dots
    ml_max_y  = max(ml_y_vals) if ml_y_vals else round(ship_h_mm * dpm)
    ml_span   = max(1, ml_max_y - ml_top_strip)
    scale_y   = min(1.0, available_dots / ml_span)

    def shift_fo(m_):
        y_orig = int(m_.group(2)) - ml_top_strip
        y_new  = round(y_orig * scale_y) + ml_offset
        return f'^FO{m_.group(1)},{max(0, y_new)}'
    ml_body = re.sub(r'\^FO(\d+),(\d+)', shift_fo, ml_body, flags=re.IGNORECASE)

    # Separador horizontal entre troquel y envío
    sep_y = ml_offset - 5

    # ── Ensamblar ────────────────────────────────────────────────────────────
    lt    = round(8 * dpm)
    parts = ['^XA', f'^PW{w}', f'^LL{total_dots}', f'^LT{lt}', '^CI27']
    parts.extend(detail)                          # troquel: latin-1 (^CI27 activo)
    parts.append(f'^FO0,{sep_y}^GB{w},3,3^FS')   # separador
    parts.append(ml_body.strip())                 # envío después
    parts.append('^XZ')

    return ('\r\n'.join(parts) + '\r\n').encode('latin-1', errors='replace')


def _print_ml_order(payload_in, is_zpl, order_data, cfg):
    """
    Imprime una orden ML según el tipo de etiqueta configurado.
    `payload_in` es ZPL nativo de ML si is_zpl=True, o un PDF si is_zpl=False
    (modo TSPL — ver label_language). Devuelve (payload_bytes, labels_count).

    El layout "combo" (troquel 100×190) es específico de ZPL — en modo TSPL
    siempre se usa el layout estándar de 2 etiquetas (envío + detalle),
    independientemente de ml_label_type.
    """
    corr = order_data.get('correlative') or next_correlative()
    order_data['correlative'] = corr

    if not is_zpl:
        dpi  = int(cfg.get('dpi', 203))
        w_mm = float(cfg.get('label_width_mm',  100))
        h_mm = float(cfg.get('label_height_mm', 150))
        ship_tspl = pdf_to_tspl(payload_in, width_mm=w_mm, height_mm=h_mm,
                                 dpi=dpi, overlay_text=f'#{corr:03d}')
        payload = ship_tspl + _build_detail_tspl(order_data, cfg)
        return payload, 2

    label_type = cfg.get('ml_label_type', 'standard')
    if label_type == 'combo':
        payload = _build_combo_zpl(order_data, payload_in, cfg)
        return payload, 1
    else:
        payload = (_inject_correlative_into_zpl(payload_in, corr)
                   + _build_detail_zpl(order_data, cfg))
        return payload, 2


@app.route('/local/orders')
def local_orders_endpoint():
    token = get_valid_token()
    if token:
        try:
            orders = _sync_orders_in_transit(token)
        except Exception:
            orders = load_orders()
    else:
        orders = load_orders()
    return jsonify({'ok': True, 'orders': orders})


@app.route('/local/import', methods=['POST'])
def local_import():
    """Importa pedidos impresos desde el cache del browser a orders.json (migración)."""
    body   = request.get_json(silent=True) or {}
    to_imp = body.get('orders', [])
    existing_ids = {o['shipment_id'] for o in load_orders()}
    added = 0
    for o in to_imp:
        sid = int(o.get('shipment_id', 0) or 0)
        if not sid or sid in existing_ids:
            continue
        _save_printed_order({
            'shipment_id':  str(sid),
            'order_id':     str(o.get('order_id', 0) or 0),
            'buyer':        o.get('buyer', ''),
            'address':      o.get('address', ''),
            'logistic_type': o.get('logistic_type', ''),
            'items':        o.get('items', []),
        })
        existing_ids.add(sid)
        added += 1
    return jsonify({'ok': True, 'added': added})


@app.route('/ml/debug-orders')
def ml_debug_orders():
    token = get_valid_token()
    if not token:
        return jsonify({'ok': False, 'error': 'no token'}), 401
    cfg = load_config()
    user_id = cfg.get('ml_user_id')
    results = {}
    for status in ('ready_to_ship', 'paid', 'shipped', 'delivered', 'cancelled'):
        r = ml_get('/orders/search', token, params={
            'seller': user_id, 'order.status': status,
            'sort': 'date_desc', 'limit': 5,
        })
        data = r.json()
        orders = data.get('results', [])
        # Enriquecer con logistic_type del envío
        enriched = []
        for o in orders[:5]:
            sid = o.get('shipping', {}).get('id')
            lt = None
            if sid:
                try:
                    sr = ml_get(f'/shipments/{sid}', token)
                    lt = sr.json().get('logistic_type')
                except Exception:
                    pass
            enriched.append({'id': o['id'], 'last_updated': o.get('last_updated'), 'logistic_type': lt})
        results[status] = {
            'count': data.get('paging', {}).get('total', '?'),
            'orders': enriched,
        }
    return jsonify(results)


@app.route('/ml/zpl/<int:shipment_id>')
def ml_zpl_preview(shipment_id):
    """Descarga el ZPL de ML sin imprimir (para diagnóstico)."""
    token = get_valid_token()
    if not token:
        return jsonify({'ok': False, 'need_login': True}), 401
    try:
        r = http.get(
            f'{ML_API}/shipment_labels',
            params={'shipment_ids': shipment_id, 'response_type': 'zpl2'},
            headers={'Authorization': f'Bearer {token}'},
            timeout=20,
        )
        zpl, err = _extract_zpl(r.content, r.status_code, r.text, r.headers.get('content-type', ''))
        if err:
            return err, 502, {'Content-Type': 'text/plain; charset=utf-8'}
        return zpl, 200, {
            'Content-Type': 'text/plain; charset=utf-8',
            'Content-Disposition': f'attachment; filename="etiqueta_{shipment_id}.zpl"',
        }
    except Exception as e:
        return str(e), 500


@app.route('/ml/combo-debug/<int:shipment_id>')
def ml_combo_debug(shipment_id):
    """Devuelve el ZPL combo sin imprimir + datos de order_data para diagnóstico."""
    token = get_valid_token()
    if not token:
        return jsonify({'ok': False, 'need_login': True}), 401
    try:
        cfg = load_config()
        r = http.get(
            f'{ML_API}/shipment_labels',
            params={'shipment_ids': shipment_id, 'response_type': 'zpl2'},
            headers={'Authorization': f'Bearer {token}'}, timeout=20,
        )
        zpl, err = _extract_zpl(r.content, r.status_code, r.text, r.headers.get('content-type', ''))
        if err:
            return jsonify({'ok': False, 'error': f'ML {r.status_code}: {r.text[:300]}'}), 502

        order_data = {'order_id': '0', 'shipment_id': str(shipment_id),
                      'buyer': 'TEST', 'items': [], 'correlative': 1}
        # Intentar obtener datos reales del pedido desde la API
        try:
            ro = http.get(f'{ML_API}/orders/search?tags=with_shipments&shipping_id={shipment_id}',
                          headers={'Authorization': f'Bearer {token}'}, timeout=10)
            orders = ro.json().get('results', [])
            if orders:
                o = orders[0]
                order_data['order_id'] = str(o.get('id', ''))
                order_data['buyer'] = o.get('buyer', {}).get('nickname', '')
                order_data['items'] = [
                    {'qty': i.get('quantity', 1), 'title': i.get('item', {}).get('title', '')}
                    for i in o.get('order_items', [])
                ]
        except Exception:
            pass
        # Fallback: buscar en orders.json (pedidos ya impresos no tienen order_items en la API)
        if not order_data['items']:
            saved = next((o for o in load_orders() if str(o.get('shipment_id')) == str(shipment_id)), None)
            if saved and saved.get('items'):
                order_data['order_id'] = str(saved.get('id', order_data['order_id']))
                order_data['buyer']    = saved.get('buyer', order_data['buyer'])
                order_data['items']    = saved['items']

        combo_zpl = _build_combo_zpl(order_data, zpl, cfg).decode('latin-1', errors='replace')
        return jsonify({
            'ok': True,
            'items_count': len(order_data['items']),
            'items': order_data['items'],
            'zpl_len': len(combo_zpl),
            'zpl_preview': combo_zpl[:500],
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/ml/print/<int:shipment_id>', methods=['POST'])
def ml_print(shipment_id):
    err = _require_license()
    if err: return err
    token = get_valid_token()
    if not token:
        return jsonify({'ok': False, 'need_login': True}), 401

    cfg        = load_config()
    want_a4    = cfg.get('output_mode') == 'a4'
    order_data = request.get_json(silent=True) or {}
    try:
        zpl, is_zpl, err = _fetch_ml_label(shipment_id, token, cfg, force_pdf=want_a4)
        if err:
            logger.error('ml_print(%s): _fetch_ml_label falló (force_pdf=%s) — %s',
                          shipment_id, want_a4, err)
            return jsonify({'ok': False, 'error': err}), 502

        # Si no vienen items desde el frontend (reimpresión), buscar en orders.json
        if not order_data.get('items'):
            saved = next((o for o in load_orders() if str(o.get('shipment_id')) == str(shipment_id)), None)
            if saved:
                order_data.setdefault('order_id', saved.get('id', ''))
                order_data.setdefault('buyer',    saved.get('buyer', ''))
                if saved.get('items'):
                    order_data['items'] = saved['items']

        # Último recurso: consultar la API de ML directamente para obtener los items
        if not order_data.get('items'):
            try:
                order_id_for_api = order_data.get('order_id') or ''
                # Intentar buscar por shipping_id
                ro = http.get(f'{ML_API}/orders/search',
                              params={'tags': 'with_shipments', 'shipping_id': shipment_id},
                              headers={'Authorization': f'Bearer {token}'}, timeout=10)
                ml_orders = ro.json().get('results', [])
                if ml_orders:
                    o = ml_orders[0]
                    items_from_api = [
                        {'qty': i.get('quantity', 1), 'title': i.get('item', {}).get('title', '')}
                        for i in o.get('order_items', [])
                    ]
                    if items_from_api:
                        order_data['items']    = items_from_api
                        order_data['order_id'] = str(o.get('id', order_id_for_api))
                        order_data['buyer']    = (o.get('buyer') or {}).get('nickname', order_data.get('buyer', ''))
                        # Actualizar orders.json con los items encontrados
                        orders = load_orders()
                        for saved_o in orders:
                            if str(saved_o.get('shipment_id')) == str(shipment_id):
                                saved_o['items'] = items_from_api
                                break
                        save_orders(orders)
            except Exception:
                pass

        if order_data.get('items'):
            corr = next_correlative()
            order_data['shipment_id'] = str(shipment_id)
            order_data['correlative'] = corr

        if want_a4:
            label_img = label_image_for_a4(zpl, width_mm=float(cfg.get('label_width_mm', 100)),
                                            height_mm=float(cfg.get('label_height_mm', 150)),
                                            dpi=int(cfg.get('dpi', 203)))
            page = _build_a4_page(label_img, order_data, cfg)
            print_a4_image(cfg.get('a4_printer_name'), page)
            _save_printed_order(order_data)
            return jsonify({'ok': True, 'labels': 1,
                            'items_used': len(order_data.get('items', []))})

        if order_data.get('items'):
            payload, n_labels = _print_ml_order(zpl, is_zpl, order_data, cfg)
        elif is_zpl:
            payload  = zpl
            n_labels = count_labels(payload)
        else:
            payload  = pdf_to_tspl(zpl, width_mm=float(cfg.get('label_width_mm', 100)),
                                    height_mm=float(cfg.get('label_height_mm', 150)),
                                    dpi=int(cfg.get('dpi', 203)))
            n_labels = 1
        print_raw(cfg, payload)
        _save_printed_order(order_data)
        return jsonify({'ok': True, 'labels': n_labels,
                        'items_used': len(order_data.get('items', []))})
    except socket.timeout:
        return jsonify({'ok': False, 'error': f"Timeout de impresora: {cfg['ip']}:{cfg['port']}"}), 500
    except Exception as e:
        logger.error('ml_print(%s) falló — %s', shipment_id, e, exc_info=True)
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/ml/print-all', methods=['POST'])
def ml_print_all():
    """Imprime etiqueta + detalle por cada pedido, en pares consecutivos."""
    err = _require_license()
    if err: return err
    token = get_valid_token()
    if not token:
        return jsonify({'ok': False, 'need_login': True}), 401

    cfg     = load_config()
    want_a4 = cfg.get('output_mode') == 'a4'
    body    = request.get_json() or {}
    orders  = body.get('orders', [])
    if not orders:
        return jsonify({'ok': False, 'error': 'Sin envíos'}), 400

    combined   = b''
    failed     = []
    printed_a4 = 0

    for order in orders[:50]:
        sid = order.get('shipment_id')
        if not sid:
            continue
        try:
            zpl, is_zpl, err = _fetch_ml_label(sid, token, cfg, force_pdf=want_a4)
            if err:
                logger.error('ml_print_all(%s): _fetch_ml_label falló (force_pdf=%s) — %s',
                              sid, want_a4, err)
                failed.append(str(sid))
                continue
            order['shipment_id'] = str(sid)
            if order.get('items'):
                corr = next_correlative()
                order['correlative'] = corr

            if want_a4:
                # Modo A4: un trabajo de impresión por pedido — no se puede
                # concatenar como los bytes ZPL/TSPL de más abajo.
                label_img = label_image_for_a4(zpl, width_mm=float(cfg.get('label_width_mm', 100)),
                                                height_mm=float(cfg.get('label_height_mm', 150)),
                                                dpi=int(cfg.get('dpi', 203)))
                page = _build_a4_page(label_img, order, cfg)
                print_a4_image(cfg.get('a4_printer_name'), page)
                printed_a4 += 1
                _save_printed_order(order)
                continue

            if order.get('items'):
                chunk, _ = _print_ml_order(zpl, is_zpl, order, cfg)
                combined += chunk
            elif is_zpl:
                combined += zpl
            else:
                combined += pdf_to_tspl(zpl, width_mm=float(cfg.get('label_width_mm', 100)),
                                         height_mm=float(cfg.get('label_height_mm', 150)),
                                         dpi=int(cfg.get('dpi', 203)))
            _save_printed_order(order)
        except Exception as e:
            logger.error('ml_print_all(%s) falló — %s', sid, e, exc_info=True)
            failed.append(str(sid))

    if want_a4:
        if not printed_a4:
            return jsonify({'ok': False, 'error': 'No se pudo imprimir ninguna etiqueta.'}), 502
        return jsonify({'ok': True, 'printed': printed_a4, 'labels': printed_a4, 'failed': failed})

    if not combined:
        return jsonify({'ok': False, 'error': 'No se pudo obtener ninguna etiqueta.'}), 502

    n_labels = count_labels(combined)
    try:
        print_raw(cfg, combined)
        return jsonify({'ok': True, 'printed': len(orders) - len(failed),
                        'labels': n_labels, 'failed': failed})
    except socket.timeout:
        return jsonify({'ok': False, 'error': f"Timeout de impresora: {cfg['ip']}:{cfg['port']}"}), 500
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── TiendaNube helpers ────────────────────────────────────────────────────────

def _tn_get_valid_token():
    """Devuelve el access_token de TN (no expira) o None si no está configurado."""
    return load_config().get('tn_access_token') or None


def _tn_api(method, path, cfg, **kwargs):
    """Ejecuta un call a la API pública de TiendaNube con bearer auth."""
    store_id = str(cfg.get('tn_store_id', ''))
    token    = cfg.get('tn_access_token', '')
    if not store_id or not token:
        raise RuntimeError('TiendaNube no autenticado')
    return http.request(
        method,
        f'{TN_API_BASE}/{store_id}{path}',
        headers={
            'Authorization': f'bearer {token}',
            'User-Agent':    TN_USER_AGENT,
            'Content-Type':  'application/json',
        },
        timeout=15,
        **kwargs,
    )


def _tn_fetch_fulfillment_orders(order_id, cfg):
    """Devuelve lista de fulfillment orders de un pedido TN.
    [] solo cuando TiendaNube confirma que no hay ninguno (404) — cualquier otra
    falla (red, timeout, 5xx) se propaga como excepción en vez de disfrazarse de
    'sin pedidos', que antes hacía pensar que el pedido no era de Envío Nube."""
    r = _tn_api('GET', f'/orders/{order_id}/fulfillment-orders', cfg)
    if r.status_code == 200:
        return r.json()
    if r.status_code == 404:
        return []
    raise RuntimeError(f'TiendaNube no respondió bien al buscar el pedido (HTTP {r.status_code})')


def _tn_get_label_pdf(fulfillment_order_id: str, cfg) -> bytes:
    """Crea el despacho en Envío Nube (cirrus) y descarga el PDF de Andreani."""
    token    = cfg.get('tn_access_token', '')
    store_id = str(cfg.get('tn_store_id', ''))
    r = http.post(
        TN_CIRRUS,
        headers={
            'x-access-token': token,
            'x-store-id':     store_id,
            'Content-Type':   'application/json',
        },
        json={
            'createFile':          {'label': True, 'contentDeclaration': False},
            'fulfillmentOrderIds': [fulfillment_order_id],
        },
        timeout=25,
    )
    data = r.json()
    urls = data.get('labelUrls', [])
    errs = data.get('errors', [])
    if not urls:
        raise RuntimeError(f'cirrus no devolvió labelUrls. Errores: {errs}')
    pdf_r = http.get(urls[0], timeout=30)
    pdf_r.raise_for_status()
    return pdf_r.content


# ── TiendaNube OAuth ───────────────────────────────────────────────────────────

@app.route('/tn/auth/login')
def tn_auth_login():
    cfg       = load_config()
    client_id = cfg.get('tn_client_id', '').strip()
    if not client_id:
        return redirect('/?error=Configurar+App+ID+de+TiendaNube+primero')
    state = secrets.token_hex(16)
    _tn_state_store[state] = True
    url = (f'https://www.tiendanube.com/apps/{client_id}/authorize'
           f'?redirect_uri={TN_REDIRECT_URI}'
           f'&state={state}')
    return redirect(url)


@app.route('/tn/auth/callback')
def tn_auth_callback():
    code  = request.args.get('code')
    state = request.args.get('state', '')
    error = request.args.get('error')
    if error or not code:
        return redirect(f'/?tab=tn&error={error or "sin_codigo"}')
    if state not in _tn_state_store:
        return redirect('/?tab=tn&error=estado_invalido')
    _tn_state_store.pop(state, None)
    cfg = load_config()
    try:
        r = http.post(TN_TOKEN_URL, data={
            'client_id':     cfg.get('tn_client_id', ''),
            'client_secret': cfg.get('tn_client_secret', ''),
            'grant_type':    'authorization_code',
            'code':          code,
        }, timeout=15)
        data = r.json()
        if 'access_token' not in data:
            return redirect(f'/?tab=tn&error=token_error')
        cfg['tn_access_token'] = data['access_token']
        cfg['tn_store_id']     = data.get('user_id')
        save_config(cfg)
        return redirect('/?tab=tn')
    except Exception as e:
        return redirect(f'/?tab=tn&error={str(e)[:80]}')


@app.route('/tn/auth/status')
def tn_auth_status():
    token = _tn_get_valid_token()
    if not token:
        return jsonify({'logged_in': False})
    cfg = load_config()
    return jsonify({'logged_in': True, 'store_id': cfg.get('tn_store_id')})


@app.route('/tn/auth/logout', methods=['POST'])
def tn_auth_logout():
    cfg = load_config()
    cfg.pop('tn_access_token', None)
    cfg.pop('tn_store_id', None)
    save_config(cfg)
    return jsonify({'ok': True})


# ── TiendaNube Orders ──────────────────────────────────────────────────────────

@app.route('/tn/orders')
def tn_orders():
    """Lista pedidos TN abiertos y pagados (candidatos a imprimir etiqueta Andreani)."""
    token = _tn_get_valid_token()
    if not token:
        return jsonify({'ok': False, 'need_login': True}), 401
    cfg = load_config()
    try:
        r = _tn_api('GET', '/orders', cfg, params={
            'status':         'open',
            'payment_status': 'paid',
            'per_page':       50,
        })
        if r.status_code != 200:
            return jsonify({'ok': False, 'error': f'TN API: {r.status_code} {r.text[:200]}'}), 500
        orders = r.json()
        return jsonify({'ok': True, 'orders': orders, 'total': len(orders)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/tn/print/<int:order_id>', methods=['POST'])
def tn_print(order_id):
    """Obtiene fulfillment order, genera despacho en cirrus, convierte PDF a ZPL e imprime."""
    err = _require_license()
    if err: return err
    token = _tn_get_valid_token()
    if not token:
        return jsonify({'ok': False, 'need_login': True}), 401
    cfg = load_config()
    try:
        # 1. Buscar fulfillment order pendiente
        fos = _tn_fetch_fulfillment_orders(order_id, cfg)
        fo  = next(
            (f for f in fos if f.get('status') not in ('DISPATCHED', 'CANCELLED', 'FULFILLED')),
            None
        )
        if not fo:
            # Si ya estaba despachado pero queremos reimprimir, usar el primero disponible
            fo = fos[0] if fos else None
        if not fo:
            return jsonify({'ok': False, 'error': 'Sin fulfillment order para este pedido. ¿Es un pedido con Envío Nube?'}), 404

        # 2. Obtener PDF de Andreani
        pdf_bytes = _tn_get_label_pdf(fo['id'], cfg)

        # 3. Imprimir — hoja A4 en impresora normal, o ZPL/TSPL en térmica
        if cfg.get('output_mode') == 'a4':
            label_img = label_image_for_a4(pdf_bytes, width_mm=100.0, height_mm=150.0,
                                            dpi=int(cfg.get('dpi', 203)))
            page = _build_a4_page(label_img, None, cfg)
            print_a4_image(cfg.get('a4_printer_name'), page)
        else:
            # Andreani labels are always 100×150mm regardless of ML combo config
            zpl = _pdf_to_label(pdf_bytes, cfg, width_mm=100.0, height_mm=150.0)
            print_raw(cfg, zpl)
        return jsonify({'ok': True, 'labels': 1, 'fulfillment_id': fo['id']})

    except socket.timeout:
        return jsonify({'ok': False, 'error': f"Timeout de impresora: {cfg['ip']}:{cfg['port']}"}), 500
    except ConnectionRefusedError:
        return jsonify({'ok': False, 'error': 'Impresora no responde'}), 500
    except Exception as e:
        logger.error('tn_print(%s) falló — %s', order_id, e, exc_info=True)
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/tn/print-all', methods=['POST'])
def tn_print_all():
    """Imprime etiquetas Andreani de todos los pedidos TN indicados."""
    err = _require_license()
    if err: return err
    token = _tn_get_valid_token()
    if not token:
        return jsonify({'ok': False, 'need_login': True}), 401
    cfg       = load_config()
    want_a4   = cfg.get('output_mode') == 'a4'
    body      = request.get_json() or {}
    order_ids = body.get('order_ids', [])
    if not order_ids:
        return jsonify({'ok': False, 'error': 'Sin pedidos'}), 400

    combined = b''
    printed, failed = 0, []

    for oid in order_ids[:20]:
        try:
            fos = _tn_fetch_fulfillment_orders(oid, cfg)
            fo  = next(
                (f for f in fos if f.get('status') not in ('DISPATCHED', 'CANCELLED', 'FULFILLED')),
                fos[0] if fos else None
            )
            if not fo:
                failed.append(str(oid))
                continue
            pdf_bytes = _tn_get_label_pdf(fo['id'], cfg)
            if want_a4:
                # Modo A4: un trabajo de impresión por pedido, no se concatena.
                label_img = label_image_for_a4(pdf_bytes, width_mm=100.0, height_mm=150.0,
                                                dpi=int(cfg.get('dpi', 203)))
                page = _build_a4_page(label_img, None, cfg)
                print_a4_image(cfg.get('a4_printer_name'), page)
            else:
                zpl = _pdf_to_label(pdf_bytes, cfg, width_mm=100.0, height_mm=150.0)
                combined += zpl
            printed  += 1
        except Exception as e:
            logger.warning('tn_print_all: falló pedido %s — %s', oid, e)
            failed.append(str(oid))

    if want_a4:
        if not printed:
            return jsonify({'ok': False, 'error': 'No se pudo imprimir ninguna etiqueta.'}), 502
        return jsonify({'ok': True, 'printed': printed, 'labels': printed, 'failed': failed})

    if not combined:
        return jsonify({'ok': False, 'error': 'No se pudo obtener ninguna etiqueta.'}), 502

    try:
        print_raw(cfg, combined)
        return jsonify({'ok': True, 'printed': printed, 'labels': printed, 'failed': failed})
    except socket.timeout:
        return jsonify({'ok': False, 'error': f"Timeout de impresora: {cfg['ip']}:{cfg['port']}"}), 500
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500




# ── SSE stream ────────────────────────────────────────────────────────────────

@app.route('/ml/events')
def ml_events():
    def generate():
        q = queue_module.Queue(maxsize=200)
        with _sse_clients_lock:
            _sse_clients.append(q)
        try:
            # Enviar estado actual al conectar
            with _poll_lock:
                init_data = {
                    'enabled':     _poll['enabled'],
                    'auto_print':  _poll['auto_print'],
                    'interval':    _poll['interval'],
                    'status':      _poll['status'],
                    'checked_at':  _poll['checked_at'],
                    'initialized': _poll['initialized'],
                }
            yield f"event: poll_status\ndata: {json.dumps(init_data)}\n\n"
            while True:
                try:
                    msg = q.get(timeout=25)
                    yield msg
                except queue_module.Empty:
                    yield ': keepalive\n\n'   # heartbeat (SSE comment)
        finally:
            with _sse_clients_lock:
                try:
                    _sse_clients.remove(q)
                except ValueError:
                    pass

    return app.response_class(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control':     'no-cache',
            'X-Accel-Buffering': 'no',
        },
    )


# ── Auto-print config ──────────────────────────────────────────────────────────

@app.route('/ml/autoprint', methods=['GET'])
def ml_autoprint_get():
    with _poll_lock:
        return jsonify({
            'enabled':     _poll['enabled'],
            'auto_print':  _poll['auto_print'],
            'interval':    _poll['interval'],
            'status':      _poll['status'],
            'error':       _poll['error'],
            'checked_at':  _poll['checked_at'],
            'initialized': _poll['initialized'],
        })


@app.route('/ml/autoprint', methods=['POST'])
def ml_autoprint_set():
    body = request.get_json(silent=True) or {}
    with _poll_lock:
        if 'enabled' in body:
            new_val = bool(body['enabled'])
            if new_val and not _poll['enabled']:
                # Al activar: hacer snapshot inicial sin imprimir
                _poll['initialized'] = False
                _poll['known_ids']   = set()
                _poll['last_check']  = 0.0   # disparar de inmediato
            _poll['enabled'] = new_val
        if 'auto_print' in body:
            _poll['auto_print'] = bool(body['auto_print'])
        if 'interval' in body:
            _poll['interval'] = max(30, int(body['interval']))
        enabled, auto_print, interval = _poll['enabled'], _poll['auto_print'], _poll['interval']

    _update_tray_icon()

    # Solo se recuerda el intervalo elegido (preferencia sin sorpresas). El
    # on/off NO se persiste a propósito: si el proceso se cierra de verdad
    # (crash, "Salir", apagado/reinicio de la PC) el monitoreo tiene que
    # arrancar apagado la próxima vez — cerrar la ventana con la X no cuenta
    # como "cerrar de verdad" (se minimiza a la bandeja, el proceso sigue
    # vivo y el estado ya sigue tal cual sin necesidad de guardar nada).
    cfg = load_config()
    cfg['_autoprint_interval'] = interval
    save_config(cfg)

    return jsonify({'ok': True, 'enabled': enabled,
                    'auto_print': auto_print, 'interval': interval})


# ── Startup ────────────────────────────────────────────────────────────────────

def run_flask():
    import logging
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    app.run(host='127.0.0.1', port=5050, debug=False, use_reloader=False, threaded=True)


# ── System tray ────────────────────────────────────────────────────────────────

_tray_icon = None       # referencia global para notificaciones desde el poll worker
_webview_window = None  # referencia global a la ventana nativa (pywebview)


def tray_notify(title, message):
    """Muestra una notificación Windows nativa desde cualquier hilo."""
    try:
        if _tray_icon:
            _tray_icon.notify(message, title)
    except Exception:
        pass


def _make_tray_image(active=False):
    """Crea el ícono de 64×64 px para la bandeja del sistema.
    La luz indicadora se ve verde solo si el monitoreo está activo — así el
    operador sabe de un vistazo, sin abrir la ventana, si está funcionando."""
    from PIL import Image, ImageDraw
    img  = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Fondo redondeado naranja
    draw.rounded_rectangle([0, 0, 63, 63], radius=14, fill='#f5a623')
    # Cuerpo de impresora
    draw.rounded_rectangle([10, 24, 54, 44], radius=4, fill='#1c1e26')
    # Bandeja de papel (arriba)
    draw.rounded_rectangle([16, 16, 48, 26], radius=2, fill='#1c1e26')
    # Etiqueta saliendo (abajo)
    draw.rounded_rectangle([18, 42, 46, 54], radius=2, fill='white')
    # Líneas de código de barras en la etiqueta
    for x in (22, 26, 30, 34, 38, 42):
        draw.line([(x, 44), (x, 52)], fill='#333', width=2)
    # Luz indicadora: verde = monitoreando, gris = inactivo
    draw.ellipse([44, 29, 51, 36], fill='#4caf88' if active else '#555555')
    return img


def _update_tray_icon():
    """Refleja en el ícono de la bandeja si el monitoreo está activo o no.
    Se llama cada vez que cambia _poll['enabled'] (activar/detener)."""
    if not _tray_icon:
        return
    try:
        with _poll_lock:
            active = _poll['enabled']
        _tray_icon.icon = _make_tray_image(active)
    except Exception:
        pass


def _run_tray():
    """Inicia el ícono en la bandeja del sistema (bloquea el hilo principal)."""
    global _tray_icon
    import pystray

    def show_window(icon, item):
        if _webview_window:
            _webview_window.show()

    def get_status(item):
        with _poll_lock:
            enabled   = _poll['enabled']
            auto_p    = _poll['auto_print']
            checked   = _poll['checked_at']
        ago = ''
        if checked:
            s = int(time.time() - checked)
            ago = f' (hace {s}s)' if s < 60 else f' (hace {s//60}min)'
        if not enabled:
            return 'Monitoreo: inactivo'
        return f'Monitoreo: activo{"  · Auto-imprimir" if auto_p else ""}{ago}'

    menu = pystray.Menu(
        pystray.MenuItem('Abrir panel de pedidos', show_window, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(get_status, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('Salir', lambda icon, item: (icon.stop(), os._exit(0))),
    )

    with _poll_lock:
        initial_active = _poll['enabled']

    _tray_icon = pystray.Icon(
        name    = 'enviobot',
        icon    = _make_tray_image(initial_active),
        title   = 'EnvioBot',
        menu    = menu,
    )
    _tray_icon.run()


def _try_focus_existing_instance(port=5050):
    """Si ya hay una instancia de EnvioBot corriendo (monitoreando o no), le
    pide que muestre su ventana y devuelve True — el proceso que llama a
    esto tiene que terminar sin arrancar nada más. Evita el problema de abrir
    el .exe de nuevo mientras ya está corriendo minimizado: en vez de matar
    la instancia vieja (cortando el monitoreo) y abrir una segunda, se
    reusa la que ya está."""
    if not http:
        return False
    try:
        r = http.get(f'http://127.0.0.1:{port}/config', timeout=1.5)
        if r.status_code != 200:
            return False
        http.post(f'http://127.0.0.1:{port}/internal/show', timeout=1.5)
        print("  EnvioBot ya está corriendo — mostrando la ventana existente.")
        return True
    except Exception:
        return False


def _kill_existing_on_port(port=5050):
    """Mata cualquier proceso que ya esté escuchando en el puerto pero no
    responde como EnvioBot (zombie) — red de seguridad, no el camino normal."""
    import subprocess
    try:
        result = subprocess.run(
            ['netstat', '-ano'],
            capture_output=True, text=True, timeout=5
        )
        own_pid = str(os.getpid())
        killed = []
        for line in result.stdout.splitlines():
            if f':{port} ' in line and 'LISTEN' in line:
                parts = line.split()
                pid = parts[-1] if parts else ''
                if pid and pid != own_pid and pid != '0':
                    subprocess.run(['taskkill', '/F', '/PID', pid],
                                   capture_output=True, timeout=5)
                    killed.append(pid)
        if killed:
            print(f"  Instancia(s) anterior(es) terminada(s): PID {', '.join(killed)}")
            time.sleep(0.8)
    except Exception:
        pass


def _restore_poll_state():
    """Restaura solo el intervalo elegido (preferencia). El monitoreo arranca
    apagado siempre que el PROCESO se reinicia de verdad (crash, 'Salir',
    apagado/reinicio de la PC) — a propósito, para no auto-imprimir sin que
    el operador lo haya prendido de nuevo él mismo. Cerrar la ventana con la
    X no pasa por acá: el proceso sigue vivo y el estado ya sigue como estaba."""
    cfg = load_config()
    with _poll_lock:
        _poll['interval'] = max(30, int(cfg.get('_autoprint_interval', 60)))


def start():
    global _webview_window

    if _try_focus_existing_instance(5050):
        return  # ya hay una instancia corriendo, no arrancar una segunda

    _kill_existing_on_port(5050)
    _restore_poll_state()
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=_poll_worker, daemon=True).start()
    threading.Thread(target=_announcement_worker, daemon=True).start()
    time.sleep(1.2)

    frozen = getattr(sys, 'frozen', False)

    # La bandeja corre en su propio hilo: pywebview necesita el hilo principal para sí mismo.
    if frozen:
        threading.Thread(target=_run_tray, daemon=True).start()
    else:
        print("=" * 50)
        print("  EnvioBot — http://localhost:5050")
        print("  Cerrá la ventana o Ctrl+C para salir")
        print("=" * 50)

    _webview_window = webview.create_window(
        'EnvioBot', 'http://localhost:5050',
        width=1180, height=820, min_size=(900, 650),
    )

    def _on_closing():
        # Empaquetado: la X minimiza a la bandeja, el monitoreo sigue corriendo.
        # "Salir" desde la bandeja es la única forma de terminar el proceso.
        if frozen:
            _webview_window.hide()
            return False
        return True

    _webview_window.events.closing += _on_closing

    webview.start()  # bloquea el hilo principal hasta que la ventana se cierre de verdad

    if not frozen:
        os._exit(0)


if __name__ == '__main__':
    start()
