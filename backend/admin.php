<?php
require 'config.php';
session_start();

// ── Protección contra fuerza bruta en el login ────────────────────────────────
// Sin esto, cualquiera puede probar contraseñas sin límite. Bloquea por IP
// tras varios intentos fallidos seguidos, con un archivo simple (no hace falta
// tabla nueva en la DB).
define('LOGIN_MAX_ATTEMPTS', 5);
define('LOGIN_LOCKOUT_SECONDS', 900); // 15 minutos

function _login_attempts_file() {
    return __DIR__ . '/.login_attempts.json';
}

function _login_attempts_load() {
    $f = _login_attempts_file();
    if (!file_exists($f)) return [];
    $data = json_decode(file_get_contents($f), true);
    return is_array($data) ? $data : [];
}

function _login_attempts_save($data) {
    file_put_contents(_login_attempts_file(), json_encode($data), LOCK_EX);
}

function _login_ip() {
    return $_SERVER['REMOTE_ADDR'] ?? 'unknown';
}

function login_is_locked_out() {
    $data = _login_attempts_load();
    $ip   = _login_ip();
    if (!isset($data[$ip])) return false;
    if ($data[$ip]['count'] < LOGIN_MAX_ATTEMPTS) return false;
    return (time() - $data[$ip]['last']) < LOGIN_LOCKOUT_SECONDS;
}

function login_register_failure() {
    $data = _login_attempts_load();
    $ip   = _login_ip();
    if (!isset($data[$ip]) || (time() - $data[$ip]['last']) > LOGIN_LOCKOUT_SECONDS) {
        $data[$ip] = ['count' => 0, 'last' => time()];
    }
    $data[$ip]['count']++;
    $data[$ip]['last'] = time();
    _login_attempts_save($data);
}

function login_register_success() {
    $data = _login_attempts_load();
    unset($data[_login_ip()]);
    _login_attempts_save($data);
}

// ── Acciones POST ────────────────────────────────────────────────────────────

