<?php
// EnvioBot — Setup de la tabla de anuncios (versión nueva / mensajes).
// Subir, abrir una vez en el navegador, y BORRAR este archivo del servidor.
require 'config.php';

$db = new mysqli(DB_HOST, DB_USER, DB_PASS, DB_NAME);
if ($db->connect_error) die('Error DB: ' . $db->connect_error);
$db->set_charset('utf8mb4');

$sql = "CREATE TABLE IF NOT EXISTS app_announcement (
    id             INT          PRIMARY KEY DEFAULT 1,
    latest_version VARCHAR(20)  NOT NULL DEFAULT '1.0.0',
    message        TEXT         NULL,
    download_url   VARCHAR(500) NULL,
    active         TINYINT(1)   NOT NULL DEFAULT 0,
    updated_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4";

if (!$db->query($sql)) {
    die('<p style="color:red">Error creando tabla: ' . $db->error . '</p>');
}

// Fila única (id=1) — arranca con active=0 para no avisar nada hasta que
// se cargue un mensaje de verdad desde admin.php.
$db->query("INSERT INTO app_announcement (id, latest_version, active)
            VALUES (1, '1.0.0', 0)
            ON DUPLICATE KEY UPDATE id = id");

echo '<p style="font-family:monospace;color:green">✓ Tabla <strong>app_announcement</strong> lista en <strong>' . DB_NAME . '</strong>.</p>';
echo '<p style="font-family:monospace;color:red;margin-top:10px">⚠ IMPORTANTE: borrá este archivo (setup_announcement.php) del servidor ahora.</p>';
