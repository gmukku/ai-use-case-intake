---
id: fin-invoice-three-way-match
title: Accounts Payable Invoice Three-Way Match
department: finance
owner: Accounts Payable
last_updated: 2026-05-12
summary: How AP matches vendor invoices to purchase orders and receiving records before approval and payment, including tolerances and exception handling.
---

# Accounts Payable Invoice Three-Way Match

*Northbridge Group internal SOP. Synthetic document for demonstration.*

## Purpose

Pay only for goods and services that were ordered and received, at the agreed price, and
create a documentation trail auditors can follow.

## Scope

All vendor invoices above $500 that reference a purchase order. Invoices without a PO follow
the "Non-PO Invoice Approval" SOP instead.

## The three documents

1. **Purchase order** (created in the ERP by the requesting department, approved per the
   delegation-of-authority matrix)
2. **Receiving record** (goods receipt entered by the receiving team, or service confirmation
   from the requester)
3. **Vendor invoice** (arrives via the AP mailbox or the AP automation tool)

## Procedure

1. AP automation extracts the invoice header and lines and links the PO number. If no PO
   number is present, AP emails the vendor and the requester the same day.
2. Match quantity and unit price per line against the PO and the receiving record.
3. **Tolerances:** price variance up to 2% or $50 per line (whichever is lower) and quantity
   variance up to 1 unit on counted goods are auto-approved. Anything beyond tolerance is an
   exception.
4. Exceptions are routed to the requester with the variance highlighted. The requester has 3
   business days to accept the variance (PO amendment) or dispute it with the vendor.
5. Matched invoices post to the ledger and enter the payment run according to vendor terms
   (net 30 default). Early-payment discounts are taken when the invoice is matched at least 5
   business days before the discount date.
6. Duplicate check: the AP tool flags any invoice with the same vendor, amount, and invoice
   number as a prior invoice; AP confirms before posting.

## Controls

- Segregation of duties: the person who creates a PO cannot approve the matched invoice.
- Monthly report of exceptions older than 10 business days goes to the controller.
- Vendor master changes (banking details) require a callback verification, see "Vendor
  Master Change Control".

## Systems

ERP (POs, ledger, vendor master), AP automation tool (intake, extraction, matching),
receiving system, AP shared mailbox.