if ($_SERVER['REQUEST_METHOD'] === 'POST') {

    // Login
    if (isset($_POST['password'])) {
        if (login_is_locked_out()) {
            $login_error = 'Demasiados intentos fallidos. Esperá unos minutos antes de volver a intentar.';
        } elseif (password_verify($_POST['password'], ADMIN_PASS_HASH)) {
            login_register_success();
            $_SESSION['auth'] = true;
        } else {
            login_register_failure();
            $login_error = 'Contraseña incorrecta';
        }
    }

    // Acciones autenticadas
    if ($_SESSION['auth'] ?? false) {
        $db = db_connect();
        $db->set_charset('utf8mb4');

        // Crear licencia
        if (isset($_POST['action']) && $_POST['action'] === 'create') {
            $key    = generate_key($db);
            $type   = in_array($_POST['type'], ['OWNER','DEMO','PAID']) ? $_POST['type'] : 'PAID';
            $name   = trim($_POST['customer_name'] ?? '');
            $email  = trim($_POST['customer_email'] ?? '');
            $notes  = trim($_POST['notes'] ?? '');
            $exp    = trim($_POST['expires_at'] ?? '') ?: null;

            $stmt = $db->prepare("INSERT INTO licenses (license_key, type, customer_name, customer_email, notes, expires_at) VALUES (?,?,?,?,?,?)");
            $stmt->bind_param('ssssss', $key, $type, $name, $email, $notes, $exp);
            $stmt->execute();
            $msg = "Licencia creada: <strong>$key</strong>";
        }

        // Revocar / reactivar
        if (isset($_POST['action']) && $_POST['action'] === 'toggle_status') {
            $id   = (int)$_POST['id'];
            $stmt = $db->prepare("UPDATE licenses SET status = IF(status='active','revoked','active') WHERE id = ?");
            $stmt->bind_param('i', $id);
            $stmt->execute();
        }

        // Resetear fingerprint (permite mover licencia a otra PC)
        if (isset($_POST['action']) && $_POST['action'] === 'reset_fp') {
            $id   = (int)$_POST['id'];
            $stmt = $db->prepare("UPDATE licenses SET fingerprint = NULL, activated_at = NULL WHERE id = ?");
            $stmt->bind_param('i', $id);
            $stmt->execute();
            $msg = 'Huella de máquina reseteada. El cliente puede activar en una nueva PC.';
        }

        // Eliminar
        if (isset($_POST['action']) && $_POST['action'] === 'delete') {
            $id   = (int)$_POST['id'];
            $stmt = $db->prepare("DELETE FROM licenses WHERE id = ?");
            $stmt->bind_param('i', $id);
            $stmt->execute();
        }

        // Extender vencimiento
        if (isset($_POST['action']) && $_POST['action'] === 'extend') {
            $id  = (int)$_POST['id'];
            $exp = trim($_POST['expires_at'] ?? '');
            if ($exp) {
                $stmt = $db->prepare("UPDATE licenses SET expires_at = ?, status = 'active' WHERE id = ?");
                $stmt->bind_param('si', $exp, $id);
                $stmt->execute();
                $msg = 'Vencimiento actualizado.';
            }
        }

        // Logout
        if (isset($_POST['action']) && $_POST['action'] === 'logout') {
            session_destroy();
            header('Location: admin.php');
            exit;
        }
    }
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function generate_key($db) {
    $chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
    do {
        $key = '';
        for ($i = 0; $i < 4; $i++) {
            if ($i) $key .= '-';
            for ($j = 0; $j < 4; $j++) $key .= $chars[random_int(0, strlen($chars)-1)];
        }
        $r = $db->query("SELECT id FROM licenses WHERE license_key = '$key'");
    } while ($r->num_rows > 0);
    return $key;
}

function type_badge($type) {
    $colors = ['OWNER' => '#f59e0b', 'DEMO' => '#06b6d4', 'PAID' => '#22c55e'];
    $c = $colors[$type] ?? '#888';
    return "<span style='background:$c;color:#000;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700'>$type</span>";
}

function status_badge($status) {
    return $status === 'active'
        ? "<span style='color:#22c55e;font-weight:700'>● Activa</span>"
        : "<span style='color:#ef4444;font-weight:700'>● Revocada</span>";
}

// ── Datos para la vista ───────────────────────────────────────────────────────

$licenses = [];
if ($_SESSION['auth'] ?? false) {
    if (!isset($db)) $db = db_connect();
    $res = $db->query("SELECT * FROM licenses ORDER BY created_at DESC");
    while ($row = $res->fetch_assoc()) $licenses[] = $row;
}

$auth = $_SESSION['auth'] ?? false;
?>
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>EnvioBot — Admin</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #111318; color: #e8e9ed; font-family: system-ui, sans-serif; font-size: 14px; }
a { color: #f5a623; text-decoration: none; }

.topbar { background: #1c1e26; border-bottom: 1px solid rgba(255,255,255,0.07);
          padding: 12px 24px; display: flex; align-items: center; gap: 12px; }
.topbar h1 { font-size: 18px; font-weight: 700; color: #f5a623; }
.topbar .sub { color: #888; font-size: 13px; margin-left: auto; }

.container { max-width: 1100px; margin: 0 auto; padding: 24px 16px; }

/* Login */
.login-box { max-width: 340px; margin: 80px auto; background: #1c1e26;
             border: 1px solid rgba(255,255,255,0.07); border-radius: 12px; padding: 32px; }
.login-box h2 { margin-bottom: 20px; color: #f5a623; }
.login-box input { width: 100%; padding: 10px 14px; background: #252830;
                   border: 1px solid rgba(255,255,255,0.1); border-radius: 8px;
                   color: #e8e9ed; margin-bottom: 12px; font-size: 14px; }
.err { color: #ef4444; font-size: 13px; margin-bottom: 10px; }

/* Botones */
.btn { display: inline-block; padding: 8px 16px; border-radius: 8px; border: none;
       cursor: pointer; font-size: 13px; font-weight: 600; transition: opacity .15s; }
.btn:hover { opacity: .85; }
.btn-primary { background: #f5a623; color: #000; }
.btn-danger  { background: #ef4444; color: #fff; }
.btn-ghost   { background: #252830; color: #e8e9ed; border: 1px solid rgba(255,255,255,0.1); }
.btn-sm { padding: 4px 10px; font-size: 12px; }

/* Mensaje flash */
.flash { background: #1a2e1a; border: 1px solid #22c55e; border-radius: 8px;
         padding: 10px 16px; margin-bottom: 16px; color: #86efac; }

/* Tabla */
.table-wrap { overflow-x: auto; border-radius: 10px; border: 1px solid rgba(255,255,255,0.07); }
table { width: 100%; border-collapse: collapse; }
th { background: #1c1e26; padding: 10px 12px; text-align: left; color: #888;
     font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: .5px; }
td { padding: 10px 12px; border-top: 1px solid rgba(255,255,255,0.05); vertical-align: middle; }
tr:hover td { background: rgba(255,255,255,0.02); }
.key-cell { font-family: monospace; font-size: 13px; letter-spacing: 1px; color: #f5a623; }
.fp-cell  { font-family: monospace; font-size: 11px; color: #555; }

/* Panel crear */
.create-panel { background: #1c1e26; border: 1px solid rgba(255,255,255,0.07);
                border-radius: 10px; padding: 20px; margin-bottom: 20px; }
.create-panel h3 { margin-bottom: 14px; font-size: 15px; }
.form-row { display: flex; gap: 10px; flex-wrap: wrap; align-items: flex-end; }
.form-row label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: #888; }
.form-row input, .form-row select {
    padding: 8px 12px; background: #252830; border: 1px solid rgba(255,255,255,0.1);
    border-radius: 8px; color: #e8e9ed; font-size: 13px; min-width: 140px; }
.actions-cell { display: flex; gap: 6px; flex-wrap: wrap; }

/* Modal extender */
.modal-bg { display:none; position:fixed; inset:0; background:rgba(0,0,0,.7); z-index:100;
            align-items:center; justify-content:center; }
.modal-bg.open { display:flex; }
.modal { background:#1c1e26; border:1px solid rgba(255,255,255,0.1); border-radius:12px;
         padding:24px; min-width:280px; }
.modal h3 { margin-bottom:14px; }
.modal input { width:100%; padding:8px 12px; background:#252830;
               border:1px solid rgba(255,255,255,0.1); border-radius:8px;
               color:#e8e9ed; font-size:13px; margin-bottom:12px; }
</style>
</head>
<body>

<div class="topbar">
  <h1>EnvioBot</h1>
  <span style="color:#555;font-size:18px">|</span>
  <span style="color:#888">Panel de licencias</span>
  <?php if ($auth): ?>
  <span class="sub">
    <?= count($licenses) ?> licencias
    &nbsp;·&nbsp;
    <form method="post" style="display:inline">
      <input type="hidden" name="action" value="logout">
      <button class="btn btn-ghost btn-sm" type="submit">Cerrar sesión</button>
    </form>
  </span>
  <?php endif; ?>
</div>

<div class="container">

<?php if (!$auth): ?>
<!-- LOGIN -->
<div class="login-box">
  <h2>Acceso admin</h2>
  <?php if (!empty($login_error)): ?>
    <div class="err"><?= htmlspecialchars($login_error) ?></div>
  <?php endif; ?>
  <form method="post">
    <input type="password" name="password" placeholder="Contraseña" autofocus required>
    <button class="btn btn-primary" style="width:100%" type="submit">Entrar</button>
  </form>
</div>

<?php else: ?>
<!-- PANEL PRINCIPAL -->

<?php if (!empty($msg)): ?>
  <div class="flash"><?= $msg ?></div>
<?php endif; ?>

<!-- Crear licencia -->
<div class="create-panel">
  <h3>Nueva licencia</h3>
  <form method="post">
    <input type="hidden" name="action" value="create">
    <div class="form-row">
      <label>Tipo
        <select name="type">
          <option value="PAID">PAID — Pago mensual</option>
          <option value="DEMO">DEMO — Prueba/regalo</option>
          <option value="OWNER">OWNER — Tuya (cualquier PC)</option>
        </select>
      </label>
      <label>Cliente
        <input type="text" name="customer_name" placeholder="Nombre del cliente">
      </label>
      <label>Email
        <input type="email" name="customer_email" placeholder="email@ejemplo.com">
      </label>
      <label>Vence (vacío = nunca)
        <input type="date" name="expires_at">
      </label>
      <label>Notas
        <input type="text" name="notes" placeholder="Opcional">
      </label>
      <button class="btn btn-primary" type="submit" style="height:36px">Generar key</button>
    </div>
  </form>
</div>

<!-- Tabla de licencias -->
<?php if ($licenses): ?>
<div class="table-wrap">
<table>
<thead>
  <tr>
    <th>Key</th><th>Tipo</th><th>Estado</th><th>Cliente</th>
    <th>Vence</th><th>Última validación</th><th>Máquina (huella)</th><th>Acciones</th>
  </tr>
</thead>
<tbody>
<?php foreach ($licenses as $lic): ?>
<tr>
  <td class="key-cell"><?= htmlspecialchars($lic['license_key']) ?></td>
  <td><?= type_badge($lic['type']) ?></td>
  <td><?= status_badge($lic['status']) ?></td>
  <td>
    <?= htmlspecialchars($lic['customer_name'] ?: '—') ?>
    <?php if ($lic['customer_email']): ?>
      <br><small style="color:#888"><?= htmlspecialchars($lic['customer_email']) ?></small>
    <?php endif; ?>
  </td>
  <td><?= $lic['expires_at'] ? date('d/m/Y', strtotime($lic['expires_at'])) : '<span style="color:#555">Nunca</span>' ?></td>
  <td><?= $lic['last_validated_at'] ? date('d/m/Y H:i', strtotime($lic['last_validated_at'])) : '<span style="color:#555">—</span>' ?></td>
  <td class="fp-cell">
    <?php if ($lic['fingerprint']): ?>
      <?= substr($lic['fingerprint'], 0, 8) ?>…
      <br><small style="color:#444"><?= $lic['activated_at'] ? 'Activada '.date('d/m/Y', strtotime($lic['activated_at'])) : '' ?></small>
    <?php else: ?>
      <span style="color:#555">Sin activar</span>
    <?php endif; ?>
  </td>
  <td>
    <div class="actions-cell">
      <!-- Revocar / Activar -->
      <form method="post">
        <input type="hidden" name="action" value="toggle_status">
        <input type="hidden" name="id" value="<?= $lic['id'] ?>">
        <button class="btn btn-sm <?= $lic['status']==='active' ? 'btn-danger' : 'btn-ghost' ?>" type="submit">
          <?= $lic['status']==='active' ? 'Revocar' : 'Reactivar' ?>
        </button>
      </form>
      <!-- Extender vencimiento -->
      <button class="btn btn-sm btn-ghost" onclick="openExtend(<?= $lic['id'] ?>)">Extender</button>
      <!-- Reset máquina -->
      <?php if ($lic['fingerprint'] && $lic['type'] !== 'OWNER'): ?>
      <form method="post" onsubmit="return confirm('¿Resetear huella de máquina? El cliente podrá activar en una PC diferente.')">
        <input type="hidden" name="action" value="reset_fp">
        <input type="hidden" name="id" value="<?= $lic['id'] ?>">
        <button class="btn btn-sm btn-ghost" type="submit">Reset PC</button>
      </form>
      <?php endif; ?>
      <!-- Eliminar -->
      <form method="post" onsubmit="return confirm('¿Eliminar esta licencia?')">
        <input type="hidden" name="action" value="delete">
        <input type="hidden" name="id" value="<?= $lic['id'] ?>">
        <button class="btn btn-sm btn-danger" type="submit">Eliminar</button>
      </form>
    </div>
  </td>
</tr>
<?php endforeach; ?>
</tbody>
</table>
</div>

<?php else: ?>
<p style="color:#555;margin-top:20px">No hay licencias aún. Creá la primera arriba.</p>
<?php endif; ?>

<!-- Modal extender vencimiento -->
<div class="modal-bg" id="extendModal">
  <div class="modal">
    <h3>Extender vencimiento</h3>
    <form method="post">
      <input type="hidden" name="action" value="extend">
      <input type="hidden" name="id" id="extendId">
      <input type="date" name="expires_at" id="extendDate" required>
      <div style="display:flex;gap:8px">
        <button class="btn btn-primary" type="submit">Guardar</button>
        <button class="btn btn-ghost" type="button" onclick="closeExtend()">Cancelar</button>
      </div>
    </form>
  </div>
</div>
<script>
function openExtend(id) {
  document.getElementById('extendId').value = id;
  document.getElementById('extendModal').classList.add('open');
}
function closeExtend() {
  document.getElementById('extendModal').classList.remove('open');
}
</script>

<?php endif; ?>
</div>
</body>
</html>
