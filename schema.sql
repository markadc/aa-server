-- pcc 服务端数据库结构（PostgreSQL）
-- 执行：psql -U user1 -d pcc -f schema.sql

-- 兑换码
CREATE TABLE IF NOT EXISTS license_keys (
    id          BIGSERIAL PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    days        INT  NOT NULL CHECK (days > 0),
    note        TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    used_at     TIMESTAMPTZ,
    used_by     TEXT,
    revoked     BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS idx_license_keys_created ON license_keys (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_license_keys_used    ON license_keys (used_at);

-- 设备（install_id 为 App 首次启动生成的 UUID）
CREATE TABLE IF NOT EXISTS devices (
    install_id          TEXT PRIMARY KEY,
    android_id          TEXT NOT NULL DEFAULT '',
    model               TEXT NOT NULL DEFAULT '',
    os_version          TEXT NOT NULL DEFAULT '',
    app_version         TEXT NOT NULL DEFAULT '',
    first_seen          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen           TIMESTAMPTZ NOT NULL DEFAULT now(),
    license_expires_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_devices_last_seen ON devices (last_seen DESC);

-- 激活记录（每核销一次一条，便于追溯）
CREATE TABLE IF NOT EXISTS activations (
    id            BIGSERIAL PRIMARY KEY,
    install_id    TEXT NOT NULL,
    key_id        BIGINT NOT NULL REFERENCES license_keys (id),
    code          TEXT NOT NULL,
    activated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activations_install ON activations (install_id, activated_at DESC);
