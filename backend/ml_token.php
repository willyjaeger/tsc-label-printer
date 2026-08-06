<?php
// EnvioBot — Proxy de token OAuth de MercadoLibre para la "app compartida".
// El cliente nunca ve ni necesita client_id/client_secret propios: entra acá
// con su licencia ya activada, y este endpoint hace el intercambio real con
// ML usando las credenciales de la app de EnvioBot (ML_SHARED_CLIENT_ID/
// SECRET en config.php, nunca en el .exe).
//
// Recibe: { secret, key, fingerprint, grant_type: 'authorization_code'|'refresh_token',
//           code?, code_verifier?, refresh_token? }
// Responde:
//   - Si falla el gate de licencia: { ok:false, reason:'...' } (forma DISTINTA
//     de un error de ML, para que app.py no confunda "se venció la
//     suscripción" con "hay que reconectar la cuenta de ML").
//   - Si pasa el gate: la respuesta de ML tal cual (pass-through), para que
//     el parseo que ya existe en app.py (data['access_token'], data.get('error'))
//     funcione igual que en modo 'own_app'.

require 'config.php';
header('Content-Type: application/json');

// Mismo redirect_uri que ya usa app.py — no lo manda el cliente, para no
// dejarlo como superficie de ataque (alguien podría intentar redirigir el
// intercambio a otro lado).
const ML_REDIRECT_URI = 'https://willyjaeger.github.io/tsc-label-printer/callback.html';
const ML_TOKEN_URL     = 'https://api.mercadolibre.com/oauth/token';

$data = json_decode(file_get_contents('php://input'), true);

if (!$data || ($data['secret'] ?? '') !== API_SECRET) {
    http_response_code(403);
    echo json_encode(['ok' => false, 'reason' => 'forbidden']);
    exit;
}

$key   = strtoupper(trim($data['key'] ?? ''));
$fp    = trim($data['fingerprint'] ?? '');
$grant = $data['grant_type'] ?? '';

if (!$key || !$fp) {
    echo json_encode(['ok' => false, 'reason' => 'missing_params']);
    exit;
}
if (!in_array($grant, ['authorization_code', 'refresh_token'], true)) {
    echo json_encode(['ok' => false, 'reason' => 'invalid_grant_type']);
    exit;
}

// ── Gate: mismo criterio que validate.php (licencia ya activada, no
// activate.php — acá no se hace el primer vínculo de fingerprint) ──────────
$db   = db_connect();
$stmt = $db->prepare("SELECT id, type, status, fingerprint, expires_at FROM licenses WHERE license_key = ?");
$stmt->bind_param('s', $key);
$stmt->execute();
$row = $stmt->get_result()->fetch_assoc();
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
if ($row['type'] !== 'OWNER' && (!$row['fingerprint'] || $row['fingerprint'] !== $fp)) {
    echo json_encode(['ok' => false, 'reason' => 'wrong_machine']);
    exit;
}

// ── Gate OK: armar el intercambio real con ML ───────────────────────────────
$post = [
    'grant_type'    => $grant,
    'client_id'     => ML_SHARED_CLIENT_ID,
    'client_secret' => ML_SHARED_CLIENT_SECRET,
    'redirect_uri'  => ML_REDIRECT_URI,
];
if ($grant === 'authorization_code') {
    $post['code']          = $data['code'] ?? '';
    $post['code_verifier'] = $data['code_verifier'] ?? '';
} else {
    $post['refresh_token'] = $data['refresh_token'] ?? '';
}

$ch = curl_init(ML_TOKEN_URL);
curl_setopt_array($ch, [
    CURLOPT_POST           => true,
    CURLOPT_POSTFIELDS     => http_build_query($post),
    CURLOPT_HTTPHEADER     => ['Accept: application/json'],
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_TIMEOUT        => 15,
]);
$ml_response = curl_exec($ch);
$ml_status   = curl_getinfo($ch, CURLINFO_HTTP_CODE);
$curl_err    = curl_error($ch);
curl_close($ch);

if ($ml_response === false) {
    http_response_code(502);
    echo json_encode(['ok' => false, 'reason' => 'ml_unreachable', 'detail' => $curl_err]);
    exit;
}

$stmt2 = $db->prepare("UPDATE licenses SET last_validated_at = NOW() WHERE id = ?");
$stmt2->bind_param('i', $row['id']);
$stmt2->execute();

// Pass-through: mismo status y body que devolvió ML, sin envolver ni
// reinterpretar — app.py ya sabe leer access_token/refresh_token/error de acá.
http_response_code($ml_status ?: 200);
echo $ml_response;
