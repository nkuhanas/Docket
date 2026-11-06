# Forward delta index

Current specification: [SPEC 1.1](../SPEC.md).

| Delta | Adoption | Implementation |
| --- | --- | --- |
| [2026-11-05T04-27-33Z — single SPEC, context and local CI](2026-11-05T04-27-33Z-single-spec-context-and-local-ci.md) | Adopted for SPEC 1.0 by Operator implementation instruction | Complete: implemented, deployed and live host verified; [evidence](../docs/native-context-local-ci.md) |
| [2026-11-05T21-18-26Z — work attribution and personal operations](2026-11-05T21-18-26Z-work-attribution-and-personal-operations.md) | Adopted into SPEC 1.1 by Operator implementation instruction | Complete: C0, P0a/P0b, P1a–P1c, P2 and opt-in revision-pinned visibility implemented, deployed and host-accepted; tested-image activation, plugin 2.1.0 read-back and successful fresh ChatGPT work-context calls pass ([implementation evidence](../docs/work-attribution-implementation.md), [receipt](../evidence/work-attribution-release/activation.json), SPEC ADR 009). P3 requires its separate concrete provider delta |
| [2026-11-06T17-59-26Z — Google Tasks and NocoDB retirement](2026-11-06T17-59-26Z-google-tasks-and-nocodb-retirement.md) | Proposed against SPEC 1.1; target 1.2 on implementation instruction | Draft only: selected-list two-way task sync, six compact new tools, complete NocoDB retirement and removal of fifteen hidden native aliases; no code, credentials, provider data or runtime changed |

The Operator additionally requested removal of Hermes/SearXNG directories and
project Playwright artifacts. SPEC ADR 005 records that scoped amendment.
Older private registries/frozen sources remain unchanged and are not implicitly
imported into this forward index.
