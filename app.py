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

DEFAULT_CONFIG = {
    'ip': '192.168.1.100',
    'port': 9100,
    'label_height_mm': 150,
    'label_width_mm': 100,
    'backfeed_dots': 0,
    'media_type': 'gap',
    'dpi': 203,
    'ml_client_id': '',
    'ml_client_secret': '',
}

ML_AUTH_URL  = 'https://auth.mercadolibre.com.ar/authorization'
ML_TOKEN_URL = 'https://api.mercadolibre.com/oauth/token'
ML_API       = 'https://api.mercadolibre.com'
REDIRECT_URI = 'https://willyjaeger.github.io/tsc-label-printer/callback.html'

# PKCE: almacena {state: code_verifier} durante el flujo OAuth (en memoria, vida corta)
_pkce_store = {}

# ── SSE / Auto-print state ──────────────────────────────────────────────────────
_sse_clients      = []           # una Queue por cada cliente SSE conectado
_sse_clients_lock = threading.Lock()

_poll = {
    'enabled':     False,
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

            if not was_initialized:
                # Primera pasada: solo registrar IDs existentes, no imprimir
                _push_event('poll_status', {
                    'status': 'idle', 'checked_at': now,
                    'initialized': True, 'count': len(printable),
                })
            elif new_orders:
                # Pedidos nuevos detectados → notificar y auto-imprimir
                _push_event('new_orders', {
                    'count':      len(new_orders),
                    'checked_at': now,
                    'orders': [{'id': o['id'],
                                'shipment_id': o.get('shipping', {}).get('id')}
                               for o in new_orders],
                })
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
                        payload = (_inject_correlative_into_zpl(zpl, corr)
                                   + _build_detail_zpl(order_data, cfg))
                        send_to_printer(cfg['ip'], cfg['port'], payload)
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
    # No exponer tokens al frontend
    safe = {k: v for k, v in cfg.items() if not k.startswith('ml_access') and not k.startswith('ml_refresh') and not k.startswith('ml_token_exp')}
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
        dpi        = int(cfg.get('dpi', 203))
        height_mm  = float(cfg.get('label_height_mm', 150))
        height_dots = round(height_mm * dpi / 25.4)
        # ~JC detecta gap avanzando ~3 etiquetas; luego retrocedemos esa misma distancia
        backfeed_dots = height_dots * 3
        import time
        send_to_printer(cfg['ip'], cfg['port'], '~JC')
        time.sleep(4)  # esperar que la impresora termine la calibración
        send_to_printer(cfg['ip'], cfg['port'], f'BACKFEED {backfeed_dots}\r\n'.encode())
        return jsonify({'ok': True})
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

        # 3. Filtrar Full y enriquecer con datos de envío
        printable = []
        for o in all_orders:
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
    fld_w = w - m - 22 - m   # ancho disponible después de la viñeta
    cpl   = max(25, fld_w // 14)   # chars estimados por línea (ADN,34,17)
    first = True
    for item in items:
        if y > h - 55:
            lines.append(f'^FO{m},{y}^ADN,22,11^FD... y mas articulos^FS')
            break
        if not first:
            hsep(1)
        first = False
        qty   = item.get('qty', 1)
        title = _ascii_zpl(str(item.get('title', '')))
        label = f'x{qty}  {title}'
        nlines = max(1, min(3, (len(label) + cpl - 1) // cpl))
        lines.append(f'^FO{m},{y + 10}^GB14,14,14^FS')
        lines.append(f'^FO{m + 22},{y}^ADN,34,17^FB{fld_w},{nlines},0,L,0^FD{label}^FS')
        y += nlines * 44 + 5

    lines.append('^XZ')
    return ('\r\n'.join(lines) + '\r\n').encode('latin-1', errors='replace')


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
        results[status] = {
            'count': data.get('paging', {}).get('total', '?'),
            'sample': [{'id': o['id'], 'date_closed': o.get('date_closed'), 'last_updated': o.get('last_updated')} for o in data.get('results', [])[:3]],
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
            payload = _inject_correlative_into_zpl(zpl, corr) + _build_detail_zpl(order_data, cfg)
        else:
            payload = zpl

        n_labels = count_labels(payload)
        send_to_printer(cfg['ip'], cfg['port'], payload)
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
                combined += _inject_correlative_into_zpl(zpl, corr) + _build_detail_zpl(order, cfg)
            else:
                combined += zpl
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
        if 'interval' in body:
            _poll['interval'] = max(30, int(body['interval']))
    with _poll_lock:
        return jsonify({'ok': True, 'enabled': _poll['enabled'],
                        'interval': _poll['interval']})


# ── Startup ────────────────────────────────────────────────────────────────────

def run_flask():
    import logging
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    app.run(host='127.0.0.1', port=5050, debug=False, use_reloader=False, threaded=True)


def start():
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=_poll_worker, daemon=True).start()
    time.sleep(1.2)
    webbrowser.open('http://localhost:5050')

    if getattr(sys, 'frozen', False):
        _show_control_window()
    else:
        print("=" * 45)
        print("  TSC Label Printer — http://localhost:5050")
        print("  Ctrl+C para cerrar")
        print("=" * 45)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


def _show_control_window():
    import tkinter as tk
    root = tk.Tk()
    root.title("TSC Label Printer")
    root.geometry("300x110")
    root.resizable(False, False)
    root.configure(bg='#1c1e26')

    tk.Label(root, text="TSC Label Printer está corriendo",
             bg='#1c1e26', fg='#e8e9ed', font=('Segoe UI', 10, 'bold')).pack(pady=(18, 4))
    tk.Label(root, text="http://localhost:5050",
             bg='#1c1e26', fg='#f5a623', font=('Segoe UI', 9)).pack(pady=(0, 10))

    f = tk.Frame(root, bg='#1c1e26')
    f.pack()
    tk.Button(f, text="Abrir navegador", bg='#f5a623', fg='#111',
              font=('Segoe UI', 9, 'bold'), bd=0, padx=12, pady=5, cursor='hand2',
              relief='flat', command=lambda: webbrowser.open('http://localhost:5050')).pack(side='left', padx=4)
    tk.Button(f, text="Cerrar", bg='#2a2d3a', fg='#8a8d9a',
              font=('Segoe UI', 9), bd=0, padx=12, pady=5, cursor='hand2',
              relief='flat', command=lambda: os._exit(0)).pack(side='left', padx=4)

    root.protocol("WM_DELETE_WINDOW", lambda: os._exit(0))
    root.mainloop()


if __name__ == '__main__':
    start()
