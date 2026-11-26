-- Mini-ERP schema (stands in for Oracle ERP) plus the AP automation tables.
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- master data
CREATE TABLE IF NOT EXISTS departments (
    dept_id         TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    cost_center     TEXT NOT NULL,
    jira_component  TEXT NOT NULL,
    ap_owner        TEXT NOT NULL          -- who exception tickets route to
);

CREATE TABLE IF NOT EXISTS budgets (
    dept_id         TEXT NOT NULL REFERENCES departments(dept_id),
    fiscal_quarter  TEXT NOT NULL,         -- e.g. 2026-Q3
    amount          REAL NOT NULL,
    PRIMARY KEY (dept_id, fiscal_quarter)
);

CREATE TABLE IF NOT EXISTS vendors (
    vendor_id           TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('APPROVED','PENDING','BLOCKED')),
    category            TEXT NOT NULL,
    payment_terms_days  INTEGER NOT NULL,
    remit_email         TEXT
);

-- ------------------------------------------------------------- procure side
CREATE TABLE IF NOT EXISTS purchase_orders (
    po_number   TEXT PRIMARY KEY,
    vendor_id   TEXT NOT NULL REFERENCES vendors(vendor_id),
    dept_id     TEXT NOT NULL REFERENCES departments(dept_id),
    order_date  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'OPEN',
    currency    TEXT NOT NULL DEFAULT 'USD'
);

CREATE TABLE IF NOT EXISTS po_lines (
    po_number   TEXT NOT NULL REFERENCES purchase_orders(po_number),
    line_no     INTEGER NOT NULL,
    sku         TEXT NOT NULL,
    description TEXT NOT NULL,
    quantity    REAL NOT NULL,
    unit_price  REAL NOT NULL,
    PRIMARY KEY (po_number, line_no)
);

CREATE TABLE IF NOT EXISTS goods_receipts (
    receipt_id   TEXT PRIMARY KEY,
    po_number    TEXT NOT NULL REFERENCES purchase_orders(po_number),
    receipt_date TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS receipt_lines (
    receipt_id        TEXT NOT NULL REFERENCES goods_receipts(receipt_id),
    po_number         TEXT NOT NULL,
    line_no           INTEGER NOT NULL,
    quantity_received REAL NOT NULL,
    PRIMARY KEY (receipt_id, line_no)
);

-- ------------------------------------------------------------------ AP side
CREATE TABLE IF NOT EXISTS invoices (
    invoice_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file        TEXT NOT NULL,
    file_sha256        TEXT NOT NULL UNIQUE,     -- idempotency: same file never ingested twice
    received_at        TEXT NOT NULL,
    processed_at       TEXT,
    processing_ms      REAL,
    extractor          TEXT,
    extraction_status  TEXT NOT NULL CHECK (extraction_status IN ('OK','FAILED')),
    extraction_error   TEXT,
    vendor_name_raw    TEXT,
    vendor_id          TEXT REFERENCES vendors(vendor_id),
    invoice_number     TEXT,
    po_number          TEXT,
    invoice_date       TEXT,
    due_date           TEXT,
    currency           TEXT,
    subtotal           REAL,
    tax                REAL,
    total              REAL,
    status             TEXT NOT NULL DEFAULT 'RECEIVED'
                       CHECK (status IN ('RECEIVED','APPROVED','EXCEPTION','PAID'))
);

CREATE TABLE IF NOT EXISTS invoice_lines (
    invoice_id   INTEGER NOT NULL REFERENCES invoices(invoice_id),
    line_no      INTEGER NOT NULL,
    sku          TEXT,
    description  TEXT,
    quantity     REAL NOT NULL,
    unit_price   REAL NOT NULL,
    amount       REAL NOT NULL,
    PRIMARY KEY (invoice_id, line_no)
);

CREATE TABLE IF NOT EXISTS match_results (
    invoice_id               INTEGER PRIMARY KEY REFERENCES invoices(invoice_id),
    extraction_failed        INTEGER NOT NULL,
    missing_po               INTEGER NOT NULL,
    po_not_found             INTEGER NOT NULL,
    vendor_not_approved      INTEGER NOT NULL,
    vendor_po_mismatch       INTEGER NOT NULL,
    duplicate_invoice        INTEGER NOT NULL,
    amount_out_of_tolerance  INTEGER NOT NULL,
    quantity_mismatch        INTEGER NOT NULL,
    expected_amount          REAL,
    amount_variance_pct      REAL,
    exception_codes          TEXT NOT NULL,     -- comma separated, '' when clean
    matched                  INTEGER NOT NULL,  -- 1 = clean three-way match
    matched_at               TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exception_tickets (
    ticket_key       TEXT PRIMARY KEY,
    invoice_id       INTEGER NOT NULL REFERENCES invoices(invoice_id),
    summary          TEXT NOT NULL,
    description      TEXT NOT NULL,
    exception_codes  TEXT NOT NULL,
    priority         TEXT NOT NULL,
    component        TEXT NOT NULL,
    assignee         TEXT NOT NULL,
    labels           TEXT NOT NULL,
    amount           REAL,
    status           TEXT NOT NULL DEFAULT 'Open',
    created_at       TEXT NOT NULL,
    last_reminder_at TEXT,
    resolved_at      TEXT
);

CREATE TABLE IF NOT EXISTS approved_for_payment (
    invoice_id      INTEGER PRIMARY KEY REFERENCES invoices(invoice_id),
    vendor_name     TEXT NOT NULL,
    invoice_number  TEXT NOT NULL,
    po_number       TEXT NOT NULL,
    amount          REAL NOT NULL,
    due_date        TEXT,
    approved_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
    invoice_id  INTEGER PRIMARY KEY REFERENCES invoices(invoice_id),
    paid_date   TEXT NOT NULL,
    amount      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    extractor     TEXT,
    files_seen    INTEGER,
    files_new     INTEGER,
    matched       INTEGER,
    exceptions    INTEGER
);

-- One-row-per-key settings so views can use the demo's "as of" date.
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
