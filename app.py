from flask import Flask, request, jsonify, send_from_directory
import socket
import json
import os
import sys
import threading
import webbrowser
import time

# ── Path resolution ────────────────────────────────────────────────────────────
# PyInstaller extrae archivos bundleados a sys._MEIPASS (directorio temporal).
# El config.json debe guardarse junto al .exe, no en el temp dir.
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
}


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


# ── Flask app ──────────────────────────────────────────────────────────────────

app = Flask(__name__)


@app.route('/')
def index():
    return send_from_directory(BUNDLE_DIR, 'index.html')


@app.route('/config', methods=['GET'])
def get_config():
    return jsonify(load_config())


@app.route('/config', methods=['POST'])
def post_config():
    try:
        cfg = load_config()
        cfg.update(request.get_json())
        save_config(cfg)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


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
        return jsonify({'ok': False, 'error': f"Timeout: no se pudo conectar a {cfg['ip']}:{cfg['port']}. Verificar IP y red."}), 500
    except ConnectionRefusedError:
        return jsonify({'ok': False, 'error': 'Conexión rechazada: verificar que la impresora esté encendida y en red.'}), 500
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/calibrate', methods=['POST'])
def calibrate():
    cfg = load_config()
    params = request.get_json() or {}
    cfg.update({k: v for k, v in params.items() if v is not None})
    save_config(cfg)

    dpi = int(cfg.get('dpi', 203))
    dots_per_mm = dpi / 25.4
    height_dots = round(float(cfg.get('label_height_mm', 150)) * dots_per_mm)
    backfeed_dots = int(cfg.get('backfeed_dots', 0))

    media_map = {'gap': 'G', 'continuous': 'N', 'mark': 'T'}
    media_char = media_map.get(cfg.get('media_type', 'gap'), 'G')

    zpl = (
        f'^XA\r\n'
        f'^MN{media_char}\r\n'
        f'^LL{height_dots}\r\n'
        f'^LT{backfeed_dots}\r\n'
        f'^XZ\r\n'
    )

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
    dots_per_mm = dpi / 25.4
    height_dots = round(float(cfg.get('label_height_mm', 150)) * dots_per_mm)
    width_dots = round(float(cfg.get('label_width_mm', 100)) * dots_per_mm)
    cx = width_dots // 2

    zpl = (
        f'^XA\r\n'
        f'^PW{width_dots}\r\n'
        f'^LL{height_dots}\r\n'
        f'^FO{cx - 200},{height_dots//2 - 50}^ADN,36,20^FDTEST CALIBRACION^FS\r\n'
        f'^FO{cx - 150},{height_dots//2 + 10}^ADN,20,10^FD{cfg["label_height_mm"]}mm x {cfg["label_width_mm"]}mm  {dpi}dpi^FS\r\n'
        f'^XZ\r\n'
    )
    try:
        send_to_printer(cfg['ip'], cfg['port'], zpl)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── Startup ────────────────────────────────────────────────────────────────────

def run_flask():
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    app.run(host='127.0.0.1', port=5050, debug=False, use_reloader=False)


def start():
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Esperar a que Flask levante antes de abrir el browser
    time.sleep(1.2)
    webbrowser.open('http://localhost:5050')

    if getattr(sys, 'frozen', False):
        # Corriendo como .exe: mostrar ventana de control mínima (sin consola)
        _show_control_window()
    else:
        # Modo desarrollo: usar consola
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

    try:
        root.iconbitmap(default='')
    except Exception:
        pass

    tk.Label(
        root, text="TSC Label Printer está corriendo",
        bg='#1c1e26', fg='#e8e9ed',
        font=('Segoe UI', 10, 'bold')
    ).pack(pady=(18, 4))

    tk.Label(
        root, text="http://localhost:5050",
        bg='#1c1e26', fg='#f5a623',
        font=('Segoe UI', 9)
    ).pack(pady=(0, 10))

    btn_frame = tk.Frame(root, bg='#1c1e26')
    btn_frame.pack()

    tk.Button(
        btn_frame, text="Abrir navegador",
        bg='#f5a623', fg='#111',
        font=('Segoe UI', 9, 'bold'),
        bd=0, padx=12, pady=5, cursor='hand2',
        relief='flat', activebackground='#c97f10',
        command=lambda: webbrowser.open('http://localhost:5050')
    ).pack(side='left', padx=4)

    tk.Button(
        btn_frame, text="Cerrar",
        bg='#2a2d3a', fg='#8a8d9a',
        font=('Segoe UI', 9),
        bd=0, padx=12, pady=5, cursor='hand2',
        relief='flat', activebackground='#353847',
        command=lambda: os._exit(0)
    ).pack(side='left', padx=4)

    root.protocol("WM_DELETE_WINDOW", lambda: os._exit(0))
    root.mainloop()


if __name__ == '__main__':
    start()
