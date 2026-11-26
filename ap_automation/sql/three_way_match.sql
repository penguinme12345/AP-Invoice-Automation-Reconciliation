-- Three-way match: invoice  <->  purchase order  <->  goods receipt.
--
-- Evaluates every invoice that does not yet have a row in match_results and
-- returns one row per invoice with a flag per control:
--
--   EXTRACTION_FAILED        the Pydantic gate rejected the extracted data
--   MISSING_PO               no PO number on the invoice
--   PO_NOT_FOUND             PO number printed but not in the ERP
--   VENDOR_NOT_APPROVED      vendor unknown, pending or blocked in the vendor master
--   VENDOR_PO_MISMATCH       PO belongs to a different vendor
--   DUPLICATE_INVOICE        same vendor + invoice number (or same vendor + date + total) seen before
--   AMOUNT_OUT_OF_TOLERANCE  invoice subtotal vs (invoiced qty x PO unit price) off by more than :tolerance
--   QUANTITY_MISMATCH        invoiced qty exceeds received qty or ordered qty, or line not on the PO
--
-- Parameter: :tolerance  (0.02 = +/-2%)

WITH new_inv AS (
    SELECT i.*
    FROM invoices i
    WHERE i.invoice_id NOT IN (SELECT invoice_id FROM match_results)
),

received AS (                -- cumulative receipts per PO line
    SELECT po_number, line_no, SUM(quantity_received) AS qty_received
    FROM receipt_lines
    GROUP BY po_number, line_no
),

line_check AS (
    SELECT
        il.invoice_id,
        il.line_no,
        il.quantity                       AS qty_invoiced,
        pl.quantity                       AS qty_ordered,
        COALESCE(r.qty_received, 0)       AS qty_received,
        pl.unit_price                     AS po_unit_price,
        il.quantity * pl.unit_price       AS expected_amount
    FROM new_inv n
    JOIN invoice_lines il      ON il.invoice_id = n.invoice_id
    LEFT JOIN po_lines pl      ON pl.po_number = n.po_number
                              AND pl.line_no   = il.line_no
                              AND (il.sku IS NULL OR il.sku = pl.sku)
    LEFT JOIN received r       ON r.po_number = n.po_number
                              AND r.line_no   = il.line_no
),

line_agg AS (
    SELECT
        invoice_id,
        SUM(expected_amount)                                            AS expected_amount,
        MAX(CASE WHEN po_unit_price IS NULL THEN 1 ELSE 0 END)          AS line_not_on_po,
        MAX(CASE WHEN qty_invoiced > qty_received + 1e-9
                   OR qty_invoiced > qty_ordered  + 1e-9 THEN 1 ELSE 0 END) AS qty_exceeds
    FROM line_check
    GROUP BY invoice_id
),

dup AS (
    SELECT
        a.invoice_id,
        MAX(CASE WHEN b.invoice_id IS NOT NULL THEN 1 ELSE 0 END) AS is_duplicate
    FROM new_inv a
    LEFT JOIN invoices b
           ON b.invoice_id < a.invoice_id
          AND b.extraction_status = 'OK'
          AND b.vendor_id = a.vendor_id
          AND (
                UPPER(REPLACE(REPLACE(b.invoice_number, '-', ''), ' ', ''))
                    = UPPER(REPLACE(REPLACE(a.invoice_number, '-', ''), ' ', ''))
             OR (b.total = a.total AND b.invoice_date = a.invoice_date)
          )
    GROUP BY a.invoice_id
),

flags AS (
    SELECT
        n.invoice_id,
        CASE WHEN n.extraction_status = 'FAILED' THEN 1 ELSE 0 END AS extraction_failed,
        CASE WHEN n.extraction_status = 'OK' AND n.po_number IS NULL THEN 1 ELSE 0 END AS missing_po,
        CASE WHEN n.extraction_status = 'OK' AND n.po_number IS NOT NULL AND p.po_number IS NULL
             THEN 1 ELSE 0 END AS po_not_found,
        CASE WHEN n.extraction_status = 'OK' AND (v.vendor_id IS NULL OR v.status <> 'APPROVED')
             THEN 1 ELSE 0 END AS vendor_not_approved,
        CASE WHEN n.extraction_status = 'OK' AND p.po_number IS NOT NULL AND p.vendor_id <> n.vendor_id
             THEN 1 ELSE 0 END AS vendor_po_mismatch,
        CASE WHEN n.extraction_status = 'OK' THEN COALESCE(d.is_duplicate, 0) ELSE 0 END AS duplicate_invoice,
        CASE WHEN n.extraction_status = 'OK' AND p.po_number IS NOT NULL
              AND (la.line_not_on_po = 1
                   OR la.expected_amount IS NULL
                   OR ABS(n.subtotal - la.expected_amount) > :tolerance * la.expected_amount)
             THEN 1 ELSE 0 END AS amount_out_of_tolerance,
        CASE WHEN n.extraction_status = 'OK' AND p.po_number IS NOT NULL
              AND (la.qty_exceeds = 1 OR la.line_not_on_po = 1)
             THEN 1 ELSE 0 END AS quantity_mismatch,
        CASE WHEN p.po_number IS NOT NULL THEN ROUND(la.expected_amount, 2) END AS expected_amount,
        CASE WHEN p.po_number IS NOT NULL AND la.expected_amount > 0
             THEN ROUND((n.subtotal - la.expected_amount) / la.expected_amount, 5) END AS amount_variance_pct
    FROM new_inv n
    LEFT JOIN purchase_orders p ON p.po_number = n.po_number
    LEFT JOIN vendors v         ON v.vendor_id = n.vendor_id
    LEFT JOIN line_agg la       ON la.invoice_id = n.invoice_id
    LEFT JOIN dup d             ON d.invoice_id = n.invoice_id
)

SELECT
    f.*,
    TRIM(
        CASE WHEN extraction_failed        = 1 THEN 'EXTRACTION_FAILED,'       ELSE '' END ||
        CASE WHEN missing_po               = 1 THEN 'MISSING_PO,'              ELSE '' END ||
        CASE WHEN po_not_found             = 1 THEN 'PO_NOT_FOUND,'            ELSE '' END ||
        CASE WHEN vendor_not_approved      = 1 THEN 'VENDOR_NOT_APPROVED,'     ELSE '' END ||
        CASE WHEN vendor_po_mismatch       = 1 THEN 'VENDOR_PO_MISMATCH,'      ELSE '' END ||
        CASE WHEN duplicate_invoice        = 1 THEN 'DUPLICATE_INVOICE,'       ELSE '' END ||
        CASE WHEN amount_out_of_tolerance  = 1 THEN 'AMOUNT_OUT_OF_TOLERANCE,' ELSE '' END ||
        CASE WHEN quantity_mismatch        = 1 THEN 'QUANTITY_MISMATCH,'       ELSE '' END,
        ','
    ) AS exception_codes
FROM flags f
ORDER BY f.invoice_id;
