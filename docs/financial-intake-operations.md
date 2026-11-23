# Financial intake operations

## Funding and original evidence

From the native connection, read `docket_get_funding_context`. Use the create/update funding-account and payment-instrument commands with the returned context and a stable operation ID. Register an authorized-user instrument with its own cardholder, liability and account references. Two instruments can share a masked suffix. For a replacement, create the successor with `replaces_ref`, then record the predecessor's retirement. Previous activity keeps the revisions observed at posting.

Submit an original PDF, JPEG, PNG, EML or CSV using `docket_submit_financial_evidence`. Use the host-supplied file parameter. Reuse the same operation ID after a lost response. Read the receipt through `docket_get_financial_context(intake_ref=…)`. The response reports original-byte acceptance, digest, extraction progress, per-purchase disposition, declarations and funding/review gaps. The native connection may record one unambiguous purchase. Mail and optional local inference produce candidates. Use `docket_update_financial_candidate` to adopt a candidate or link it to a known expense, instrument or exact posted activity. Pass intended link targets in the intake context's `refs` list. An image without readable facts remains available for review.

Use `docket_create_account_activity` for manual observations. For a statement, supply the retained CSV source and account together in a funding context. Preview `docket_create_statement_import` before accepting the mapping. CSV uses ISO `posted_on`, `description`, signed `amount` (negative for outflows), and `currency`. Optional columns are `external_id`, `kind`, `state` and `corrects_ref`. Use stable account-scoped bank IDs where available. Inspect the resulting statement and activity refs. A card repayment can be recorded as a transfer. The original economic cost retains its own identity.

## Dedicated billing mailbox

The operator enrolls the IMAP-only Purelymail application password from a private terminal at the repository root. The label is `docket-integration`. Enrollment can be repeated to rotate the mail password. The Docket source identity and existing receipts survive rotation.

```sh
scripts/financial-collector enroll-mail
scripts/financial-collector test
```

The private supervised profile uses the running Docket service's image. The connection test authenticates as `billing@chacel.ai` on `imap.purelymail.com:993`, verifies the TLS peer and selects INBOX read-only. A TCP timeout means the connection could not reach authentication. Check outbound connectivity from the collector network before replacing credentials.

After the deployed image passes the connection test, authorize the dedicated source with its separate Docket token and the agreed scope. Backfill starts November 22, 2026, inclusive by IMAP internal date. Originals are encrypted, access is owner-scoped, and retention continues until operator removal. This is the policy chosen for this mailbox. A new authorization captures a new policy revision.

```sh
scripts/financial-collector authorize --owner-only-retain-until-removed
scripts/financial-collector start
scripts/financial-collector status
```

For activation, place one synthetic receipt in the dedicated mailbox. Verify its source, original SHA-256 when original message bytes are available, extraction state and 100% declaration in Docket. Confirm the checkpoint, repeat the scan and confirm one accepted delivery. Watch the pending delivery and consecutive failures. The collector leaves flags, folders and deletion state alone. UIDVALIDITY changes start a new delivery epoch. Replayed purchases retain their original expense identity. Revoke the source to stop new admissions, including from a still-running collector.

```sh
scripts/financial-collector revoke
```

A verified mailbox declaration applies to qualifying expenses. An address in an uploaded message confers no mailbox policy. An explicit manual 0% is retained. Missing funding, conflicting declarations and unreviewed tax treatment remain visible. A receipt cannot establish that a charge posted.

## Optional local suggestions

The default extractor uses deterministic receipt labels and retained MIME/PDF parts. For admitted plain billing text, an administrator may enable a local classifier or extractor with `DOCKET_FINANCIAL_CLASSIFIER_MODEL`, `DOCKET_FINANCIAL_EXTRACTION_MODEL` and `DOCKET_FINANCIAL_OLLAMA_URL`. Configure the actual local model identifiers and a permitted local runtime address. The worker retains evidence-anchored proposals and bounded retry state. It has no authority to create instruments, declare business use or adopt purchases. No local model setup is required to accept originals.
