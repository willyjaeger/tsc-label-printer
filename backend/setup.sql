-- EnvioBot — Tabla de licencias
-- Ejecutar una sola vez en po000380_envios

CREATE TABLE IF NOT EXISTS licenses (
    id               INT AUTO_INCREMENT PRIMARY KEY,
    license_key      CHAR(19)     NOT NULL UNIQUE COMMENT 'Formato: XXXX-XXXX-XXXX-XXXX',
    type             ENUM('OWNER','DEMO','PAID') NOT NULL DEFAULT 'PAID',
    status           ENUM('active','revoked')    NOT NULL DEFAULT 'active',
    fingerprint      VARCHAR(64)  NULL  COMMENT 'SHA256 de MAC+disco, se graba en la primera activacion',
    customer_name    VARCHAR(200) NULL,
    customer_email   VARCHAR(200) NULL,
    notes            TEXT         NULL,
    expires_at       DATE         NULL  COMMENT 'NULL = nunca vence',
    activated_at     DATETIME     NULL,
    created_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_validated_at DATETIME    NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
