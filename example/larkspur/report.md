# Pilot findings, as of 2025-10

Inputs: schedule `schedule.csv` (521 rows), invoices `invoices.csv` (511 lines). Hashes in manifest.json. Kit 2026-10-22.

## Totals

| | ARR |
|---|---:|
| Reported on the schedule | 868,800 |
| Supported by invoices | 834,000 |
| Unresolved, needs review | 34,800 |
| Accounts on the schedule | 37 |
| Accounts with an exception in the last 12 months | 10 |

## Exceptions by cause

| cause | accounts | unresolved ARR |
|---|---:|---:|
| unmatched_schedule_only | 1 | 25,200 |
| over_stated | 1 | 9,600 |
| classification | 1 | 0 |
| credit_or_rebill | 1 | 0 |
| duplicate_invoice | 1 | 0 |
| ramp_or_proration | 1 | 0 |
| timing | 1 | 0 |
| under_stated | 2 | 0 |
| unmatched_invoice_only | 1 | 0 |

What each cause means: supported, the invoices back the schedule within tolerance. over_stated, the schedule carries more than the invoices show. under_stated, the invoices show more than the schedule. timing, the same money lands a month earlier or later. credit_or_rebill, a credit memo or rebill touched the month. duplicate_invoice, the same invoice id appears twice. ramp_or_proration, the first months bill below the schedule. classification, a non recurring or untyped line sits where recurring revenue is expected. unmatched_schedule_only, no invoices found for the account. unmatched_invoice_only, invoices with no account on the schedule.

## Retention, three ways

| basis | from | to | starting book MRR | accounts | GRR | NRR |
|---|---|---|---:|---:|---:|---:|
| schedule | 2024-10 | 2025-10 | 75,800 | 41 | 84.2 | 85.0 |
| invoices | 2024-10 | 2025-10 | 69,400 | 38 | 84.7 | 84.7 |
| supported | 2024-10 | 2025-10 | 69,100 | 38 | 82.6 | 82.6 |

Schedule is what the company reports. Invoices is what the billing lines support on their own. Supported keeps the schedule figure wherever the invoices back it and drops to the invoice figure where they do not.

## Cohorts, revenue retained at 12 months

| cohort | accounts | schedule | supported |
|---|---:|---:|---:|
| 2024Q4 | 41 | 85.0 | 89.0 |

## Accounts to review first

| account | schedule MRR | invoice MRR | unresolved MRR | cause | months not supported |
|---|---:|---:|---:|---|---:|
| Silent Partner Inc | 2,100 | 0 | 2,100 | unmatched_schedule_only | 12 |
| Granite Peak Software | 5,000 | 4,200 | 800 | over_stated | 12 |
| Quiet Creek Software | 1,000 | 1,300 | 0 | under_stated | 12 |
| Orphan Billing LLC | 0 | 950 | 0 | unmatched_invoice_only | 9 |
| Juniper Ridge Labs | 8,000 | 8,000 | 0 | ramp_or_proration | 3 |
| Lantern Field Co | 0 | 1,400 | 0 | under_stated | 2 |
| Cedar Loop Systems | 3,000 | 3,000 | 0 | credit_or_rebill | 1 |
| Northwind Dynamics | 2,400 | 2,400 | 0 | timing | 1 |
| Maple Street Health | 2,600 | 2,600 | 0 | classification | 1 |
| Harbor Light Media | 1,700 | 1,700 | 0 | duplicate_invoice | 1 |

## Matching

Exact id: 40. Normalised name: 1. Ambiguous: 0. Schedule only: 1. Invoices only: 1. Full table in match_table.csv.

## What the files cannot support

Invoice lines with no type the kit could classify: 1. Lines with no service period (spread to the invoice month instead): 14. Mapping warnings: 1.

The kit does not read contracts, order forms, the general ledger or bank records. Where the schedule and the invoices disagree, the kit shows the disagreement and the lines behind it. Which figure is right is a question for the company's finance lead, and in a transaction for the diligence team. Nothing here is an accounting opinion.

## Questions for finance

* Where are the invoices for the accounts with none?
* Which accounts carry schedule MRR above the invoices, and is there a contract or a billing gap behind each one?
* Which untyped or one off lines are recurring by contract and which are not?
* What caused each credit memo, and was the rebill for the same period?
* Are the duplicate invoice ids a true double bill or an export artefact?
* Do the first months bill below the schedule by contract (ramp) or by proration, and should the schedule show the ramp?
* Is invoice timing a month off by design (billing in arrears) or by accident?
* Which accounts bill above the schedule, and is the schedule stale or the billing wrong?
* Which billed accounts are missing from the schedule, and why?
