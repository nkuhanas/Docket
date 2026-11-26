# Original receipts for file-backed expenses

Adopted on the SPEC 1.6 baseline at `6a4d7c9`. When an operator attaches a
bill to log a cost, accept the original through the host file parameter. The
agent reports success only against a durable source receipt. On transfer
failure it gives the error and stops. It neither requests another upload nor
falls back to a manual cost. A lost response to a successful submission can
still replay the same operation.

A new direct expense requires a retained original owned by the operator or
explicit intent to record a standalone, receiptless cost. The direct command
rejects unbacked references and a receiptless fallback from a known file
operation. A host failure ahead of MCP has no Docket request to correlate.
The authenticated agent is responsible for describing the operator's intent
accurately. Independent text-only reports remain possible, including costs for
which a reimbursement policy will require a receipt later. Earlier expenses,
committed retries and exact saved proposals keep their original provenance.
No data migration or additional upload interface is needed.

Test an unbound attachment followed by a direct create, a bogus source, an
explicit standalone report, and a successful handoff with digest and replay.
Distinguish a Docket-side DNS failure from an upstream failure that never
reaches the audited submission tool. Preserve host and redirect checks.
