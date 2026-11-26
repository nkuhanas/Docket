# Expenses inbox and agent review

Proposed on SPEC 1.7 at `89b03a2`. Nimble sorts incoming billing mail. Docket retains originals and outstanding items. The operator’s chosen agent inspects the evidence, asks about material ambiguities and records or links the expense. The same agent can finish a directly supplied receipt in the conversation where the operator gave it.

## What changes

| Area | Current behavior | Proposed change |
| --- | --- | --- |
| Mail acquisition and classification | Purelymail delivers original EML into protected financial intake. An optional classifier can suggest a type from plain email text for incompletely processed intake. | Run Nimble on each newly admitted billing message, independently of purchase extraction. Use bounded permitted message text and attachment descriptions for routing. |
| Expenses inbox | The financial context can open a known intake reference. Candidates retain per-purchase outcomes. | Add a paginated outstanding-expenses view to financial context, backed by these records. Show new items, items awaiting an operator answer and their source references. |
| Evidence access | Agents can read retained PDF text. The current read cannot show an image-only receipt or visually distinguish adjacent columns. | Add one owner-authorized visual read. Let the agent select a retained image or PDF page, including a specific attachment inside an EML. Return an image visible to the calling model, with its source, part and page identity. |
| Recording | Interactive submission can adopt one machine-extracted purchase. Mail produces candidates. An agent can record an expense and link the candidate separately. | Receipt submission retains evidence. The authorized agent makes the explicit booking decision. Extend the ordinary expense write to record reviewed fields and resolve the matching candidate in one transaction. Reuse the existing candidate command to link an existing cost or dismiss an item. |

Existing boundaries are documented in [financial intake operations](../docs/financial-intake-operations.md), the [file submission handler](../src/docket/plugin/financial_intake.py), the [classifier](../src/docket/services/financial_inference.py) and the [connector events implementation](../src/docket/services/notifications.py).

## Operator experience

**Billing mail.** Once the original is durable, Nimble routes likely purchase receipts and incoming vendor invoices to Expenses. Statements remain available for the existing statement-import workflow. Clear non-expenses are set aside with their classification and can be reopened. A message saying only “see attached,” an inconclusive classification or a model outage must leave the source discoverable for review. Classification can retry without fetching the mail again. The collector and Nimble have no expense-write permission.

**Connector events.** Publish the existing `docket.finance.attention` event with a generic summary and an intake reference when an expense item first needs agent review. Other financial obligations already use this event. The agent can recognize an intake reference and open Expenses directly. Commit the notification with the inbox transition and deduplicate retries. The agent reads the outstanding inbox on wake or catch-up. An unanswered question stays open without repeatedly waking the agent; new evidence or an operator answer can make it actionable again. Subscribing grants notification access only. A missed callback, a read or an acknowledgement never clears the work.

**Direct upload.** For “log this receipt, 60% business, Savor,” the agent submits the original once, keeps the declaration, inspects the attached or retained pages and books it in the same interaction if the facts are clear. Mailbox items skip the upload step. If the original transfer fails, report the error and stop. If it already arrived but the client cannot display the retained page, leave the item open with that specific reason. The original stays available to another authorized agent.

**Review and recording.** Show the agent safe mail context, available parts, optional text and any extracted suggestions. Keep visual reads page-bounded and protected by the owner’s evidence permissions. Keep private contents out of events and logs. For a $120 invoice showing $20 in credit and a $100 balance, the agent can inspect the labels and ask what the credit represents if it affects the expense. Record supported facts, the evidence reference and the acting principal. Funding may remain unresolved. An invoice alone establishes neither settlement nor a posted card charge.

Preserve the current declaration rules. A qualifying expense from the verified billing route carries 100% declared business use and claim intent. A manual upload retains the supplied percentage, including zero; an omitted share stays unknown. A positive share defaults to claim intent unless explicitly overridden. Reviewed tax treatment remains separate. Mail content and model output cannot override these declarations.

## Acceptance before activation

- Classify a vendor invoice, statement, unrelated message and attachment-only email. Simulate Nimble being unavailable. Retain every original and recover pending work without creating expenses or duplicating notifications.
- From an actual connected agent, inspect a synthetic image-only PDF **inside an EML** and read a value visible only on its second page. Repeat with a directly uploaded image. A text dump, download link or successful server-side rendering alone does not prove the model saw the page.
- Let the agent book an unambiguous receipt and leave an ambiguous one awaiting an answer. Resume from another authorized session. Verify the original, declaration, exact expense and inbox status after a lost response and concurrent attempts to book the same purchase.
- Deliver the same purchase by mail and direct upload. Link both sources to one economic expense and retain any conflicting declarations for review. Treat an explicitly requested receiptless cost, payroll evidence and account statements under their existing workflows.
- Exercise a missed event, duplicate delivery and an event-only principal. The outstanding list must support recovery and expense creation must still require the owner-approved write grant. Existing booked expenses and historical evidence keep their original attribution.
