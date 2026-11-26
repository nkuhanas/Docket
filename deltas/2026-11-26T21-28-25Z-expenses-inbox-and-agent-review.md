# Expenses inbox and agent review

Adopted into SPEC 1.8 on the SPEC 1.7 baseline at `89b03a2`. Nimble sorts incoming billing mail. Docket retains originals and outstanding items. The operator’s chosen agent inspects the evidence, asks about material ambiguities and records or links the expense. The same agent can finish a directly supplied receipt in the conversation where the operator gave it.

## What changes

| Area | Current behavior | Proposed change |
| --- | --- | --- |
| Mail acquisition and classification | Purelymail delivers original EML into protected financial intake. An optional classifier can suggest a type from plain email text for incompletely processed intake. | Run Nimble on each newly admitted billing message, independently of purchase extraction. Use bounded permitted message text and attachment descriptions for routing. |
| Expenses inbox | The financial context can open a known intake reference. Candidates retain per-purchase outcomes. | Add a paginated outstanding-expenses view to financial context, backed by these records. Show new items, items awaiting an operator answer and their source references. |
| Evidence access | Agents can read retained PDF text. The current read cannot show an image-only receipt or visually distinguish adjacent columns. | Add one owner-authorized visual read. Let the agent select a retained image or PDF page, including a specific attachment inside an EML. Return an image visible to the calling model, with its source, part and page identity. |
| Recording | `docket_submit_financial_evidence` may record a machine-extracted purchase. An agent can also record an expense and link the candidate separately. | Remove automatic booking from the handler and its advertised contract. Submission, worker processing and replay may retain evidence and produce candidates at any confidence level. Creating or linking an expense always requires a separate, attributable, authorized agent command. |

Existing boundaries are documented in [financial intake operations](../docs/financial-intake-operations.md), the [file submission handler](../src/docket/plugin/financial_intake.py), the [classifier](../src/docket/services/financial_inference.py) and the [connector events implementation](../src/docket/services/notifications.py).

## Operator experience

**Billing mail.** Every admitted message has a discoverable routing outcome, including pending, uncertain and failed classification. Record the outcome for each distinguishable document in the body or attachments. One email can put its invoice in Expenses, its statement in the statement-import workflow and its brochure aside. When Nimble cannot distinguish the parts, the message goes to agent review with its attachments intact. An empty body, ambiguous result or classifier outage also leaves reviewable work. An agent can find a receipt Nimble set aside and reclassify it using the retained original. Preserve earlier classifications and their provenance. A classifier retry cannot undo a subsequent agent decision.

**Connector events.** The outstanding-expenses view is the durable queue. When an item becomes actionable, publish the existing `docket.finance.attention` event with a generic summary and an intake reference. Other obligations use the same event. The intake reference tells the agent to open Expenses. Commit the notification with the inbox transition and deduplicate retries. A subscribed agent reads the queue on wake. A polling or reconnecting agent uses the same view. A missed callback, a read or an acknowledgement never clears the work. Subscribing grants notification access only.

**Direct upload.** For “log this receipt, 60% business, Savor,” the agent submits the original once, keeps the declaration, inspects the attached or retained pages and books it in the same interaction if the facts are clear. Mailbox items skip the upload step. If the original transfer fails, report the error and stop. If it already arrived but the client cannot display the retained page, leave the item open with that specific reason. The original stays available to another authorized agent.

**Visual review.** The agent can request a model-visible image for a particular retained source, MIME part (for mail) and page. Return those identifiers alongside the image. Resolve the part and page through the authorized owner’s source on every read. Bound page count, pixel resolution and response size. Report a limit or unreadable page explicitly. A client that can only show a download link cannot complete visual review. Keep document contents out of events and logs.

Preserve the current declaration rules. A qualifying expense from the verified billing route carries 100% declared business use and claim intent. A manual upload retains the supplied percentage, including zero; an omitted share stays unknown. A positive share defaults to claim intent unless explicitly overridden. Reviewed tax treatment remains separate. Mail content and model output cannot override these declarations.

