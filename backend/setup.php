<?php
// EnvioBot — Setup inicial. BORRAR este archivo después de ejecutarlo.
require 'config.php';

$db = new mysqli(DB_HOST, DB_USER, DB_PASS, DB_NAME);
if ($db->connect_error) die('Error DB: ' . $db->connect_error);
$db->set_charset('utf8mb4');

$sql = "CREATE TABLE IF NOT EXISTS licenses (
    id                INT AUTO_INCREMENT PRIMARY KEY,
    license_key       CHAR(19)     NOT NULL UNIQUE,
    type              ENUM('OWNER','DEMO','PAID') NOT NULL DEFAULT 'PAID',
    status            ENUM('active','revoked')    NOT NULL DEFAULT 'active',
    fingerprint       VARCHAR(64)  NULL,
    customer_name     VARCHAR(200) NULL,
    customer_email    VARCHAR(200) NULL,
    notes             TEXT         NULL,
    expires_at        DATE         NULL,
    activated_at      DATETIME     NULL,
    created_at        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_validated_at DATETIME     NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4";

if ($db->query($sql)) {
    echo '<p style="font-family:monospace;color:green">✓ Tabla <strong>licenses</strong> creada correctamente en <strong>' . DB_NAME . '</strong>.</p>';
    echo '<p style="font-family:monospace;color:red;margin-top:10px">⚠ IMPORTANTE: borrá este archivo (setup.php) del servidor ahora.</p>';
} else {
    echo '<p style="color:red">Error: ' . $db->error . '</p>';
}
