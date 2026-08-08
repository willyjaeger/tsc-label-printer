<?php
// EnvioBot — Primera activación de una licencia
// Recibe: { secret, key, fingerprint }
// Responde: { ok, type, activated } o { ok:false, reason }

require 'config.php';
header('Content-Type: application/json');

$data = json_decode(file_get_contents('php://input'), true);

if (!$data || ($data['secret'] ?? '') !== API_SECRET) {
    http_response_code(403);
    echo json_encode(['ok' => false, 'error' => 'forbidden']);
    exit;
}

$key = strtoupper(trim($data['key'] ?? ''));
$fp  = trim($data['fingerprint'] ?? '');

if (!$key || !$fp) {
    echo json_encode(['ok' => false, 'reason' => 'missing_params']);
    exit;
}

$db   = db_connect();
$stmt = $db->prepare("SELECT id, type, status, fingerprint, expires_at FROM licenses WHERE license_key = ?");
$stmt->bind_param('s', $key);
$stmt->execute();
$row  = $stmt->get_result()->fetch_assoc();
$stmt->close();

if (!$row) {
    echo json_encode(['ok' => false, 'reason' => 'not_found']);
    exit;
}

if ($row['status'] === 'revoked') {
    echo json_encode(['ok' => false, 'reason' => 'revoked']);
    exit;
}

if ($row['expires_at'] && $row['expires_at'] < date('Y-m-d')) {
    echo json_encode(['ok' => false, 'reason' => 'expired']);
    exit;
}

// OWNER: funciona en cualquier máquina, nunca se bloquea
if ($row['type'] === 'OWNER') {
    $stmt2 = $db->prepare("UPDATE licenses SET last_validated_at = NOW() WHERE id = ?");
    $stmt2->bind_param('i', $row['id']);
    $stmt2->execute();
    echo json_encode(['ok' => true, 'type' => 'OWNER']);
    exit;
}

// DEMO: si esta PC ya activó otra licencia DEMO antes (con otra key, ej. otro
// mail), no dejar activar una nueva — evita que se "renueve" la prueba
// gratis pidiendo claves nuevas. PAID/OWNER en la misma PC no se bloquean
// (una persona real puede tener varias licencias pagas en su PC).
if ($row['type'] === 'DEMO' && !$row['fingerprint']) {
    $stmt3 = $db->prepare("SELECT id FROM licenses WHERE fingerprint = ? AND type = 'DEMO' AND id != ?");
    $stmt3->bind_param('si', $fp, $row['id']);
    $stmt3->execute();
    $dup = $stmt3->get_result()->fetch_assoc();
    $stmt3->close();
    if ($dup) {
        echo json_encode(['ok' => false, 'reason' => 'demo_already_used']);
        exit;
    }
}

// Sin fingerprint aún: primera activación, graba la máquina
if (!$row['fingerprint']) {
    $stmt2 = $db->prepare("UPDATE licenses SET fingerprint = ?, activated_at = NOW(), last_validated_at = NOW() WHERE id = ?");
    $stmt2->bind_param('si', $fp, $row['id']);
    $stmt2->execute();
    echo json_encode(['ok' => true, 'type' => $row['type'], 'activated' => true]);
    exit;
}

// Ya tiene fingerprint: debe coincidir
if ($row['fingerprint'] !== $fp) {
    echo json_encode(['ok' => false, 'reason' => 'wrong_machine']);
    exit;
}

$stmt2 = $db->prepare("UPDATE licenses SET last_validated_at = NOW() WHERE id = ?");
$stmt2->bind_param('i', $row['id']);
$stmt2->execute();
echo json_encode(['ok' => true, 'type' => $row['type']]);
