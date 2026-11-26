-- Reporting views. Looker Studio / Power BI (or the bundled HTML dashboard)
-- read these instead of the raw tables.

DROP VIEW IF EXISTS v_invoice_detail;
CREATE VIEW v_invoice_detail AS
SELECT
    i.invoice_id,
    i.source_file,
    i.status,
    i.extraction_status,
    COALESCE(v.name, i.vendor_name_raw)          AS vendor_name,
    v.status                                      AS vendor_status,
    i.invoice_number,
    i.po_number,
    COALESCE(d.dept_id, 'UNASSIGNED')             AS dept_id,
    COALESCE(d.name, 'Unassigned')                AS department,
    i.invoice_date,
    i.due_date,
    i.received_at,
    i.processed_at,
    i.processing_ms,
    i.total,
    m.exception_codes,
    m.matched,
    m.amount_variance_pct,
    CAST(julianday((SELECT value FROM app_settings WHERE key = 'as_of')) - julianday(i.invoice_date) AS INTEGER)
                                                  AS age_days,
    strftime('%Y', i.invoice_date) || '-Q' ||
        ((CAST(strftime('%m', i.invoice_date) AS INTEGER) - 1) / 3 + 1)  AS fiscal_quarter
FROM invoices i
LEFT JOIN match_results m    ON m.invoice_id = i.invoice_id
LEFT JOIN vendors v          ON v.vendor_id = i.vendor_id
LEFT JOIN purchase_orders p  ON p.po_number = i.po_number
LEFT JOIN departments d      ON d.dept_id = p.dept_id;

-- Unpaid invoices by age bucket (approved-but-not-yet-paid plus open exceptions)
DROP VIEW IF EXISTS v_unpaid_aging;
CREATE VIEW v_unpaid_aging AS
SELECT
    CASE
        WHEN age_days IS NULL THEN 'Unknown date'
        WHEN age_days <= 30 THEN '0-30 days'
        WHEN age_days <= 60 THEN '31-60 days'
        WHEN age_days <= 90 THEN '61-90 days'
        ELSE '90+ days'
    END AS age_bucket,
    status,
    COUNT(*)                 AS invoice_count,
    ROUND(SUM(COALESCE(total, 0)), 2) AS amount
FROM v_invoice_detail
WHERE status IN ('APPROVED', 'EXCEPTION')
GROUP BY 1, 2;

-- Spend = approved or paid invoices
DROP VIEW IF EXISTS v_spend_by_vendor;
CREATE VIEW v_spend_by_vendor AS
SELECT vendor_name, COUNT(*) AS invoice_count, ROUND(SUM(total), 2) AS spend
FROM v_invoice_detail
WHERE status IN ('APPROVED', 'PAID')
GROUP BY vendor_name
ORDER BY spend DESC;

DROP VIEW IF EXISTS v_spend_by_department;
CREATE VIEW v_spend_by_department AS
SELECT dept_id, department, COUNT(*) AS invoice_count, ROUND(SUM(total), 2) AS spend
FROM v_invoice_detail
WHERE status IN ('APPROVED', 'PAID')
GROUP BY dept_id, department
ORDER BY spend DESC;

-- FP&A: budget vs actual by department and quarter.
-- actual    = approved/paid invoices (pre-tax subtotal) dated in the quarter
-- committed = open PO value ordered in the quarter that has not been invoiced yet
DROP VIEW IF EXISTS v_budget_vs_actual;
CREATE VIEW v_budget_vs_actual AS
WITH actual AS (
    SELECT p.dept_id,
           strftime('%Y', i.invoice_date) || '-Q' ||
               ((CAST(strftime('%m', i.invoice_date) AS INTEGER) - 1) / 3 + 1) AS fiscal_quarter,
           SUM(i.subtotal) AS actual
    FROM invoices i
    JOIN purchase_orders p ON p.po_number = i.po_number
    WHERE i.status IN ('APPROVED', 'PAID')
    GROUP BY 1, 2
),
committed AS (
    SELECT p.dept_id,
           strftime('%Y', p.order_date) || '-Q' ||
               ((CAST(strftime('%m', p.order_date) AS INTEGER) - 1) / 3 + 1) AS fiscal_quarter,
           SUM(pl.quantity * pl.unit_price) AS committed
    FROM purchase_orders p
    JOIN po_lines pl ON pl.po_number = p.po_number
    WHERE p.po_number NOT IN (SELECT po_number FROM invoices WHERE po_number IS NOT NULL)
    GROUP BY 1, 2
)
SELECT
    b.dept_id,
    d.name                                   AS department,
    b.fiscal_quarter,
    b.amount                                 AS budget,
    ROUND(COALESCE(a.actual, 0), 2)          AS actual,
    ROUND(COALESCE(c.committed, 0), 2)       AS committed,
    ROUND(b.amount - COALESCE(a.actual, 0), 2) AS remaining,
    ROUND(COALESCE(a.actual, 0) / b.amount, 4) AS pct_used
