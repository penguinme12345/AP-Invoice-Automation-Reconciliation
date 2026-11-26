# Runbook: AP invoice automation

For whoever supports the pipeline day to day. Short version: **every invoice
ends up either on the "Approved for payment" sheet or as a Jira ticket. If
one is in neither place, something is wrong.**

## What runs, and when

| Job | Trigger | Command |
|---|---|---|
| Intake + match | New PDF in the Drive intake folder (n8n / Workato, every 5 min) | `python -m ap_automation run --json --integrations none` (orchestrator does Jira + Sheets) or `python -m ap_automation run` (Python does them) |
| SLA reminders | Jira Automation scheduled rule, daily 9:00 (or `python -m ap_automation sla`) | |
| Dashboard + BI CSVs | Weekdays 7:00 | `python -m ap_automation report` |
| Link Jira key | After the orchestrator creates the issue | `python -m ap_automation link-ticket <invoice_id> <JIRA-KEY>` (replaces the `LOCAL-<id>` placeholder) |

`run` never marks invoices paid. `PAID` status (and the `payments` table) is simulated only by `demo`; in production it must come from the ERP or bank payment confirmation.

State lives in the SQL database (`AP_DB_PATH`, default `data/ap.db`). Each run
is logged in `pipeline_runs`.

## Daily check (5 minutes)

1. Open the dashboard. Check **Past 3-day SLA** and the exception queue.
2. Confirm the last run finished:
   ```sql
   SELECT * FROM pipeline_runs ORDER BY run_id DESC LIMIT 5;
   ```
   `finished_at` should be set and `files_new` should roughly match what
   arrived in Drive.
3. Reconciliation check, should return no rows:
   ```sql
   SELECT i.invoice_id, i.source_file, i.status FROM invoices i
   LEFT JOIN approved_for_payment a USING (invoice_id)
   LEFT JOIN exception_tickets t USING (invoice_id)
   WHERE a.invoice_id IS NULL AND t.invoice_id IS NULL;
   ```

## Handling each exception code

| Code | What it means | What to do |
|---|---|---|
| `EXTRACTION_FAILED` | The PDF could not be read, or what was read did not add up (Pydantic gate). The error is in the ticket. | Open the PDF. If it is a bad scan, ask the vendor for a clean PDF or key it manually. If the math really is wrong on the invoice, reject it to the vendor. |
| `MISSING_PO` | No PO number printed. | Find the PO by vendor and amount in the ERP, or ask the requester to raise one. Reply to the vendor asking them to quote the PO. |
| `PO_NOT_FOUND` | PO number printed but not in the ERP. | Usually a typo or a PO from another entity. Correct it with the vendor. |
| `VENDOR_NOT_APPROVED` | Vendor is pending onboarding or blocked. | Do not pay. Send to Procurement / vendor management. |
| `VENDOR_PO_MISMATCH` | The PO belongs to a different vendor. | Check for a reseller or acquired vendor; otherwise reject. |
| `CURRENCY_MISMATCH` | Invoice is in a different currency from the PO. | Confirm the agreed currency with the buyer; ask the vendor to reissue, or amend the PO. |
| `DUPLICATE_INVOICE` | Same vendor + invoice number (or same vendor, date and total) already received. | Confirm the original was approved or paid, then close the ticket and reply to the vendor. Never pay both. |
| `AMOUNT_OUT_OF_TOLERANCE` | Invoice subtotal differs from PO price × billed quantity by more than ±2%. The ticket shows the variance. | Ask the buyer whether a price change was agreed. Get a credit note or a PO change order. |
| `QUANTITY_MISMATCH` | Units billed (this invoice plus earlier approved invoices on the same PO line) are more than were received or ordered. | Check with receiving. If goods are still coming, hold until the receipt is posted, then re-run. |

After fixing the underlying data (e.g. PO amended or receipt posted), the
invoice needs a re-match. Today that means deleting its `match_results` row and
re-running, or approving it manually by adding it to the sheet. Wiring Jira's
"Done" transition to a re-match webhook is rule 4 in `docs/jira_automation.md`.

## Common problems

| Symptom | Likely cause | Fix |
|---|---|---|
| Many `EXTRACTION_FAILED` with `extractor error: AuthenticationError` | `ANTHROPIC_API_KEY` missing or expired | Fix the key in the environment the orchestrator uses. Re-run: failed files are already recorded, so delete those `invoices` rows (and their `match_results`) first, or move the PDFs back in with a new name. |
| `extractor error: RateLimitError` / timeouts | Burst of invoices | The client retries 3 times. Lower the orchestrator batch size; re-run picks up only new files. |
| Same invoice appears twice in the sheet | Should not happen: files are de-duplicated by SHA-256, invoices by vendor + number | Check whether the vendor changed the invoice number format. Add a normalisation rule in `three_way_match.sql` (the `dup` CTE). |
| Jira returns 400 on create | Component or priority name does not exist in the project | Create the component / check the priority scheme; names must match `departments.jira_component`. |
| Google Sheets 403 | Sheet not shared with the service account | Share the sheet with the service account's email as Editor. |
| Vendor resolved to `NULL` (shows as unapproved) | Name on invoice is too different from the vendor master | Add or correct the vendor master name; the matcher needs ≥ 0.88 similarity after removing Inc/LLC/etc. |

## Changing business rules

| Rule | Where |
|---|---|
| Amount tolerance (±2%) | `AP_TOLERANCE_PCT` |
| High-dollar threshold ($10k) | `AP_HIGH_DOLLAR`, and the matching Jira rule |
| SLA (3 days) | `AP_SLA_DAYS`, and the Jira scheduled rule JQL |
| Routing (who owns a department) | Jira component lead (and `departments` table for the mock) |
| A new control | Add a flag in `ap_automation/sql/three_way_match.sql`, a description in `CODE_TEXT` in `integrations/jira.py`, and a test |

Run `python -m pytest` before deploying any change.

## Recovery

- The pipeline is idempotent: re-running never re-ingests a file it has seen
  (SHA-256) and only matches invoices without a `match_results` row.
- To rebuild the demo from scratch: `python -m ap_automation demo` (it wipes and
  regenerates the synthetic data; never point it at a production database).
