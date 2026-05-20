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
    try:
        send_to_printer(cfg['ip'], cfg['port'], raw)
        return jsonify({'ok': True})
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
        send_to_printer(cfg['ip'], cfg['port'], '~JA')
        return jsonify({'ok': True})
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
        r = ml_get('/orders/search', token, params={
            'seller':       user_id,
            'order.status': 'ready_to_ship',
            'sort':         'date_desc',
            'limit':        50,
        })
        data   = r.json()
        orders = data.get('results', [])
        total  = data.get('paging', {}).get('total', 0)
        return jsonify({'ok': True, 'orders': orders, 'total': total})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/ml/print/<int:shipment_id>', methods=['POST'])
def ml_print(shipment_id):
    token = get_valid_token()
    if not token:
        return jsonify({'ok': False, 'need_login': True}), 401

    cfg = load_config()
    try:
        r = http.get(
            f'{ML_API}/shipment_labels',
            params={'shipment_ids': shipment_id, 'response_type': 'zpl2'},
            headers={'Authorization': f'Bearer {token}'},
            timeout=20,
        )
        if r.status_code != 200:
            return jsonify({'ok': False, 'error': f'ML devolvió {r.status_code}: {r.text[:200]}'}), 502

        send_to_printer(cfg['ip'], cfg['port'], r.content)
        return jsonify({'ok': True})
    except socket.timeout:
        return jsonify({'ok': False, 'error': f"Timeout de impresora: {cfg['ip']}:{cfg['port']}"}), 500
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/ml/print-all', methods=['POST'])
def ml_print_all():
    """Imprime etiquetas de todos los envíos enviados en el body."""
    token = get_valid_token()
    if not token:
        return jsonify({'ok': False, 'need_login': True}), 401

    cfg = load_config()
    shipment_ids = request.get_json().get('shipment_ids', [])
    if not shipment_ids:
        return jsonify({'ok': False, 'error': 'Sin envíos'}), 400

    # ML permite hasta 50 IDs por request
    ids_str = ','.join(str(i) for i in shipment_ids[:50])
    try:
        r = http.get(
            f'{ML_API}/shipment_labels',
            params={'shipment_ids': ids_str, 'response_type': 'zpl2'},
            headers={'Authorization': f'Bearer {token}'},
            timeout=30,
        )
        if r.status_code != 200:
            return jsonify({'ok': False, 'error': f'ML devolvió {r.status_code}'}), 502

        send_to_printer(cfg['ip'], cfg['port'], r.content)
        return jsonify({'ok': True, 'printed': len(shipment_ids)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── Startup ────────────────────────────────────────────────────────────────────

def run_flask():
    import logging
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    app.run(host='127.0.0.1', port=5050, debug=False, use_reloader=False)


def start():
    threading.Thread(target=run_flask, daemon=True).start()
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
