# Jira setup and automation rules

The pipeline creates one ticket per exception invoice in project `AP`
(configurable with `JIRA_PROJECT_KEY`). It sets **component**, **labels** and
**priority**; Jira Automation does the rest. Keeping routing in Jira means AP
leads can change who owns what without a code change.

## Project setup

1. Create a Jira Software or Jira Work Management project with key `AP`.
2. Add one component per department, each with a component lead:

   | Component | Department | Lead (assignee) |
   |---|---|---|
   | AP-Engineering | Engineering | priya.raman |
   | AP-Operations | Operations | marcus.lee |
   | AP-Marketing | Marketing | sofia.alvarez |
   | AP-Facilities | Facilities | daniel.okafor |
   | AP-Finance | Finance | hannah.kim |
   | AP-People | People | omar.haddad |
   | AP-IT | IT | grace.chen |
   | AP-Triage | No PO / unreadable invoice | ap.triage |

   (The names are the synthetic AP owners in `departments.ap_owner`.)
3. Create an API token at id.atlassian.com and set `JIRA_BASE_URL`,
   `JIRA_EMAIL`, `JIRA_API_TOKEN`, then `AP_JIRA_MODE=live`.

## Fields the pipeline sets

| Field | Value |
|---|---|
| Summary | `[Exception Type] Vendor invoice INV-123 ($4,250.00)` |
| Description | Source file, vendor and status, invoice #, PO #, department, totals, PO-priced value and variance %, extraction error, and a plain-English line per failed control |
| Priority | `Highest` if total ≥ $10,000; else `High` for duplicates and unapproved vendors; else `Medium` |
| Labels | `ap-exception`, one label per code (`missing-po`, `duplicate-invoice`, ...), `high-dollar` at ≥ $10,000 |
| Component | Department of the PO the invoice references, or `AP-Triage` |

## Automation rules

Create these under Project settings → Automation.

### 1. Assign by department

- **Trigger:** Issue created
- **Condition:** Labels contains `ap-exception`
- **Action:** Assign issue → *Component lead*

### 2. Flag high-dollar exceptions

- **Trigger:** Issue created
- **Condition:** Labels contains `high-dollar`
- **Actions:**
  - Edit issue: Priority = Highest
  - Add watcher: AP manager (and Controller)
  - Send Slack/email: "High-dollar AP exception {{issue.key}}: {{issue.summary}}"

### 3. Reminder after 3 days

- **Trigger:** Scheduled, daily at 9:00, JQL:
  `project = AP AND labels = ap-exception AND statusCategory != Done AND created <= -3d AND (updated <= -3d)`
- **Actions:**
  - Add comment: "Reminder for {{issue.assignee.displayName}}: this AP exception has been open {{issue.created.diff(now).days}} days (SLA 3 days)."
  - Send email to assignee.
  - (Optional) If open more than 6 days, edit Priority = Highest and notify the AP manager.

### 4. Release the invoice when resolved

- **Trigger:** Issue transitioned to Done
- **Action:** Send web request to the orchestrator webhook (n8n Webhook node or
  Workato webhook trigger) with `{{issue.key}}` so the invoice can be
  re-matched and, if clean, appended to the payment sheet.

## Offline equivalent

`MockJira` (default) stores the same tickets in the `exception_tickets` table
and `outputs/jira_tickets.jsonl`, and applies rules 1 and 2 in
`integrations/jira.py:build_ticket`. Rule 3 runs as `python -m ap_automation sla`
(`send_sla_reminders`), which comments once per SLA window.
