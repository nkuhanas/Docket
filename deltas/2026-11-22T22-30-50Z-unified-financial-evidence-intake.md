# Docket — one-step financial evidence intake

**Proposed F2 amendment** · November 22, 2026 · Companion to [Financial ingestion and tax records](2026-11-22T22-08-43Z-financial-ingestion-and-tax-records.md) against [SPEC 1.2](../SPEC.md). F1 can ship its single-account manual/CSV import independently.

## 1. One submission, one result

The operator attaches a Micro Center receipt and says “60% business use, paid with my Amex.” The agent calls one proposed write tool, `docket_submit_financial_evidence`. Docket retains the original, decomposes it, and returns what it found. The response distinguishes what the receipt shows, what the operator declared, whether an expense was recorded or linked, and what still needs attention. “Amex” can remain a reported hint until a statement identifies the card.

| Surface | Proposed contract |
| --- | --- |
| Interactive agent | One required top-level `file` and the usual native command identity. Optional flat inputs are `business_use_percent` (exact decimal, 0–100 inclusive), `write_off_intent` and `reported_instrument` (known ref or short description). The agent passes operator declarations only when supplied. |
| Independent collector | One authenticated binary submission carrying the original and a stable source-delivery identity. The collector grant determines owner, permitted route and retained scope. An EML is one submission including admitted MIME parts. The collector calls the same intake service as the tool. |
| Result | A receipt with public source ref, processing status, bounded extracted facts with evidence anchors, declarations and their origin, disposition (evidence only, candidate, linked or recorded expense), and unresolved fields. A statement can return row counts and paginated exceptions. Any charge or settlement reported as matched carries its own observation ref. |

**Feasibility.** Docket's expense writer accepts an existing receipt source. Its attachment reader extracts bounded PDF text. The private attachment route already captures request-bound bytes with size limits, encryption and integrity checks. Its request path records an agent-reported origin. Reuse those storage guarantees and give verified collector deliveries their own genuine provenance. The published plugin needs a file-capable command. ChatGPT documents a top-level file parameter in `_meta["openai/fileParams"]` with a host file ID and temporary download URL. The tool adapter can fetch authorized bytes during the call. Keep this host detail outside the intake model. The collector can stream bytes directly. [Internal capture](../src/docket/internal_api/agent_router.py) · [Evidence storage and PDF reads](../src/docket/services/attachment_evidence.py) · [OpenAI file inputs](https://developers.openai.com/plugins/reference#define-file-inputs)

Docket owns staging, checksum verification, commit and retry. Callers have no begin/finalize sequence. Return completed results within the call's time budget when available. For slower work, return `processing` with the durable receipt and finish from retained bytes. Extend `docket_get_financial_context` to resolve that receipt and show recent intake needing attention. The operator only needs to act on a material ambiguity. If an approved client cannot supply original bytes, report the transfer failure and allow resubmission through a supported client.

## 2. Acceptance and recovery of original evidence

1. Authenticate and admit the request under the owner's native write grant or a collector grant limited to its approved source. Admit PDF, JPEG, PNG, EML and CSV with configured per-file, expanded-part and aggregate limits. Check actual content and byte length. Resolve only permitted host file references, including redirects, and keep temporary URLs, credentials and raw contents out of audit logs.
2. Stream to protected storage, calculate SHA-256 and verify any supplied checksum. Once bytes are durable, atomically publish the owned source, receipt, declaration provenance and pending extraction work. Clean up abandoned staging. A partial transfer returns no accepted receipt. A malformed field, unreadable scan, parser failure or unavailable local model after acceptance leaves the original retrievable under its retention policy.
3. Run deterministic parsers first and optional bounded local inference over admitted content. The backend tracks progress by source and part/row. A message with several distinct purchases may yield several candidates. An attachment repeating its enclosing invoice can corroborate the same candidate. Preserve anchors and uncertainty. The native tool description discloses that an authorized submission may record or link an unambiguous expense under the original write request. Deferred adoption rechecks that grant and the applicable policy. A collector submission defaults to a candidate unless a standing policy separately permits adoption. The extractor has no financial write authority.
4. Replay the same owner, operation and logical input to recover the same receipt, including after a lost response. Exclude expiring transport URLs from request identity. Changed declarations or different file identity/bytes under the same key produce a conflict. A separately authorized correction preserves the original declaration. Distinct deliveries of the same evidence retain their provenance. Link converging evidence to one cost after reliable matching, and leave genuinely distinct equal-value purchases separate.

