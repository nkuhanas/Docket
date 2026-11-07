# Forward delta index

Current specification: [SPEC 1.2](../SPEC.md).

| Delta | Adoption | Implementation |
| --- | --- | --- |
| [2026-11-05T04-27-33Z — single SPEC, context and local CI](2026-11-05T04-27-33Z-single-spec-context-and-local-ci.md) | Adopted for SPEC 1.0 by Operator implementation instruction | Complete: implemented, deployed and live host verified; [evidence](../docs/native-context-local-ci.md) |
| [2026-11-05T21-18-26Z — work attribution and personal operations](2026-11-05T21-18-26Z-work-attribution-and-personal-operations.md) | Adopted into SPEC 1.1 by Operator implementation instruction | Complete: C0, P0a/P0b, P1a–P1c, P2 and opt-in revision-pinned visibility implemented, deployed and host-accepted; tested-image activation, plugin 2.1.0 read-back and successful fresh ChatGPT work-context calls pass ([implementation evidence](../docs/work-attribution-implementation.md), [receipt](../evidence/work-attribution-release/activation.json), SPEC ADR 009). P3 requires its separate concrete provider delta |
| [2026-11-06T17-59-26Z — Google Tasks and NocoDB retirement](2026-11-06T17-59-26Z-google-tasks-and-nocodb-retirement.md) | Adopted into SPEC 1.2 by Operator implementation goal | Complete: deployed from 4236a81e55fd (initial activation c7d25789ecfb): source-bound release/PostgreSQL checks and actual runtime method admission pass; same private plugin is 2.2.1. Exact NocoDB runtime/storage/images and fifteen aliases retired. Fresh ChatGPT Tasks context passes in the [host read receipt](../evidence/google-tasks-retirement/host-context.json). [Activation receipt](../evidence/google-tasks-retirement/activation.json) and [acceptance matrix](../docs/google-tasks-implementation.md) include successful [native write journeys](../evidence/google-tasks-retirement/native-journey.json). Intended-device popup and exact fixture cleanup pass in the [device receipt](../evidence/google-tasks-retirement/device-acceptance.json). JSON/CSV exports pass release and fresh host acceptance, with eight rows and thirteen fields confirmed by the actual v69 invocation in the [final receipt](../evidence/google-tasks-retirement/exports-activation.json). All R0/G0/G1/G2/G3/R1/A milestones are complete |

The Operator additionally requested removal of Hermes/SearXNG directories and
project Playwright artifacts. SPEC ADR 005 records that scoped amendment.
The November 6 Tasks amendment uses derived AM/PM title headers for visible times,
with Calendar companions for exact alerts (SPEC ADR 011).
Parent completion/deletion uses Operator-approved Google-style current-subtree
semantics without a recursive input flag or repeated prompt (SPEC ADR 014).
Older private registries/frozen sources remain unchanged and are not implicitly
imported into this forward index.

Live acceptance also identified valid zero-duration Google Calendar observations.
Their exact point times are preserved by readers without changing canonical event
creation rules or fabricating duration; source correction and actual postdeploy
promotion are tracked in the implementation report.