FROM budgets b
JOIN departments d    ON d.dept_id = b.dept_id
LEFT JOIN actual a    ON a.dept_id = b.dept_id AND a.fiscal_quarter = b.fiscal_quarter
LEFT JOIN committed c ON c.dept_id = b.dept_id AND c.fiscal_quarter = b.fiscal_quarter
ORDER BY b.fiscal_quarter, b.dept_id;

-- Exception rate overall and by code
DROP VIEW IF EXISTS v_exception_summary;
CREATE VIEW v_exception_summary AS
WITH RECURSIVE split(invoice_id, code, rest) AS (
    SELECT invoice_id, '', exception_codes || ',' FROM match_results WHERE exception_codes <> ''
    UNION ALL
    SELECT invoice_id,
           substr(rest, 1, instr(rest, ',') - 1),
           substr(rest, instr(rest, ',') + 1)
    FROM split WHERE rest <> ''
)
SELECT code AS exception_code,
       COUNT(*) AS invoice_count,
       ROUND(1.0 * COUNT(*) / (SELECT COUNT(*) FROM match_results), 4) AS share_of_all_invoices
FROM split
WHERE code <> ''
GROUP BY code
ORDER BY invoice_count DESC;

DROP VIEW IF EXISTS v_kpis;
CREATE VIEW v_kpis AS
SELECT
    (SELECT COUNT(*) FROM match_results)                                   AS invoices_processed,
    (SELECT COUNT(*) FROM match_results WHERE matched = 1)                 AS auto_approved,
    (SELECT COUNT(*) FROM match_results WHERE matched = 0)                 AS exceptions,
    ROUND((SELECT 1.0 * SUM(matched) / COUNT(*) FROM match_results), 4)    AS touchless_rate,
    ROUND((SELECT AVG(processing_ms) FROM invoices), 1)                    AS avg_processing_ms,
    (SELECT COUNT(*) FROM exception_tickets WHERE status <> 'Done')        AS open_tickets,
    (SELECT COUNT(*) FROM exception_tickets
      WHERE status <> 'Done'
        AND julianday((SELECT value FROM app_settings WHERE key = 'as_of')) - julianday(created_at) > 3)
                                                                           AS tickets_past_sla,
    ROUND((SELECT SUM(total) FROM invoices WHERE status IN ('APPROVED','EXCEPTION')), 2) AS unpaid_amount;

-- Processing time: automated extraction+match time, and exception ticket age
DROP VIEW IF EXISTS v_processing_time;
CREATE VIEW v_processing_time AS
SELECT
    CASE WHEN m.matched = 1 THEN 'Auto-approved' ELSE 'Exception' END AS path,
    COUNT(*)                                       AS invoices,
    ROUND(AVG(i.processing_ms), 1)                 AS avg_processing_ms,
    ROUND(MAX(i.processing_ms), 1)                 AS max_processing_ms,
    ROUND(AVG(CASE WHEN m.matched = 0 THEN
        julianday(COALESCE(t.resolved_at, (SELECT value FROM app_settings WHERE key = 'as_of')))
        - julianday(t.created_at) END), 1)         AS avg_open_ticket_age_days
FROM invoices i
JOIN match_results m          ON m.invoice_id = i.invoice_id
LEFT JOIN exception_tickets t ON t.invoice_id = i.invoice_id
GROUP BY 1;