## Outstanding-item lifecycle

Durable receipt and routing are source-level milestones. After routing, track review per candidate so a question about one purchase cannot block another from the same email. The outstanding view in `docket_get_financial_context` shows actionable and waiting items. Resolved and set-aside outcomes remain discoverable through history.

| State | Entry and next action |
| --- | --- |
| Needs agent review | A direct submission, relevant or uncertain mail, a reopened item, an answer or material new evidence makes the candidate actionable. The agent inspects it, then books, links, asks or dismisses. |
| Awaiting operator | Record the exact question, candidate, source and relevant part or page, who asked and when. For a $120 invoice with a $20 credit, ask what the credit represents if that changes the cost. Another session can resume the question. Store the answer, its author and time on the same record, then return the candidate to review. Waiting alone emits no further wake-ups. |
| Booked or linked | Extend `docket_record_expense` to book reviewed facts against a candidate. Extend `docket_update_financial_candidate` for the review transitions and reuse its existing-expense link. Commit the expense creation or link and candidate resolution together. Return the canonical expense reference. Funding may remain unresolved. Settlement requires independent evidence. |
| Dismissed or set aside | Retain the reason, actor or classifier, and original evidence. Authorized reclassification or reopening returns the item to review. |

Use existing operation IDs, actor attribution and candidate versions for decisions. Replaying a successful action returns its recorded result. A stale decision cannot overwrite an answer or book a second expense. Corrections to a booked expense use the existing revision history.

## Native decisions and evidence identity

Use the candidate reference and its version in the owner-bound intake context. The candidate binds a specific purchase and retained MIME part within the email. On `docket_update_financial_candidate`, asking stores the question and moves to `awaiting_operator`; answering records the answer and returns to `needs_review`. Dismissal and reopening are attributable decisions. Reopening returns the item to actionable review. Booking through `docket_record_expense(candidate_ref=…)` resolves to `booked`; an explicit existing-expense link resolves to `linked`. Preserve the decision and current state independently so reopened work is discoverable. Require an attributable basis for a cross-source link.

Two candidates can point to one expense. Keep both originals, document anchors and source-specific declarations, exposing disagreements. Similar vendor, date and amount can suggest a match but cannot establish identity. Report a previously booked strong identity to the reviewer for explicit linking. Preserve historical bookings and replay of completed commands.

## Acceptance before activation

- Submit a directly uploaded receipt whose fields the existing parser recognizes, then process and replay it. Repeat through mail. Verify zero expense writes until the agent’s separate booking call, including for high-confidence candidates. The direct submission and booking should complete in one conversation.
- Exercise mixed attachments, attachment-only mail, uncertain routing and a Nimble outage. Recover a deliberately misclassified receipt from set-aside history without fetching the email again. Verify its source digest, its sibling documents’ routes and one notification for the newly actionable work.
- From the selected agent in its deployed client, read a test value visible only on page two of a synthetic image-only PDF **inside an EML**. Verify the source, part, page and value against the fixture. Repeat with a directly uploaded image. Exercise limit errors. Reject a guessed part reference from another owner without disclosing content or part metadata.
- Leave one candidate awaiting an answer while another is booked. Resume in another authorized session, retrieve the exact question, record the answer and finish booking. Verify the original, declaration, answer history and expense reference survive a lost response and concurrent booking attempts.
- Deliver the same purchase by mail and direct upload. Link both sources to one economic expense and retain any conflicting declarations for review. Treat an explicitly requested receiptless cost, payroll evidence and account statements under their existing workflows.
- With the selected agent’s deployed subscription, observe the agent actually receive a synthetic expense event and retrieve the referenced outstanding item. Disconnect it, create another item, then recover that item by polling the queue. Distinguish client receipt and catch-up from server publication. Verify duplicate delivery and an event-only principal cannot resolve the item. Preserve existing booked expenses and historical attribution.
