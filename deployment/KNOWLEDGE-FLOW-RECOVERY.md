# Knowledge-flow recovery contract

This contract covers two separately reviewed changes: durable job outcomes and
stable evidence selection. It does not authorize deployment, migration or replay.
The implementation PRs record their checks and remaining release gates. No host
baseline is provided.

## Job outcomes

| Input or result | Required behavior |
| --- | --- |
| Capacity is unavailable | Preserve the complete input for a later attempt; create neither a human review nor a successful completion |
| Execution or source reading fails temporarily | Retry against the frozen input within the existing attempt bound; retain a visible failure after exhaustion |
| Output remains invalid after bounded correction | Retain a technical failure; do not repeatedly consume the same invalid model-stage cache |
| A decision conflict or user intent remains unresolved | Retain one current human-review item for that logical issue |
| Independent review accepts the update | Use the existing publication and replica protocol; execution completion alone does not prove visibility |
| Independent review confirms no durable change | Record an explained `empty` result |

Retries preserve the original issue relationship and all necessary evidence.
Historical attempts may accumulate; a valid linear review-retry chain consumes
one current review slot. Malformed identities, cycles and forks are not silently
folded. A failed attempt does not resolve its original human hold.

All intake paths share the runnable and waiting admission policy. A complete
unclaimed input may wait in the existing `capture-pending` store. A claimed batch
keeps its frozen files and basis; verified deferred source files count as waiting.
Runnable sources are limited to `maxQueuedJobs`, and admitted runnable plus
capacity-waiting sources to twice that limit. New waits also need a waiting slot.
Transfers preserve admitted inputs; resumptions must respect runnable capacity.
No available slot produces a visible admission failure with the full input.
These limits bound admitted work, not the complete audit or failure history.

## Evidence selection

Initial edits and corrections select exact `quoteId` values from one frozen
catalog. The program owns source text, source role and destination metadata.
Corrections use run-local stable claim IDs, with numeric publication references
restored only after the patch is applied. Publication formats do not change.

An accepted claim is immutable during correction. Other claims retain primary
and supporting references unless the independent reviewer explicitly permits
source replacement. Source replacement cannot raise authority. A permitted
replacement settles the claim's authority: the program applies the shape the new
source permits (an assistant quote carries only a historical lesson; an artifact
quote only historical non-decision material) and drops assistant supporting
quotes from a primary that is no longer the user; the complete result is still
reviewed independently. Missing or
ambiguous per-claim verdicts do not grant edit permission. Page text may be
rewritten, but the complete resulting diff still requires independent review.

The stable patch path replaces fuzzy text matching, longest-overlap quote
recovery and a separate quote-only correction path. It does not replace semantic
review or infer approval from an assistant report, artifact or unclear assent.
The editor gets at most one validator-driven and one reviewer-driven correction:
a draft that first failed program validation is corrected once, and the
reviewer's first verdict on the valid draft may then permit one more correction
before the rejection is final. A case previously repaired without an editor
call may need a correction call.

## Acceptance and release

- Repeated capacity rejection must resume after capacity is available, with
  original input intact and no fabricated successful result.
- Repeated human holds must not grow the number of current logical issues.
- Interruption at durable finalization boundaries must preserve accepted ledger
  evidence and recover without another model invocation.
- A blocked project and an eligible project must coexist within the admission
  bounds; malformed waiting records must not manufacture capacity.
- Paraphrasing, reordering or dropping one claim must not rebind neighboring or
  accepted claims. Actual source errors must remain repairable within permission.
- Short approvals require their original proposal; unresolved intent stays held.
- Compare source-derived expectations and normal controls. Structural reference
  checks and synthetic model checks do not establish real backlog recovery rates.
- Report model calls and observed cost limits, omissions and unresolved failures;
  moving errors between categories alone is not a health improvement.

Each PR needs repository checks and review of its committed HEAD. Before a
combined release, incorporate the chosen base, rerun affected and end-to-end
checks, and refresh review and CI evidence. Deployment and real-input recovery
require their own authorization and observable publication verification.
