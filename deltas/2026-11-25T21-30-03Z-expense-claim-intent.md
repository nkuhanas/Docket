# Docket expense claim intent

Adopted for SPEC 1.6 on the SPEC 1.5 baseline at `ac40de0`.

A manual expense upload with a positive reported business-use percentage declares
an intention to claim that share by default. The agent supplies the percentage
once. An explicit yes or no wins. Zero and omitted percentages carry no default.
Statement and income-document uploads keep their existing roles. Verified billing
mail uses its enrolled policy.

Apply the rule to new and earlier uploads when returning receipts, linked evidence
and fresh annual workpapers. Mark a derived intent with its policy origin. Keep
the raw declarations, original bytes, retry identity, and captured reports intact.
An unresolved expense or card can still have a declared share. Only the existing
tax-review command can establish a reviewed deduction.

With an expense of USD 120 and 60% reported business use, the intended amount is
USD 72. An explicit opt-out takes precedence over a default on linked evidence
and yields zero. Conflicting explicit decisions or percentages leave the
amount unresolved. Cover explicit decisions, zero and missing percentages, reads
before expense adoption, replay, old sources and pinned reports. Use the existing
file tool and its optional override. The change requires no data migration.
