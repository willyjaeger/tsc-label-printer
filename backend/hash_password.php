<?php
// EnvioBot — Generador de hash para ADMIN_PASS_HASH.
// Uso: subir por FileZilla junto a los demás archivos de backend/, abrir en
// el navegador, escribir la contraseña elegida, copiar el hash resultante a
// config.php, y BORRAR ESTE ARCHIVO del servidor cuando termines.

$hash = null;
if ($_SERVER['REQUEST_METHOD'] === 'POST' && !empty($_POST['password'])) {
    $hash = password_hash($_POST['password'], PASSWORD_BCRYPT);
}
?>
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<title>EnvioBot — Generar hash</title>
<style>
body { background:#111318; color:#e8e9ed; font-family:system-ui,sans-serif; padding:40px; }
.box { max-width:480px; margin:0 auto; background:#1c1e26; border-radius:12px; padding:28px; }
h2 { color:#f5a623; margin-bottom:16px; }
input { width:100%; padding:10px 14px; background:#252830; border:1px solid rgba(255,255,255,.1);
        border-radius:8px; color:#e8e9ed; margin-bottom:12px; font-size:14px; box-sizing:border-box; }
button { padding:10px 20px; background:#f5a623; color:#000; border:none; border-radius:8px;
         font-weight:700; cursor:pointer; }
.result { margin-top:20px; padding:14px; background:#252830; border-radius:8px;
          word-break:break-all; font-family:monospace; color:#4caf88; }
.warn { color:#ef4444; margin-top:20px; font-size:13px; }
</style>
</head>
<body>
<div class="box">
  <h2>Generar hash de contraseña</h2>
  <form method="post">
    <input type="password" name="password" placeholder="Contraseña elegida" required autofocus>
    <button type="submit">Generar hash</button>
  </form>
  <?php if ($hash): ?>
    <div class="result"><?= htmlspecialchars($hash) ?></div>
    <p style="margin-top:10px;font-size:13px;color:#888">
      Copiá esto completo (empieza con <code>$2y$</code>) en <code>ADMIN_PASS_HASH</code> de config.php.
    </p>
  <?php endif; ?>
  <p class="warn">⚠ Borrá este archivo del servidor por FileZilla apenas termines.</p>
</div>
</body>
</html>
