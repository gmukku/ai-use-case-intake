---
id: cs-ticket-triage-and-escalation
title: Support Ticket Triage and Escalation
department: customer_success
owner: Support Operations
last_updated: 2026-08-01
summary: Priority definitions, first-response and resolution targets, the triage routing rules, and when and how a ticket is escalated to tier 2 or engineering.
---

# Support Ticket Triage and Escalation

*Northbridge Group internal SOP. Synthetic document for demonstration.*

## Purpose

Get every ticket to the right person at the right urgency, and meet the response commitments
in customer contracts.

## Priority definitions and targets

| Priority | Definition | First response | Resolution target |
|---|---|---|---|
| P1 | Service unavailable or data at risk for a customer | 30 minutes, 24x7 | 4 hours |
| P2 | Major feature unusable, no workaround | 2 business hours | 1 business day |
| P3 | Feature degraded or workaround exists | 8 business hours | 3 business days |
| P4 | Question, how-to, or enhancement request | 1 business day | 5 business days |

Enterprise-tier customers have contractual targets that may be tighter; the account record
shows the tier.

## Triage (tier 1, within the first-response window)

1. Confirm the customer, account tier, and product area from the ticket and CRM.
2. Set priority per the table. When unsure between two priorities, choose the higher.
3. Check the knowledge base and the recent-incidents board before replying. If a KB article
   answers the question, reply with the article plus a one-paragraph summary in the agent's
   own words; never paste an article without context.
4. Tag the ticket with product area and root-cause category once known.

## Escalation

- **To tier 2** when a tier-1 agent cannot reproduce or resolve within 2 business hours of
  work, or when the ticket needs account-level configuration changes.
- **To engineering** when tier 2 confirms a defect. Tier 2 files the issue in the engineering
  tracker with: steps to reproduce, expected versus actual behavior, environment and version,
  customer impact and count of affected customers. The ticket stays open and linked.
- **To the account's CSM** when the customer mentions churn, a renewal, an executive, or
  when three or more tickets are open for one account.

## Communication rules

Update the customer at least every business day on P1 and P2 tickets, even if there is no
news. Do not promise dates that engineering has not confirmed.

## Systems

Help desk (tickets, macros, SLAs), knowledge base, CRM (account tier, CSM), engineering
tracker, incident status board.
