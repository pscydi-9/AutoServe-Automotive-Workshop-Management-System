-- =====================================================================
-- AutoServe Pro — Reference DDL v2 (PostgreSQL 13+)
-- Third Normal Form relational schema with explicit constraints and an
-- index optimisation profile. Equivalent to database/schema_sqlite.sql.
-- =====================================================================
BEGIN;

CREATE TABLE IF NOT EXISTS users (
    user_id       SERIAL       PRIMARY KEY,
    username      VARCHAR(40)  NOT NULL
                  CONSTRAINT chk_users_username CHECK (username ~ '^[A-Za-z0-9_.]{3,40}$'),
    password_hash CHAR(60)     NOT NULL
                  CONSTRAINT chk_users_bcrypt CHECK (password_hash ~ '^\$2[aby]\$'),
    full_name     VARCHAR(100) NOT NULL,
    email         VARCHAR(254) NOT NULL,
    phone         CHAR(10)     UNIQUE
                  CONSTRAINT chk_users_phone CHECK (phone IS NULL OR phone ~ '^[0-9]{10}$'),
    role        VARCHAR(20)  NOT NULL DEFAULT 'Advisor'
                CONSTRAINT chk_users_role CHECK (role IN ('Admin', 'Advisor', 'Mechanic')),
    is_active   BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email_ci    ON users (LOWER(email));
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_username_ci ON users (LOWER(username));

CREATE TABLE IF NOT EXISTS vehicles (
    license_plate VARCHAR(15)  PRIMARY KEY
                  CONSTRAINT chk_vehicles_plate_len CHECK (char_length(license_plate) BETWEEN 4 AND 15),
    model         VARCHAR(80)  NOT NULL,
    owner_name    VARCHAR(100) NOT NULL,
    owner_phone   CHAR(10)     NOT NULL
                  CONSTRAINT chk_vehicles_phone CHECK (owner_phone ~ '^[0-9]{10}$'),
    owner_email   VARCHAR(254)
                  CONSTRAINT chk_vehicles_email CHECK (owner_email IS NULL OR owner_email ~* '^[^@\s]+@[^@\s]+\.[^@\s]+$'),
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS mechanics (
    mechanic_id    SERIAL       PRIMARY KEY,
    name           VARCHAR(100) NOT NULL,
    specialization VARCHAR(80)  NOT NULL,
    status         VARCHAR(20)  NOT NULL DEFAULT 'Available'
                   CONSTRAINT chk_mechanics_status CHECK (status IN ('Available', 'Busy', 'Off-Duty'))
);

CREATE TABLE IF NOT EXISTS spare_parts (
    part_id        SERIAL        PRIMARY KEY,
    part_name      VARCHAR(120)  NOT NULL UNIQUE,
    stock_quantity INT           NOT NULL DEFAULT 0
                   CONSTRAINT chk_parts_stock CHECK (stock_quantity >= 0),
    unit_price     DECIMAL(10,2) NOT NULL
                   CONSTRAINT chk_parts_price CHECK (unit_price >= 0)
);

CREATE TABLE IF NOT EXISTS service_catalog (
    service_id      SERIAL        PRIMARY KEY,
    service_name    VARCHAR(120)  NOT NULL,
    category        VARCHAR(40)   NOT NULL,
    base_labor_cost DECIMAL(10,2) NOT NULL CONSTRAINT chk_catalog_cost CHECK (base_labor_cost >= 0),
    estimated_hours DECIMAL(4,1)  NOT NULL CONSTRAINT chk_catalog_hours CHECK (estimated_hours > 0),
    is_active       BOOLEAN       NOT NULL DEFAULT TRUE
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_catalog_name_ci ON service_catalog (LOWER(service_name));

CREATE TABLE IF NOT EXISTS service_jobs (
    job_id        SERIAL        PRIMARY KEY,
    license_plate VARCHAR(15)   NOT NULL
                  CONSTRAINT fk_jobs_vehicle REFERENCES vehicles (license_plate)
                  ON UPDATE CASCADE ON DELETE RESTRICT,
    mechanic_id   INT
                  CONSTRAINT fk_jobs_mechanic REFERENCES mechanics (mechanic_id)
                  ON DELETE SET NULL,
    description   TEXT          NOT NULL DEFAULT '',
    status        VARCHAR(20)   NOT NULL DEFAULT 'Pending'
                  CONSTRAINT chk_jobs_status CHECK (status IN ('Pending', 'In Progress', 'Completed', 'Cancelled')),
    labor_cost    DECIMAL(10,2) NOT NULL DEFAULT 0 CONSTRAINT chk_jobs_labor CHECK (labor_cost >= 0),
    total_bill    DECIMAL(10,2) NOT NULL DEFAULT 0 CONSTRAINT chk_jobs_total CHECK (total_bill >= 0),
    job_date      DATE          NOT NULL DEFAULT CURRENT_DATE,
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    service_id    INT
                  CONSTRAINT fk_jobs_service REFERENCES service_catalog (service_id)
                  ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS parts_used (
    usage_id          SERIAL        PRIMARY KEY,
    job_id            INT           NOT NULL
                      CONSTRAINT fk_usage_job REFERENCES service_jobs (job_id) ON DELETE CASCADE,
    part_id           INT           NOT NULL
                      CONSTRAINT fk_usage_part REFERENCES spare_parts (part_id) ON DELETE RESTRICT,
    quantity_used     INT           NOT NULL CONSTRAINT chk_usage_qty CHECK (quantity_used > 0),
    unit_price_at_use DECIMAL(10,2) NOT NULL CONSTRAINT chk_usage_price CHECK (unit_price_at_use >= 0),
    CONSTRAINT uq_usage_job_part UNIQUE (job_id, part_id)
);

-- Index optimisation profile
CREATE INDEX IF NOT EXISTS idx_vehicles_owner_phone ON vehicles (owner_phone);
CREATE INDEX IF NOT EXISTS idx_mechanics_status     ON mechanics (status);
CREATE INDEX IF NOT EXISTS idx_parts_low_stock      ON spare_parts (stock_quantity) WHERE stock_quantity < 10;
CREATE INDEX IF NOT EXISTS idx_jobs_plate           ON service_jobs (license_plate);
CREATE INDEX IF NOT EXISTS idx_jobs_mechanic_status ON service_jobs (mechanic_id, status);
CREATE INDEX IF NOT EXISTS idx_jobs_service         ON service_jobs (service_id);
CREATE INDEX IF NOT EXISTS idx_catalog_category     ON service_catalog (category);
CREATE INDEX IF NOT EXISTS idx_jobs_status_date     ON service_jobs (status, job_date);
CREATE INDEX IF NOT EXISTS idx_parts_used_part      ON parts_used (part_id);

COMMIT;

-- ---------------------------------------------------------------------
-- Reference ACID allocation (PostgreSQL). The application performs the
-- equivalent sequence inside DatabaseManager.transaction().
-- ---------------------------------------------------------------------
-- BEGIN ISOLATION LEVEL READ COMMITTED;
--   SELECT status, labor_cost FROM service_jobs WHERE job_id = $1 FOR UPDATE;
--   UPDATE spare_parts SET stock_quantity = stock_quantity - $3
--    WHERE part_id = $2 AND stock_quantity >= $3 RETURNING unit_price;   -- 0 rows => ROLLBACK
--   INSERT INTO parts_used (job_id, part_id, quantity_used, unit_price_at_use)
--        VALUES ($1, $2, $3, $4)
--   ON CONFLICT (job_id, part_id) DO UPDATE
--        SET quantity_used = parts_used.quantity_used + EXCLUDED.quantity_used;
--   UPDATE service_jobs j SET total_bill = j.labor_cost + COALESCE(
--          (SELECT SUM(pu.quantity_used * pu.unit_price_at_use)
--             FROM parts_used pu WHERE pu.job_id = j.job_id), 0)
--    WHERE j.job_id = $1;
-- COMMIT;

-- ---------------------------------------------------------------------
-- Reference safe mechanic deletion (PostgreSQL).
-- ---------------------------------------------------------------------
-- BEGIN;
--   SELECT mechanic_id FROM mechanics WHERE mechanic_id = $1 FOR UPDATE;
--   -- If any job is 'In Progress' and no reassignment target is given: ROLLBACK.
--   UPDATE service_jobs SET mechanic_id = $2            -- or NULL for Pending jobs
--    WHERE mechanic_id = $1 AND status IN ('Pending', 'In Progress');
--   UPDATE service_jobs SET mechanic_id = NULL
--    WHERE mechanic_id = $1;                            -- detach history
--   DELETE FROM mechanics WHERE mechanic_id = $1;
-- COMMIT;
