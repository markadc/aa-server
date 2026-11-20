-- pcc 服务端数据库结构（PostgreSQL）
-- 执行：psql -h 127.0.0.1 -U user1 -d aa_data -f schema.sql
-- 幂等：老库重复执行会自动补齐新增字段/表，并移除旧的 install_id 唯一索引。

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

-- 用户（注册生成：user0000..user9999 共 1 万个，密码按机主要求存明文）
-- 授权绑定在账号维度（license_expires_at）；install_id 仅记录最后登录设备，不设唯一约束
CREATE TABLE IF NOT EXISTS pcc_users (
    id                  BIGSERIAL PRIMARY KEY,
    username            TEXT NOT NULL UNIQUE,
    password            TEXT NOT NULL,
    token               TEXT NOT NULL UNIQUE,
    install_id          TEXT NOT NULL DEFAULT '',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_record_at      TIMESTAMPTZ,
    revoked             BOOLEAN NOT NULL DEFAULT false,
    license_expires_at  TIMESTAMPTZ,
    phone               TEXT NOT NULL DEFAULT ''
);
ALTER TABLE pcc_users ADD COLUMN IF NOT EXISTS license_expires_at TIMESTAMPTZ;
ALTER TABLE pcc_users ADD COLUMN IF NOT EXISTS phone TEXT NOT NULL DEFAULT '';
DROP INDEX IF EXISTS idx_pcc_users_install_uniq;
CREATE INDEX IF NOT EXISTS idx_pcc_users_install ON pcc_users (install_id);
CREATE INDEX IF NOT EXISTS idx_pcc_users_created ON pcc_users (created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pcc_users_phone_uniq
    ON pcc_users (phone) WHERE phone <> '';

-- 激活记录（每核销一次一条，便于追溯）
CREATE TABLE IF NOT EXISTS activations (
    id            BIGSERIAL PRIMARY KEY,
    install_id    TEXT NOT NULL,
    user_id       BIGINT REFERENCES pcc_users (id) ON DELETE SET NULL,
    username      TEXT NOT NULL DEFAULT '',
    key_id        BIGINT NOT NULL REFERENCES license_keys (id),
    code          TEXT NOT NULL,
    activated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL
);
ALTER TABLE activations ADD COLUMN IF NOT EXISTS user_id BIGINT REFERENCES pcc_users (id) ON DELETE SET NULL;
ALTER TABLE activations ADD COLUMN IF NOT EXISTS username TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_activations_install ON activations (install_id, activated_at DESC);
CREATE INDEX IF NOT EXISTS idx_activations_user    ON activations (user_id, activated_at DESC);

-- 运行时配置（管理后台在线修改公告 / 客服 QQ，即时生效；未保存过时回落 config.toml）
CREATE TABLE IF NOT EXISTS app_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 用户反馈（仅已登录用户可提交，自动记录账号、绑定手机号与设备版本信息）
CREATE TABLE IF NOT EXISTS pcc_feedbacks (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT REFERENCES pcc_users (id) ON DELETE SET NULL,
    username    TEXT NOT NULL DEFAULT '',
    phone       TEXT NOT NULL DEFAULT '',
    install_id  TEXT NOT NULL DEFAULT '',
    app_version TEXT NOT NULL DEFAULT '',
    model       TEXT NOT NULL DEFAULT '',
    os_version  TEXT NOT NULL DEFAULT '',
    contact     TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_pcc_feedbacks_created ON pcc_feedbacks (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pcc_feedbacks_status  ON pcc_feedbacks (status, created_at DESC);
