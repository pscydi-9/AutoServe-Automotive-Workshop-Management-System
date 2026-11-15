-- =====================================================================
-- AutoServe Pro — Runtime schema (SQLite 3.24+)
-- Third Normal Form. Idempotent: safe to execute on every start-up.
-- SERIAL columns from the PostgreSQL reference DDL map to
-- INTEGER PRIMARY KEY AUTOINCREMENT here.
-- =====================================================================
PRAGMA foreign_keys = ON;

-- Staff accounts permitted to authenticate via OTP.
CREATE TABLE IF NOT EXISTS Users (
    user_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name    VARCHAR(100) NOT NULL,
    email        VARCHAR(254) NOT NULL UNIQUE COLLATE NOCASE,
    phone        VARCHAR(15)  UNIQUE,
    role         VARCHAR(20)  NOT NULL DEFAULT 'Advisor'
                 CHECK (role IN ('Admin', 'Advisor', 'Mechanic')),
    is_active    INTEGER      NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at   TEXT         NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS Vehicles (
    license_plate VARCHAR(15)  PRIMARY KEY
                  CHECK (length(license_plate) BETWEEN 4 AND 15),
    model         VARCHAR(80)  NOT NULL,
    owner_name    VARCHAR(100) NOT NULL,
    owner_phone   VARCHAR(15)  NOT NULL CHECK (length(owner_phone) BETWEEN 7 AND 15),
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
    created_at    TEXT          NOT NULL DEFAULT (datetime('now'))
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
CREATE INDEX IF NOT EXISTS idx_vehicles_owner_phone ON Vehicles (owner_phone);
CREATE INDEX IF NOT EXISTS idx_mechanics_status     ON Mechanics (status);
CREATE INDEX IF NOT EXISTS idx_parts_stock          ON Spare_Parts (stock_quantity);
CREATE INDEX IF NOT EXISTS idx_jobs_plate           ON Service_Jobs (license_plate);
CREATE INDEX IF NOT EXISTS idx_jobs_mechanic        ON Service_Jobs (mechanic_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status_date     ON Service_Jobs (status, job_date);
CREATE INDEX IF NOT EXISTS idx_parts_used_part      ON Parts_Used (part_id);
-- (job_id, part_id) UNIQUE constraint already provides the job_id lookup index.

PRAGMA user_version = 1;
