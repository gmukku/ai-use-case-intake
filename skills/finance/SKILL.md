---
name: finance
description: Finance and Accounting - accounts payable and receivable, month-end close, expense management, budgeting, financial reporting, vendor payments.
---

# Finance / Accounting

Illustrative department profile for grounding discovery examples. Synthetic; not tied to any
real organization. Body headings mirror the discovery canvas categories.

## Classification hints

- Invoices, purchase orders, vendor payments, accounts payable or receivable, collections
- Expense reports, reimbursements, corporate cards, receipts
- Month-end close, reconciliations, journal entries, general ledger, audit support
- Budgets, forecasts, variance analysis, board reporting
- Vendor onboarding from the payment and tax angle (W-9, banking details)

## Key stakeholders

- Accounts payable and accounts receivable specialists
- Staff accountants and the controller who own the close
- FP&A analysts producing forecasts and variance commentary
- Expense approvers (managers) and employees submitting expenses
- Vendors and customers as external parties in the process
- Auditors who consume the documentation trail
- CFO for reporting and escalation

## Key activities

- Matching invoices to purchase orders and receipts before approval (three-way match)
- Reviewing expense reports against policy and flagging exceptions
- Reconciling bank statements to the general ledger at month end
- Chasing overdue receivables with reminder emails
- Building monthly variance commentary from actuals versus budget
- Typical cadence: daily for AP/AR intake; weekly for approvals; monthly close is a fixed cycle with a hard deadline; quarterly and annual for audit

## Value proposition

- Days to close the books and hours of manual reconciliation per cycle
- Invoice processing time and cost per invoice
- Exception rate on expense reports and duplicate-payment incidents
- Days sales outstanding for receivables
- Audit findings tied to missing documentation

## System integrations

- ERP / general ledger: NetSuite, QuickBooks, Sage Intacct, SAP, Microsoft Dynamics
- AP automation: Bill.com, Tipalti, Stampli, Coupa
- Expense management: Concur, Expensify, Ramp, Brex
- Banking and payments: bank portals, Stripe, Adyen
- Planning and reporting: Adaptive, Anaplan, Excel and Power BI
- Document storage: SharePoint, Box, the ERP attachment store
- Procurement and vendor master: Coupa, ERP vendor records

## Input source

- Accounting policy and expense policy documents
- Chart of accounts and vendor master data from the ERP
- Contract terms and payment schedules
- Prior-period close checklists and reconciliation workpapers
- Bank statements and payment processor exports

## Input types

- Document uploads: invoices (PDF or scanned), receipts, bank statements, contracts
- Typed prompts: "does this expense comply with policy?", "why is travel over budget this month?"
- Images: photos of receipts, screenshots of ERP screens
- Structured data: CSV exports from the ERP or bank
- Ask for samples: a few real invoices with and without matching POs; last month's variance report

## Output format

- Comparison: invoice versus PO versus receipt, with discrepancies highlighted
- Pulling data from a source: extracting invoice header and line items into a structured record for ERP entry (human-reviewed)
- Drafting into a template: collections reminder emails, variance commentary in the standard monthly format
- Summarization: condensing payment terms from a contract; summarizing a month of exceptions
- Q&A chatbot over accounting and expense policy
- Workflow automation: routing exceptions to the right approver with a human decision at the end
