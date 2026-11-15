-- =====================================================================
-- AutoServe Pro — Runtime schema v2 (SQLite 3.24+)
-- Third Normal Form. Idempotent: safe to execute on every start-up.
-- SERIAL columns from the PostgreSQL reference DDL map to
-- INTEGER PRIMARY KEY AUTOINCREMENT here.
-- Existing v1 databases are upgraded first by database/migrations/*.sql.
-- =====================================================================
PRAGMA foreign_keys = ON;

-- Staff accounts. Passwords are stored only as bcrypt hashes ($2b$...).
CREATE TABLE IF NOT EXISTS Users (
    user_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    username      VARCHAR(40)  NOT NULL COLLATE NOCASE
                  CHECK (length(username) BETWEEN 3 AND 40),
    password_hash VARCHAR(60)  NOT NULL CHECK (password_hash LIKE '$2%'),
    full_name     VARCHAR(100) NOT NULL,
    email         VARCHAR(254) NOT NULL UNIQUE COLLATE NOCASE,
    phone         VARCHAR(10)  UNIQUE
                  CHECK (phone IS NULL OR (length(phone) = 10 AND phone NOT GLOB '*[^0-9]*')),
    role          VARCHAR(20)  NOT NULL DEFAULT 'Advisor'
                  CHECK (role IN ('Admin', 'Advisor', 'Mechanic')),
    is_active     INTEGER      NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at    TEXT         NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS Vehicles (
    license_plate VARCHAR(15)  PRIMARY KEY
                  CHECK (length(license_plate) BETWEEN 4 AND 15),
    model         VARCHAR(80)  NOT NULL,
    owner_name    VARCHAR(100) NOT NULL,
    owner_phone   VARCHAR(15)  NOT NULL
                  CHECK (length(owner_phone) = 10 AND owner_phone NOT GLOB '*[^0-9]*'),
    owner_email   VARCHAR(254),
    created_at    TEXT         NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS Mechanics (
    mechanic_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name           VARCHAR(100) NOT NULL,
    specialization VARCHAR(80)  NOT NULL,
    status         VARCHAR(20)  NOT NULL DEFAULT 'Available'
                   CHECK (status IN ('Available', 'Busy', 'Off-Duty'))
);

CREATE TABLE IF NOT EXISTS Spare_Parts (
    part_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    part_name      VARCHAR(120)  NOT NULL UNIQUE COLLATE NOCASE,
    stock_quantity INT           NOT NULL DEFAULT 0 CHECK (stock_quantity >= 0),
    unit_price     DECIMAL(10,2) NOT NULL CHECK (unit_price >= 0)
);

-- Repairs catalogue: standard labour packages a job can be booked against.
CREATE TABLE IF NOT EXISTS Service_Catalog (
    service_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    service_name    VARCHAR(120)  NOT NULL UNIQUE COLLATE NOCASE,
    category        VARCHAR(40)   NOT NULL,
    base_labor_cost DECIMAL(10,2) NOT NULL CHECK (base_labor_cost >= 0),
    estimated_hours DECIMAL(4,1)  NOT NULL CHECK (estimated_hours > 0),
    is_active       INTEGER       NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS Service_Jobs (
    job_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    license_plate VARCHAR(15)   NOT NULL
                  REFERENCES Vehicles (license_plate) ON UPDATE CASCADE ON DELETE RESTRICT,
    mechanic_id   INTEGER
                  REFERENCES Mechanics (mechanic_id) ON DELETE SET NULL,
    description   TEXT          NOT NULL DEFAULT '',
    status        VARCHAR(20)   NOT NULL DEFAULT 'Pending'
                  CHECK (status IN ('Pending', 'In Progress', 'Completed', 'Cancelled')),
    labor_cost    DECIMAL(10,2) NOT NULL DEFAULT 0 CHECK (labor_cost >= 0),
    total_bill    DECIMAL(10,2) NOT NULL DEFAULT 0 CHECK (total_bill >= 0),
    job_date      DATE          NOT NULL DEFAULT (date('now')),
    created_at    TEXT          NOT NULL DEFAULT (datetime('now')),
    service_id    INTEGER
                  REFERENCES Service_Catalog (service_id) ON DELETE SET NULL
);

-- Junction (linkage matrix) between jobs and parts.
-- unit_price_at_use snapshots the price so historical invoices are immutable
-- even if catalogue prices change later.
CREATE TABLE IF NOT EXISTS Parts_Used (
    usage_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id            INTEGER       NOT NULL
                      REFERENCES Service_Jobs (job_id) ON DELETE CASCADE,
    part_id           INTEGER       NOT NULL
                      REFERENCES Spare_Parts (part_id) ON DELETE RESTRICT,
    quantity_used     INT           NOT NULL CHECK (quantity_used > 0),
    unit_price_at_use DECIMAL(10,2) NOT NULL CHECK (unit_price_at_use >= 0),
    UNIQUE (job_id, part_id)
);

-- ---------------------------------------------------------------------
-- Index optimisation profile
-- ---------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_username   ON Users (username COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_vehicles_owner_phone   ON Vehicles (owner_phone);
CREATE INDEX IF NOT EXISTS idx_mechanics_status       ON Mechanics (status);
CREATE INDEX IF NOT EXISTS idx_parts_stock            ON Spare_Parts (stock_quantity);
CREATE INDEX IF NOT EXISTS idx_catalog_category       ON Service_Catalog (category);
CREATE INDEX IF NOT EXISTS idx_jobs_plate             ON Service_Jobs (license_plate);
CREATE INDEX IF NOT EXISTS idx_jobs_mechanic_status   ON Service_Jobs (mechanic_id, status);
CREATE INDEX IF NOT EXISTS idx_jobs_status_date       ON Service_Jobs (status, job_date);
CREATE INDEX IF NOT EXISTS idx_jobs_service           ON Service_Jobs (service_id);
CREATE INDEX IF NOT EXISTS idx_parts_used_part        ON Parts_Used (part_id);
-- (job_id, part_id) UNIQUE constraint already provides the job_id lookup index.

PRAGMA user_version = 2;
