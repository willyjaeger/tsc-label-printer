<?php
// EnvioBot — Anuncios (versión nueva / mensajes a todos los clientes).
// Solo lectura — la carga/edición se hace desde admin.php (save_announcement).
// Recibe: { secret } (por POST, mismo patrón que validate.php/ml_token.php)
// Responde: { ok:true, latest_version, message, download_url, active }

require 'config.php';
header('Content-Type: application/json');

$data = json_decode(file_get_contents('php://input'), true);

if (!$data || ($data['secret'] ?? '') !== API_SECRET) {
    http_response_code(403);
    echo json_encode(['ok' => false, 'reason' => 'forbidden']);
    exit;
}

$db  = db_connect();
$row = $db->query("SELECT latest_version, message, download_url, active FROM app_announcement WHERE id = 1")
          ->fetch_assoc();

if (!$row) {
    echo json_encode(['ok' => true, 'latest_version' => '1.0.0', 'message' => null,
                       'download_url' => null, 'active' => false]);
    exit;
}

echo json_encode([
    'ok'             => true,
    'latest_version' => $row['latest_version'],
    'message'        => $row['message'],
    'download_url'   => $row['download_url'],
    'active'         => (bool)$row['active'],
]);
