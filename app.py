from flask import Flask, request, jsonify, send_from_directory, redirect
import socket
import json
import os
import sys
import threading
import webbrowser
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

DEFAULT_CONFIG = {
    'ip': '192.168.1.100',
    'port': 9100,
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

# PKCE: almacena {state: code_verifier} durante el flujo OAuth (en memoria, vida corta)
_pkce_store = {}

# ── TiendaNube constants ───────────────────────────────────────────────────────
TN_API_BASE     = 'https://api.tiendanube.com/v1'
TN_TOKEN_URL    = 'https://www.tiendanube.com/apps/authorize/token'
TN_CIRRUS       = 'https://cirrus.tiendanube.com/nuvem-envio/dispatches'
TN_REDIRECT_URI = 'https://willyjaeger.github.io/tsc-label-printer/tn-callback.html'
TN_USER_AGENT   = 'TSC-Label-Printer/1.0 (guillermo.jaeger@gmail.com)'

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

def send_to_printer(ip, port, data):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(10)
    try:
        s.connect((ip, int(port)))
        if isinstance(data, str):
            data = data.encode('utf-8')
        s.sendall(data)
    finally:
        s.close()


def query_printer(ip, port, cmd, read_bytes=512, timeout=5):
    """Envía un comando y lee la respuesta del printer. Devuelve bytes o None."""
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
    except Exception:
        return None
    finally:
        s.close()


def parse_hs_gap(hs_response):
    """
    Parsea la respuesta ~HS del TSC para extraer el gap detectado.
    El ~HS devuelve 3 paquetes STX...ETX. El gap está en el segundo paquete,
    bytes 8-9 (little-endian, en dots de 1/100 pulgada).
    Devuelve gap en mm o None si no se puede parsear.
    """
    if not hs_response or len(hs_response) < 20:
        return None
    try:
        # Buscar paquetes delimitados por STX (0x02) / ETX (0x03)
        packets = []
        i = 0
        while i < len(hs_response):
            if hs_response[i] == 0x02:
                end = hs_response.find(0x03, i + 1)
                if end != -1:
                    packets.append(hs_response[i+1:end])
                    i = end + 1
                    continue
            i += 1
        if len(packets) >= 2:
            pkt = packets[1]  # segundo paquete contiene dimensiones
            if len(pkt) >= 10:
                gap_hundredths = int.from_bytes(pkt[8:10], 'little')
                gap_mm = gap_hundredths * 25.4 / 100
                if 1.0 <= gap_mm <= 30.0:  # rango razonable
                    return round(gap_mm, 1)
    except Exception:
        pass
    return None


# ── ML Auth helpers ────────────────────────────────────────────────────────────

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
            return None
        cfg['ml_access_token']    = data['access_token']
        cfg['ml_refresh_token']   = data.get('refresh_token', cfg.get('ml_refresh_token'))
        cfg['ml_token_expires_at'] = time.time() + data.get('expires_in', 21600)
        save_config(cfg)
        return cfg['ml_access_token']
    except Exception:
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
            with _poll_lock:
                _poll['status']     = 'error'
                _poll['error']      = 'Sin sesión ML activa'
                _poll['last_check'] = now
                _poll['checked_at'] = now
            _push_event('poll_status', {'status': 'error',
                                        'error':  'Sin sesión ML activa',
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

            new_orders = [o for o in printable if o['id'] not in known_ids]

            # Detectar pedidos impresos que desaparecieron (posible cancelación)
            if was_initialized:
                disappeared = known_ids - current_ids
                if disappeared:
                    local = load_orders()
                    for saved in local:
                        oid = saved.get('order_id')
                        if oid and int(oid) in disappeared:
                            ship_st = saved.get('shipment_status', '')
                            if ship_st not in ('shipped', 'delivered', 'not_delivered'):
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
                    for o in new_orders:
                        sid = o.get('shipping', {}).get('id')
                        if not sid:
                            continue
                        try:
                            r = http.get(
                                f'{ML_API}/shipment_labels',
                                params={'shipment_ids': sid, 'response_type': 'zpl2'},
                                headers={'Authorization': f'Bearer {token}'},
                                timeout=20,
                            )
                            zpl, err = _extract_zpl(
                                r.content, r.status_code, r.text,
                                r.headers.get('content-type', ''),
                            )
                            if err:
                                _push_event('print_error', {'shipment_id': sid, 'error': err})
                                continue
                            order_data = {
                                'order_id':    o['id'],
                                'shipment_id': str(sid),
                                'buyer':       (o.get('_shipment') or {}).get('receiver_name')
                                               or (o.get('buyer') or {}).get('nickname', ''),
                                'items':       [
                                    {'qty': i['quantity'],
                                     'title': (i.get('item') or {}).get('title', '')}
                                    for i in o.get('order_items', [])
                                ],
                            }
                            corr = next_correlative()
                            order_data['correlative'] = corr
                            payload, _ = _print_ml_order(zpl, order_data, cfg)
                            send_to_printer(cfg['ip'], cfg['port'], payload)
                            _save_printed_order(order_data)
                            _push_event('auto_printed', {
                                'shipment_id': sid,
                                'order_id':    o['id'],
                                'buyer':       order_data['buyer'],
                            })
                        except Exception as pe:
                            _push_event('print_error', {'shipment_id': sid, 'error': str(pe)})

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
    return send_from_directory(BUNDLE_DIR, 'index.html')


# ── Printer config ─────────────────────────────────────────────────────────────

@app.route('/config', methods=['GET'])
def get_config():
    cfg = load_config()
    # No exponer tokens ni campos internos al frontend
    _hidden = {'ml_access_token', 'ml_refresh_token', 'ml_token_expires_at', 'tn_access_token', 'tn_store_id'}
    safe = {k: v for k, v in cfg.items() if k not in _hidden and not k.startswith('_')}
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


# ── Print (archivo manual) ─────────────────────────────────────────────────────

@app.route('/print', methods=['POST'])
def print_label():
    cfg = load_config()
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
        send_to_printer(cfg['ip'], cfg['port'], raw)
        return jsonify({'ok': True, 'labels': n_labels})
    except socket.timeout:
        return jsonify({'ok': False, 'error': f"Timeout: no se pudo conectar a {cfg['ip']}:{cfg['port']}"}), 500
    except ConnectionRefusedError:
        return jsonify({'ok': False, 'error': 'Conexión rechazada: verificar que la impresora esté encendida.'}), 500
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── Calibración ────────────────────────────────────────────────────────────────

@app.route('/calibrate', methods=['POST'])
def calibrate():
    cfg = load_config()
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
        send_to_printer(cfg['ip'], cfg['port'], zpl)
        return jsonify({'ok': True, 'zpl': zpl, 'dots': {'height': height_dots, 'backfeed': backfeed_dots}})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/autocal', methods=['POST'])
def autocal():
    cfg = load_config()
    try:
        dpi         = int(cfg.get('dpi', 203))
        height_mm   = float(cfg.get('label_height_mm', 150))
        height_dots = round(height_mm * dpi / 25.4)

        send_to_printer(cfg['ip'], cfg['port'], '~JC')
        time.sleep(5)  # esperar que la impresora termine la calibración

        # Intentar leer el gap real con ~HS
        hs = query_printer(cfg['ip'], cfg['port'], '~HS', read_bytes=64, timeout=3)
        gap_mm = parse_hs_gap(hs)
        if gap_mm is None:
            gap_mm = float(cfg.get('label_gap_mm', 10))
            gap_source = 'config'
        else:
            gap_source = f'medido ({gap_mm} mm)'
            # Guardar el gap detectado para futuros backfeeds
            cfg['label_gap_mm'] = gap_mm
            save_config(cfg)

        gap_dots      = round(gap_mm * dpi / 25.4)
        # ~JC avanza ~3 pitches (etiqueta + gap); retrocedemos esa misma distancia
        backfeed_dots = (height_dots + gap_dots) * 3
        send_to_printer(cfg['ip'], cfg['port'], f'BACKFEED {backfeed_dots}\r\n'.encode())
        return jsonify({'ok': True, 'gap_mm': gap_mm, 'gap_source': gap_source, 'backfeed_dots': backfeed_dots})
    except socket.timeout:
        return jsonify({'ok': False, 'error': f"Timeout: no se pudo conectar a {cfg['ip']}:{cfg['port']}"}), 500
    except ConnectionRefusedError:
        return jsonify({'ok': False, 'error': f"Conexión rechazada en {cfg['ip']}:{cfg['port']} — ¿está encendida?"}), 500
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/testprint', methods=['POST'])
def testprint():
    cfg = load_config()
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
        send_to_printer(cfg['ip'], cfg['port'], zpl)
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

        return jsonify({'ok': True, 'orders': printable, 'total': len(printable)})
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


def count_labels(data: bytes) -> int:
    """Cuenta etiquetas en un bloque ZPL contando ocurrencias de ^XA."""
    import re as _re
    return max(1, len(_re.findall(rb'\^XA', data, _re.IGNORECASE)))


def pdf_to_zpl(pdf_bytes: bytes, width_mm: float = 100.0,
               height_mm: float = 150.0, dpi: int = 203) -> bytes:
    """Convierte la primera página de un PDF a ZPL ^GFA listo para imprimir.
    Requiere pymupdf (fitz) y Pillow."""
    try:
        import fitz  # pymupdf
    except ImportError:
        raise RuntimeError('pymupdf no instalado. Ejecutar: pip install pymupdf')
    try:
        from PIL import Image
        import io as _io
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
    rect = page.rect  # en puntos (1 pt = 1/72 inch)

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
        # PIL '1': bit 0 = negro, bit 1 = blanco → ZPL: bit 1 = imprimir → invertir
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

    hex_data = bitmap.hex().upper()
    return (f'^XA\r\n^FO0,0\r\n'
            f'^GFA,{total_bytes},{total_bytes},{bytes_per_row},{hex_data}\r\n'
            f'^XZ\r\n').encode('ascii')


def _ascii_zpl(text):
    table = str.maketrans('áéíóúÁÉÍÓÚñÑüÜ', 'aeiouAEIOUnNuU')
    return text.translate(table)


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
        label = f'x{qty}  {title}'
        nlines = max(1, min(6, (len(label) + cpl - 1) // cpl))
        lines.append(f'^FO{m},{y + 8}^GB14,14,14^FS')
        lines.append(f'^FO{m + 22},{y}^ADN,{font_h},{font_w}^FB{fld_w},{nlines},6,L,0^FD{label}^FS')
        y += nlines * line_h + 14

    lines.append('^XZ')
    return ('\r\n'.join(lines) + '\r\n').encode('latin-1', errors='replace')


def _build_combo_zpl(order_data, ml_zpl_bytes, cfg):
    """
    Etiqueta combo 100×190 mm con troquel:
      - Top die_cut_mm: correlativo + items (sin barcode ni datos extra)
      - Bottom label_height_mm: ZPL de ML desplazado die_cut_dots hacia abajo
    Todo en un único ^XA...^XZ → una sola etiqueta física.
    """
    import re

    dpi         = int(cfg.get('dpi', 203))
    dpm         = dpi / 25.4
    w           = round(float(cfg.get('label_width_mm',  100)) * dpm)
    ship_h_mm   = float(cfg.get('label_height_mm', 150))
    die_cut_mm  = float(cfg.get('ml_die_cut_mm', 40))
    die_dots    = round(die_cut_mm * dpm)
    total_dots  = round((ship_h_mm + die_cut_mm) * dpm)

    correlative = order_data.get('correlative')
    items       = order_data.get('items', [])
    m = 20
    y = 10
    detail = []

    # Correlativo compacto en el troquel
    if correlative is not None:
        detail.append(f'^FO{m},{y}^A0N,46,24^FD#{correlative:03d}^FS')
        y += 54

    # Items dentro del troquel
    fh, fw   = 22, 10
    line_h   = fh + 5
    fld_w    = w - m * 2
    cpl      = max(10, fld_w // fw)
    for item in items:
        if y > die_dots - line_h - 8:
            detail.append(f'^FO{m},{y}^ADN,18,8^FD...^FS')
            break
        qty   = item.get('qty', 1)
        title = _ascii_zpl(str(item.get('title', '')))
        label = f'x{qty} {title}'
        nl    = max(1, min(3, (len(label) + cpl - 1) // cpl))
        detail.append(f'^FO{m},{y}^ADN,{fh},{fw}^FB{fld_w},{nl},4,L,0^FD{label}^FS')
        y += nl * line_h + 4

    # Línea separadora en el troquel
    detail.append(f'^FO0,{die_dots - 3}^GB{w},3,3^FS')

    # ── Procesar ZPL de ML ───────────────────────────────────────────────────
    ml_str = ml_zpl_bytes.decode('latin-1', errors='replace')

    # Extraer solo el cuerpo (entre ^XA y ^XZ del primer bloque)
    body_match = re.search(r'\^XA(.*?)\^XZ', ml_str, re.DOTALL | re.IGNORECASE)
    ml_body = body_match.group(1) if body_match else ml_str

    # Eliminar directivas de tamaño — las reemplazamos con las nuestras
    ml_body = re.sub(r'\^PW\d+', '', ml_body, flags=re.IGNORECASE)
    ml_body = re.sub(r'\^LL\d+', '', ml_body, flags=re.IGNORECASE)
    ml_body = re.sub(r'\^LT-?\d+', '', ml_body, flags=re.IGNORECASE)
    ml_body = re.sub(r'\^MN[A-Z]', '', ml_body, flags=re.IGNORECASE)

    # Desplazar todos los ^FO y-coords hacia abajo por die_dots
    def shift_fo(m_):
        return f'^FO{m_.group(1)},{int(m_.group(2)) + die_dots}'
    ml_body = re.sub(r'\^FO(\d+),(\d+)', shift_fo, ml_body, flags=re.IGNORECASE)

    # ── Ensamblar ZPL único ──────────────────────────────────────────────────
    parts = ['^XA', f'^PW{w}', f'^LL{total_dots}', '^LT0']
    parts.extend(detail)
    parts.append(ml_body.strip())
    parts.append('^XZ')

    return ('\r\n'.join(parts) + '\r\n').encode('latin-1', errors='replace')


def _print_ml_order(zpl_bytes, order_data, cfg):
    """
    Imprime una orden ML según el tipo de etiqueta configurado.
    Devuelve (payload_bytes, labels_count).
    """
    label_type = cfg.get('ml_label_type', 'standard')
    corr = order_data.get('correlative') or next_correlative()
    order_data['correlative'] = corr

    if label_type == 'combo':
        payload = _build_combo_zpl(order_data, zpl_bytes, cfg)
        return payload, 1
    else:
        payload = (_inject_correlative_into_zpl(zpl_bytes, corr)
                   + _build_detail_zpl(order_data, cfg))
        return payload, 2


@app.route('/local/orders')
def local_orders_endpoint():
    return jsonify({'ok': True, 'orders': load_orders()})


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


@app.route('/ml/print/<int:shipment_id>', methods=['POST'])
def ml_print(shipment_id):
    token = get_valid_token()
    if not token:
        return jsonify({'ok': False, 'need_login': True}), 401

    cfg        = load_config()
    order_data = request.get_json(silent=True) or {}
    try:
        r = http.get(
            f'{ML_API}/shipment_labels',
            params={'shipment_ids': shipment_id, 'response_type': 'zpl2'},
            headers={'Authorization': f'Bearer {token}'},
            timeout=20,
        )
        zpl, err = _extract_zpl(r.content, r.status_code, r.text, r.headers.get('content-type', ''))
        if err:
            return jsonify({'ok': False, 'error': err}), 502

        if order_data.get('items'):
            corr = next_correlative()
            order_data['shipment_id'] = str(shipment_id)
            order_data['correlative'] = corr
            payload, n_labels = _print_ml_order(zpl, order_data, cfg)
        else:
            payload  = zpl
            n_labels = count_labels(payload)
        send_to_printer(cfg['ip'], cfg['port'], payload)
        _save_printed_order(order_data)
        return jsonify({'ok': True, 'labels': n_labels})
    except socket.timeout:
        return jsonify({'ok': False, 'error': f"Timeout de impresora: {cfg['ip']}:{cfg['port']}"}), 500
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/ml/print-all', methods=['POST'])
def ml_print_all():
    """Imprime etiqueta + detalle por cada pedido, en pares consecutivos."""
    token = get_valid_token()
    if not token:
        return jsonify({'ok': False, 'need_login': True}), 401

    cfg    = load_config()
    body   = request.get_json() or {}
    orders = body.get('orders', [])
    if not orders:
        return jsonify({'ok': False, 'error': 'Sin envíos'}), 400

    combined = b''
    failed   = []

    for order in orders[:50]:
        sid = order.get('shipment_id')
        if not sid:
            continue
        try:
            r = http.get(
                f'{ML_API}/shipment_labels',
                params={'shipment_ids': sid, 'response_type': 'zpl2'},
                headers={'Authorization': f'Bearer {token}'},
                timeout=20,
            )
            zpl, err = _extract_zpl(r.content, r.status_code, r.text, r.headers.get('content-type', ''))
            if err:
                failed.append(str(sid))
                continue
            order['shipment_id'] = str(sid)
            if order.get('items'):
                corr = next_correlative()
                order['correlative'] = corr
                chunk, _ = _print_ml_order(zpl, order, cfg)
                combined += chunk
            else:
                combined += zpl
            _save_printed_order(order)
        except Exception:
            failed.append(str(sid))

    if not combined:
        return jsonify({'ok': False, 'error': 'No se pudo obtener ninguna etiqueta.'}), 502

    n_labels = count_labels(combined)
    try:
        send_to_printer(cfg['ip'], cfg['port'], combined)
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
    """Devuelve lista de fulfillment orders de un pedido TN, o [] si no hay."""
    try:
        r = _tn_api('GET', f'/orders/{order_id}/fulfillment-orders', cfg)
        return r.json() if r.status_code == 200 else []
    except Exception:
        return []


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

        # 3. Convertir PDF → ZPL
        zpl = pdf_to_zpl(
            pdf_bytes,
            width_mm=float(cfg.get('label_width_mm', 100)),
            height_mm=float(cfg.get('label_height_mm', 150)),
            dpi=int(cfg.get('dpi', 203)),
        )

        # 4. Imprimir
        send_to_printer(cfg['ip'], cfg['port'], zpl)
        return jsonify({'ok': True, 'labels': 1, 'fulfillment_id': fo['id']})

    except socket.timeout:
        return jsonify({'ok': False, 'error': f"Timeout de impresora: {cfg['ip']}:{cfg['port']}"}), 500
    except ConnectionRefusedError:
        return jsonify({'ok': False, 'error': 'Impresora no responde'}), 500
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/tn/print-all', methods=['POST'])
def tn_print_all():
    """Imprime etiquetas Andreani de todos los pedidos TN indicados."""
    token = _tn_get_valid_token()
    if not token:
        return jsonify({'ok': False, 'need_login': True}), 401
    cfg      = load_config()
    body     = request.get_json() or {}
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
            zpl = pdf_to_zpl(
                pdf_bytes,
                width_mm=float(cfg.get('label_width_mm', 100)),
                height_mm=float(cfg.get('label_height_mm', 150)),
                dpi=int(cfg.get('dpi', 203)),
            )
            combined += zpl
            printed  += 1
        except Exception:
            failed.append(str(oid))

    if not combined:
        return jsonify({'ok': False, 'error': 'No se pudo obtener ninguna etiqueta.'}), 502

    try:
        send_to_printer(cfg['ip'], cfg['port'], combined)
        return jsonify({'ok': True, 'printed': printed, 'labels': printed, 'failed': failed})
    except socket.timeout:
        return jsonify({'ok': False, 'error': f"Timeout de impresora: {cfg['ip']}:{cfg['port']}"}), 500
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── Retroceder papel ──────────────────────────────────────────────────────────

@app.route('/retract', methods=['POST'])
def retract():
    """Envía comando TSPL REVERSE {dots} para retroceder el papel."""
    cfg  = load_config()
    body = request.get_json(silent=True) or {}
    dots = int(body.get('dots', 0))
    if dots <= 0:
        return jsonify({'ok': False, 'error': 'dots debe ser > 0'}), 400
    try:
        # TSPL: BACKFEED n retrocede n dots (comando nativo TSC)
        send_to_printer(cfg['ip'], cfg['port'], f'BACKFEED {dots}\r\n'.encode())
        return jsonify({'ok': True, 'dots': dots})
    except socket.timeout:
        return jsonify({'ok': False, 'error': f"Timeout: {cfg['ip']}:{cfg['port']}"}), 500
    except ConnectionRefusedError:
        return jsonify({'ok': False, 'error': 'Conexión rechazada'}), 500
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
    with _poll_lock:
        return jsonify({'ok': True, 'enabled': _poll['enabled'],
                        'auto_print': _poll['auto_print'],
                        'interval': _poll['interval']})


# ── Startup ────────────────────────────────────────────────────────────────────

def run_flask():
    import logging
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    app.run(host='127.0.0.1', port=5050, debug=False, use_reloader=False, threaded=True)


# ── System tray ────────────────────────────────────────────────────────────────

_tray_icon = None   # referencia global para notificaciones desde el poll worker


def tray_notify(title, message):
    """Muestra una notificación Windows nativa desde cualquier hilo."""
    try:
        if _tray_icon:
            _tray_icon.notify(message, title)
    except Exception:
        pass


def _make_tray_image():
    """Crea el ícono de 64×64 px para la bandeja del sistema."""
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
    # Luz indicadora verde
    draw.ellipse([44, 29, 51, 36], fill='#4caf88')
    return img


def _run_tray():
    """Inicia el ícono en la bandeja del sistema (bloquea el hilo principal)."""
    global _tray_icon
    import pystray

    def open_browser(icon, item):
        webbrowser.open('http://localhost:5050')

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
        pystray.MenuItem('Abrir panel de pedidos', open_browser, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(get_status, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('Salir', lambda icon, item: (icon.stop(), os._exit(0))),
    )

    _tray_icon = pystray.Icon(
        name    = 'impresor-etiquetas',
        icon    = _make_tray_image(),
        title   = 'Impresor de Etiquetas',
        menu    = menu,
    )
    _tray_icon.run()


def start():
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=_poll_worker, daemon=True).start()
    time.sleep(1.2)
    webbrowser.open('http://localhost:5050')

    if getattr(sys, 'frozen', False):
        _run_tray()
    else:
        print("=" * 50)
        print("  Impresor de Etiquetas — http://localhost:5050")
        print("  Ctrl+C para cerrar")
        print("=" * 50)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    start()
