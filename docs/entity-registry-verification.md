# Entity-registry hardening verification

Verified on 2026-08-27 against deployment `02d72e8`.

## Closed gaps

The canonical entity registry now exposes validated profiles rather than
arbitrary JSON keys. It supports bounded discovery by name, alias, metadata,
class, operator identity, and directional relationship. Exact reads return the
entity version, aliases, and active relationship snapshots, giving Hermes a
durable basis for relational phrases such as “my advisor” without relying on
conversation history.

Entity updates are attribute patches: omitted facts survive, while deletion
requires an explicit key list. Relationship predicates are closed and read as
`subject predicate object`; their metadata has optimistic versions and can be
corrected or retracted without deleting history. Only one active person may be
marked as the operator. No operator identity or social relationship is
auto-created.

Every interactive entity mutation now consumes its trusted Discord request key
through an operation/input hash. An exact retry replays the saved result; key
reuse for a different operation or payload fails with an idempotency conflict.

## Automated evidence

The repository passed:

* all 293 tests;
* Ruff and strict MyPy over 88 source files;
* SQLite upgrade/downgrade through migration `0025`; and
* the isolated dummy-provider Compose/MCP smoke.

Dedicated coverage proves profile patch preservation, metadata and alias
search, operator anchoring, directional relationship filtering, explicit
relationship correction/retraction, create-conflict handling, and entity-write
request replay.

## Deployment evidence

Before migration, production held 61 entities, zero aliases in use for this
migration decision, and zero relationships. The relation-predicate constraint
therefore required no coercion or deletion. Deployment created a database
backup and retained the previous image before applying migration `0025`.

After deployment:

* Docket and PostgreSQL were healthy;
* migration head was `0025`;
* active operations and pending outbox counts were both zero;
* the interactive Hermes MCP endpoint connected and discovered exactly 30
  allowlisted tools; and
* discovery included the then-current legacy registry surface. That surface is
  now historical; typed registry reads and ChangeSet mutation tools replaced
  it under the August 27 ontology migration matrix.

Existing Hermes conversations cache MCP discovery. Run `/reload-mcp` in the
active Discord session before exercising the new registry calls.
