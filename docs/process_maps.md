# Process maps: before and after

Procure-to-pay, from the moment a vendor invoice arrives to the moment it is
paid or resolved. The "before" map is the typical manual AP process this
project replaces; the "after" map is what the pipeline does.

## Before: manual three-way match

```mermaid
flowchart TD
    A([Vendor emails invoice PDF<br/>to ap@ shared inbox]) --> B[AP clerk opens email,<br/>saves PDF to a folder]
    B --> C[Clerk keys vendor, invoice #, PO,<br/>lines and totals into the ERP]
    C --> D{PO number<br/>on invoice?}
    D -- No --> E[Email vendor / buyer<br/>asking for PO]
    E -. days .-> C
    D -- Yes --> F[Look up PO in ERP,<br/>compare prices by eye]
    F --> G[Email receiving / requester:<br/>did the goods arrive?]
    G -. days .-> H{Prices and<br/>quantities agree?}
    H -- No --> I[Email thread with buyer,<br/>vendor and requester]
    I -. days or weeks .-> H
    H -- Yes --> J{Seen this invoice<br/>before?}
    J -- "Not checked<br/>(relies on memory)" --> K[Approval request by email<br/>to budget owner]
    K -. days .-> L[Clerk adds to payment batch<br/>in a spreadsheet]
    L --> M([Treasury pays])
    M --> N[Month end: FP&A pulls ERP and<br/>spreadsheets for budget vs actual]

    classDef pain fill:#fdecea,stroke:#dc3c3d,color:#191919
    class C,E,G,I,J,K pain
```

Pain points (red):

| Step | Problem |
|---|---|
| Manual keying | Slow, typos in amounts and PO numbers, no validation |
| Chasing POs and receipts | Done by email, no owner, no timer, no audit trail |
| Mismatch resolution | Lives in inboxes; nobody can see how many are stuck or for how long |
| Duplicate check | Depends on someone remembering; resent invoices get paid twice |
| Approval | Email approvals, hard to audit |
| Reporting | Spend and budget vs actual only at month end, assembled by hand |

## After: automated pipeline

```mermaid
flowchart TD
    A([Vendor invoice PDF lands in<br/>Google Drive intake folder]) --> B[Trigger: n8n / Workato recipe<br/>picks up new file]
    B --> C[LangChain + Claude reads the PDF<br/>vendor, invoice #, PO, dates, lines, totals]
    C --> D{Pydantic gate:<br/>fields present, formats valid,<br/>lines x price = amount,<br/>lines sum to subtotal,<br/>subtotal + tax = total?}
    D -- No --> X1[EXTRACTION_FAILED]
    D -- Yes --> E[(Store in SQL:<br/>invoices + invoice_lines)]
    E --> F[SQL three-way match<br/>invoice vs PO vs goods receipt]
    F --> G{All controls pass?<br/>PO present and valid<br/>vendor approved<br/>not a duplicate<br/>amount within ±2%<br/>qty ≤ received}
    G -- Yes --> H[Append to<br/>'Approved for payment' Google Sheet]
    H --> I([Treasury payment run<br/>on due date])
    G -- No --> X2[Exception codes]
    X1 --> J[Create Jira ticket<br/>with codes + context]
    X2 --> J
    J --> K[Jira Automation:<br/>assign by department,<br/>flag high-dollar,<br/>remind after 3 days]
    K --> L[AP specialist resolves<br/>only the exceptions]
    E --> M[[Reporting views → dashboard:<br/>aging, spend, budget vs actual,<br/>exception rate, processing time]]
    J --> M

    classDef auto fill:#e8f1fc,stroke:#257de0,color:#191919
    classDef human fill:#fff4e5,stroke:#f47230,color:#191919
    class B,C,D,E,F,G,H,J,K,M auto
    class L human
```

What changed:

| Before | After |
|---|---|
| Every invoice touched by a person | Only exceptions are touched (27% of the demo set, all planted problems) |
| Keying errors go straight into the ERP | Bad extractions are rejected by the Pydantic gate and ticketed |
| Mismatches chased over email | Each mismatch is a Jira ticket with an owner, a priority and a 3-day reminder |
| Duplicate check by memory | Duplicate check in SQL against every invoice ever received |
| Month-end reporting | Dashboard over live SQL views, refreshed on every run |

## Swimlane view (who does what)

```mermaid
sequenceDiagram
    participant V as Vendor
    participant D as Google Drive
    participant O as Orchestrator (n8n / Workato)
    participant P as Python pipeline
    participant S as SQL (ERP + AP tables)
    participant J as Jira
    participant G as Google Sheet
    participant A as AP specialist
    V->>D: Invoice PDF
    D-->>O: New file trigger
    O->>P: run --json
    P->>P: LangChain extraction + Pydantic gate
    P->>S: Insert invoice, run three-way match SQL
    S-->>P: Exception codes
    P-->>O: Outcome per invoice
    alt Clean match
        O->>G: Append "Approved for payment" row
    else Exception
        O->>J: Create ticket (component, labels, priority)
        J->>A: Assigned by department
        J-->>A: Reminder if open 3 days
    end
```
