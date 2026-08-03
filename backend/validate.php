<?php
// EnvioBot — Validación por impresión
// Recibe: { secret, key, fingerprint }
// Responde: { valid, type } o { valid:false, reason }

require 'config.php';
header('Content-Type: application/json');

$data = json_decode(file_get_contents('php://input'), true);

if (!$data || ($data['secret'] ?? '') !== API_SECRET) {
    http_response_code(403);
    echo json_encode(['valid' => false, 'reason' => 'forbidden']);
    exit;
}

$key = strtoupper(trim($data['key'] ?? ''));
$fp  = trim($data['fingerprint'] ?? '');

if (!$key || !$fp) {
    echo json_encode(['valid' => false, 'reason' => 'missing_params']);
    exit;
}

$db   = db_connect();
$stmt = $db->prepare("SELECT id, type, status, fingerprint, expires_at FROM licenses WHERE license_key = ?");
$stmt->bind_param('s', $key);
$stmt->execute();
$row  = $stmt->get_result()->fetch_assoc();
$stmt->close();

if (!$row) {
    echo json_encode(['valid' => false, 'reason' => 'not_found']);
    exit;
}

if ($row['status'] === 'revoked') {
    echo json_encode(['valid' => false, 'reason' => 'revoked']);
    exit;
}

if ($row['expires_at'] && $row['expires_at'] < date('Y-m-d')) {
    echo json_encode(['valid' => false, 'reason' => 'expired']);
    exit;
}

// OWNER: siempre válido en cualquier máquina
if ($row['type'] === 'OWNER') {
    $stmt2 = $db->prepare("UPDATE licenses SET last_validated_at = NOW() WHERE id = ?");
    $stmt2->bind_param('i', $row['id']);
    $stmt2->execute();
    echo json_encode(['valid' => true, 'type' => 'OWNER']);
    exit;
}

// Licencia sin activar aún o máquina incorrecta: rechazar
if (!$row['fingerprint'] || $row['fingerprint'] !== $fp) {
    echo json_encode(['valid' => false, 'reason' => 'wrong_machine']);
    exit;
}

$stmt2 = $db->prepare("UPDATE licenses SET last_validated_at = NOW() WHERE id = ?");
$stmt2->bind_param('i', $row['id']);
$stmt2->execute();

echo json_encode(['valid' => true, 'type' => $row['type']]);
