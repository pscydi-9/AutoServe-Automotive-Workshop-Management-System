-- =====================================================================
-- Migration v1 -> v2
--   * Users: username + bcrypt password_hash + updated_at
--     (hashes for pre-existing accounts are filled in by AuthModel.ensure_admin)
--   * Service_Catalog table + Service_Jobs.service_id foreign key
--   * Phone numbers normalised to the strict 10-digit national format
-- Executed inside a single transaction by DatabaseManager.initialize_schema.
-- =====================================================================
ALTER TABLE Users ADD COLUMN username VARCHAR(40) COLLATE NOCASE;
ALTER TABLE Users ADD COLUMN password_hash VARCHAR(60);
ALTER TABLE Users ADD COLUMN updated_at TEXT;

CREATE TABLE IF NOT EXISTS Service_Catalog (
    service_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    service_name    VARCHAR(120)  NOT NULL UNIQUE COLLATE NOCASE,
    category        VARCHAR(40)   NOT NULL,
    base_labor_cost DECIMAL(10,2) NOT NULL CHECK (base_labor_cost >= 0),
    estimated_hours DECIMAL(4,1)  NOT NULL CHECK (estimated_hours > 0),
    is_active       INTEGER       NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
);

ALTER TABLE Service_Jobs ADD COLUMN service_id INTEGER
    REFERENCES Service_Catalog (service_id) ON DELETE SET NULL;

UPDATE Users    SET phone = substr(phone, -10)
 WHERE phone IS NOT NULL AND length(phone) > 10;
UPDATE Vehicles SET owner_phone = substr(owner_phone, -10)
 WHERE length(owner_phone) > 10;

DROP INDEX IF EXISTS idx_jobs_mechanic;

PRAGMA user_version = 2;