## 3. Mail boundary and standing declarations

Prefer an **external authenticated IMAP collector for a dedicated billing mailbox** if the host supports it. An authenticated inbound webhook with verified event and original-message retrieval, a verified forwarding destination, or a scoped reader on an owned mail server can serve the same role. Choose the transport after establishing which service actually receives the mail. Docket consumes the authenticated intake submission independently of Gmail, IMAP, SMTP or MX configuration.

Bind the collector to the verified receiving mailbox and admission rule. For aliases, forwarding or shared inboxes, verify the delivery route or restricted folder/label. A visible recipient, sender, forwarded header or interactively uploaded EML cannot select the billing policy. Keep the source message identity and routing evidence. For IMAP, checkpoint mailbox, UID validity and UID after durable acceptance, and reconcile a changed UID epoch. Redelivered webhook events use stable delivery identities. Permanent transfer failures get an explicit recoverable exception before advancing past them. Recheck the current grant before retention and policy-based promotion. Pin the applied policy revision to admitted evidence.

| Source of declaration | Business use and write-off intent |
| --- | --- |
| Qualifying expense on the verified `billing@chacel.ai` route | Apply the operator's standing **100% business use and yes to write-off intent**. Preserve the policy revision, source and time. An unrelated message produces no expense. |
| Manual file with an explicit percentage | Retain the exact operator-reported percentage, including 0% or 100%. Write-off intent can be declared separately. |
| Manual file without a percentage | Business use stays unknown. An extractor suggestion may be shown with provenance. Only an operator declaration or applicable standing policy fills the declared amount. |

Funding, settlement, declared use and reviewed tax eligibility each have their own status. F2 preserves the declaration even before F4 workpapers ship. A missing instrument leaves the declaration intact. A reimbursement flags the tax treatment for review. The reviewer records the treatment separately and preserves the original declaration. Conflicting declarations on a manual copy and a mailbox copy remain visible against the same expense until an attributable correction resolves them.

## 4. What counts as working

| Pilot case | Observable result |
| --- | --- |
| ChatGPT submits one synthetic receipt | Tool discovery accepts the file descriptor. One invocation returns the source ref and results or a pending receipt. Compare retained byte count and digest to the fixture. Verify native schema and response budgets. |
| Receipt establishes a cost with unknown funding | An authorized writer can record the supported expense immediately with its evidence and unresolved funding. A later statement links to that expense. If the cost itself is unclear, the original and evidence-only candidate remain available. |
| A slow or interrupted submission | A disconnect before durable acceptance can retry. A disconnect after acceptance, expired download URL on replay or extractor restart returns or resolves the original receipt. Concurrent workers create one source per delivery and one adoption effect. |
| Isolated mailbox and manual submissions | A verified billing expense gets 100% and write-off intent. A manual copy addressed to the same alias receives only its explicit declaration. Test manual 60%, 0% and omitted, conflicting declarations, duplicate delivery, and a revoked collector. No mailbox or model can attest that a charge posted. |
| Invalid or inaccessible input | Over-limit bytes, a disallowed file origin or a cross-owner reference yield an explicit rejection. An accepted image with unreadable amounts remains evidence only. A model outage resumes from the retained original. |

Start with synthetic file transfer on the actual plugin client and an isolated collector feeding the same fixture path. Before enabling live mail, establish the receiving host/account, verify alias or folder routing, and record the permitted backfill, retention, access and collector grant. These decisions affect mailbox activation only.
