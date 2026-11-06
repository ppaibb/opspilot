-- 2026-09-22 生产基线到当前 head 之间的已知差异。
-- 验证驱动先迁移到当前 head，再应用本文件还原历史形态并退回版本号，随后升级。
DROP TABLE IF EXISTS login_attempts CASCADE;
DROP TABLE IF EXISTS refresh_tokens CASCADE;

ALTER TABLE IF EXISTS risk_ack_tickets
    DROP COLUMN IF EXISTS request_digest,
    DROP COLUMN IF EXISTS route_template,
    DROP COLUMN IF EXISTS http_method;
ALTER TABLE IF EXISTS domain_records
    DROP COLUMN IF EXISTS snapshot_credential_id,
    DROP COLUMN IF EXISTS provider_zone_id;
ALTER TABLE IF EXISTS domain_dns_records
    DROP COLUMN IF EXISTS read_only,
    DROP COLUMN IF EXISTS priority,
    DROP COLUMN IF EXISTS proxied,
    DROP COLUMN IF EXISTS record_values_json,
    DROP COLUMN IF EXISTS provider_record_id;
ALTER TABLE IF EXISTS assets
    DROP COLUMN IF EXISTS cloud_project_id,
    DROP COLUMN IF EXISTS cloud_account_id;

UPDATE alembic_version
SET version_num = '20260922_security_hardening_merge';
