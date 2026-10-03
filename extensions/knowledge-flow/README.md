# Optional Codex knowledge workflow

Shared protocol v2 lets each participant publish reviewed immutable records in its
own directory and rebuild a private compiled Wiki. See
[the shared deployment protocol](../../deployment/SHARED-KNOWLEDGE.md).
`intakeEnabled` controls local collection; `publishEnabled` enables independent v2
publication. Shared Obsidian projection has a separate single writer selected by
`exchange.materializerMachineId`, when a deployment has configured one in its private full config. The
committed template selects no materializer. A writer or
publisher that is not that machine can still publish and rebuild its private
replica, but cannot write shared pages; contributor and reader never write shared
pages. Omitting the materializer identity disables shared projection. Only
migration of legacy proposals uses a designated `legacyImporterMachineId`; native
v2 publication does not depend on that machine.

This personal-fork extension adds scoped retrieval and evidence-based task intake. It is built separately from the upstream CLI. Portable project ownership, business mappings, and model settings are versioned in this private repository under [`deployment/`](../../deployment/README.md). The installer expands machine paths; credentials, evidence queues, and runtime state remain local.

## What happens

1. `UserPromptSubmit` resolves the host event from its current `cwd`: a Git repository identity, a configured project path, or, in a non-repository directory, an explicit project topic in the prompt. The owners list, project mappings, `excludedRepos`, and `excludedPaths` come from the host's private configuration and control eligibility. Third-party, archived, unverified, and excluded repositories are not included for new intake or shared-proposal review; knowledge already published while an owned, non-fork repository was active stays readable in the local replica after that repository is archived. A `workingForks` entry only bypasses owner and fork/archived metadata checks, never `excludedRepos` or `excludedPaths`. Installed runtime/state directories and `llmwiki-codex-agent-*` worker directories (including descendants) stay excluded to prevent recursive collection. This is not a scan of all repositories or conversation history. Repository metadata expires after 24 hours; lookup failure denies automatic use.
2. An eligible turn retrieves accepted pages within the active scope: mapped project pages by default, or all accepted concepts after shared semantic activation. The compiler filters the page set before ranking and graph expansion. In semantic scope, a section from another project's page also needs at least two matched query terms and a local embedding hit of at least 0.6 on that section (broad lexical coverage when embeddings are unavailable), so a single generic word such as “配置” or “继续” cannot inject another business's decisions. The adapter injects at most three sourced excerpts and 2,400 characters, excluding stale, archived, or contradicted pages. Source projects and applicability remain explicit. Unchanged excerpts are suppressed within the session. An empty scope returns no context. Before those excerpts, the first prompt routed to a project in a session receives a deterministic digest of that project's decided decisions and constraints (`current_decisions.py`): only fully visible records from the local replica, grouped by decision object, newest first with dates, exact duplicates removed, at most 1,400 characters. Claims-only ledger records (publication version 3) are included, and a decided claim they validly supersede is left out; a ledger record with any invalid supersede reference is ignored as a whole (`ledger.py`). It is repeated only when the digest changes or after the context compacts, and no model runs to build it.
3. Synchronous `Stop` runs at the end of each assistant turn, without requiring task archival. Intake checks the current turn and quickly queues eligible visible user/assistant text and explicitly linked evidence. Routine business analysis can qualify without a special capture phrase; acknowledgements do not. No model runs inside the hook. Assistant analysis can support only a dated historical lesson, never a user decision, verified metric, constraint, or deployment fact. Files must be explicitly linked in the final message, be inside the task directory after symlink resolution, have an allowed text/code suffix, and fit size limits. Credential-like filenames are excluded and common credential patterns are redacted.
4. New jobs accumulate by native session and project. After five quiet minutes, thirty minutes of continuous eligible input, or an explicit consolidation request, Luna plans existing/new topic destinations and drafts complete pages with at most five evidence-backed claims. A separate Luna review checks the entire page diff, routing, citations, history, and authority. A saved reference Prompt is document content, not a user's adopted implementation plan.
5. Accepted claims become immutable v2 publications, then each machine's deterministic materializer rebuilds its private view from the frozen baseline and publications. Only the configured materializer also projects its managed derived pages and navigation into the shared Obsidian root. A contributor instead submits a legacy proposal for the designated migration machine to review. Noise is discarded. Conflicts and uncertainty stay outside automatic retrieval: the reviewer lists incompatible pages, and concurrently published incompatible variants are held by the materializer. Pending records cannot be blindly accepted through the resolution API.

This is a conservative pilot, not a guarantee that a model will recognize every semantic conflict. Quoted evidence, isolated review, bounded intake, and replayable publication make errors inspectable and recoverable. Rules do not rewrite themselves based on model confidence.

Native Claude capture permits directory changes within one physical Git worktree,
or, outside Git, within one configured project directory (`projects.*.paths`);
other folders such as a home directory must match exactly. Nested repositories,
other projects, other worktrees and symlink escapes remain separate. Native
session, prompt ancestry and final-message checks still apply. Long Codex rollouts
are scanned for identity, completion and visible messages instead of using only
the last bytes, so large tool output cannot hide a turn's start. The scan has byte,
time and retained-evidence limits; invalid or over-limit input remains an explicit
capture error rather than becoming partial evidence.

## Topic pages in shared v2

New session batches use whole-page `topicRevisions`. The session checkpoint keeps
the goal, alternatives, decisions, unresolved questions and topic associations in
a private summary, plus a bounded window of original evidence. The summary is
never a citation. One conversation can continue the next day; other conversations
can reuse the same topic within the active scope. `sessionConsolidation` configures `enabled`
(default true), `quietSeconds` (300), and `maxWaitSeconds` (1800). Batch/byte limits
remain active. Original batch audits retain evidence beyond the 40,000-byte
checkpoint window, without putting whole transcripts into the shared exchange. The window
is measured like the job byte limit (UTF-8 JSON), so prior evidence alone never fills a job.

Each model stage has a durable input-bound result. Finalization advances the
checkpoint once, after publication export succeeds. New messages arriving during
processing belong to the next batch. Existing frozen jobs and legacy submissions
retain their original processing contract. A corrupt checkpoint fails closed.

The captured `currentTaskContext` is passed unchanged from the job prompt to
planning, editing, and independent review, including a bounded correction. It
only scopes what the batch should address: it is not citable evidence, proof of
user approval or implementation, or an override of evidence, policy, authority,
or routing. Essential requested requirements must be supported by the original
evidence; if they cannot be represented safely, the batch remains held.

Each reviewed revision contains `pageId`, stable `topicId`, title/topic/object,
the complete previous page `basisHash` (null for creation), Markdown `body`, and
`claimIndexes`. Draft placeholders `{{claim:N}}` become exact quote citations.
Before validation, `quote-repair.ts` restores a claim quote that lost markdown, quote marks or spacing to the exact original span of its evidence, and rebinds a verbatim quote cited under the wrong evidence ID to the only evidence that contains it; paraphrases and stitched fragments stay for the validator. A correction receives `claimAnchors`, the quote options of each previous claim's own evidence. Because corrections still tended to pin every claim to one quote option, the corrected draft is also checked deterministically: a claim whose text is nearly the same as exactly one previous claim keeps that claim's evidence (its exact quote, or the option of the same evidence that overlaps it most); rewritten or ambiguous claims keep the correction's choice. Every evidence item shown to the planner, editor and reviewer is marked `origin: current` (the turns being consolidated) or `origin: earlier` (session context). The editor chooses each claim's primary quote first and restates only what that quote says, citing earlier evidence as primary only when it states the claim; the marker is prompt-only and never appears in published evidence.
The editor receives each existing page's `citationChecklist` and must keep or retire every listed marker; a new page may not contain `^[...]` markers. Before validation, `citation-repair.ts` deterministically restores provenance the draft visibly kept: it unwraps `^[{{claim:N}}]`, swaps a renumbered or merged marker back to the dropped original markers of the same file it covers, and gives a line kept verbatim its original markers back. Retired markers are never restored, and anything it cannot prove still reaches the strict citation validator; a correction is told which markers were dropped, invented, or retired outside the page's basis.
The independent reviewer can accept an explicit user change while preserving useful
prior rationale, constraints and counterexamples. It holds unresolved conflicts and uncertain
intent. Multi-turn approvals retain both original proposal and approval quotes.
The reviewer also returns a conclusion for each claim (`claimDecisions`); each review attempt's conclusions travel on the batch result as `claimReviews`, and a missing or incomplete list is recorded as incomplete rather than holding the batch. The page-level decision still decides whether a batch publishes or is held, with one refinement: when the final review rejects some claims but accepts others, `claim-pruning.ts` removes the lines citing the rejected claims (only when they carry no accepted claim and no existing citation, and drops a heading left empty by the removal), renumbers the rest, and the reduced draft is validated and reviewed again as stage `pruned` (a page whose claims were all rejected leaves the plan for that review and keeps its current text); it publishes only if that fresh review accepts it, and any other outcome holds the batch with the original reason plus the cause. When the knowledge-ledger gate is enabled (`deployment/KNOWLEDGE-LEDGER.md` §7), a held batch whose final review accepted some claims publishes exactly those claims, with only the evidence they cite, as one ledger record (version 3) and stays held; a closed gate, an unready reader or a contract violation publishes nothing and records `ledgerError`.
The editor and reviewer also receive original files cited by the prior page;
missing sources stop the batch for review. One bounded correction may address
review findings. Invalid routing or incomplete review coverage is held with the
frozen inputs and model stages available in the private audit.
Replica replay verifies the prior hash and holds concurrent whole-page variants.
It never asks a model to reinterpret the record on another machine. MOC navigation
groups readable topic titles by project until semantic activation, then by knowledge topic.
Navigation has no volatile generation timestamp. Reviewed migrations also remap
source ownership and frozen-page paths, so later compilation uses canonical pages.

### Semantic topic organization

An explicit `topicScope: "semantic"` revision may update a matching topic and decision
object across source projects. Existing page IDs, topic IDs, basis hashes, citations
and conflict checks remain intact. New identities omit the source project; titles
and filenames do not acquire a project prefix. Updated pages replace owner metadata
with `topicScope: semantic` and sorted `sourceProjectIds`. Publication `projectId`
still identifies evidence origin and controls admission, budgets and claim identity.
Model planning and independent review must preserve project-specific conditions and
conflicting conclusions instead of treating a local decision as a universal rule.

`get_knowledge_context` retrieves topic evidence across projects; optional `projectId`
describes the current task without restricting results. `get_project_context` keeps
its project filter and includes shared pages whose sources contain that project.
Explicit `allowedPageIds` remains a hard boundary in either mode.

All replica participants must install a runtime advertising `semantic-topic-revisions-v1`
and run `llmwiki-maintain --announce`. Then preview `llmwiki-maintain --semantic-topics enable`
and apply with `--apply`. The immutable exchange policy `v2/topic-scope.json` determines
effective scope on every upgraded host; a local configuration flag cannot enable it.
`llmwiki-maintain --semantic-topics status` reports current scope and missing capabilities.
Reader readiness is checked again before semantic processing/publication and active sync.
Old readers can omit unknown records, so their announcements must never be fabricated.

Runtimes that can read ledger records (publication version 3, `deployment/KNOWLEDGE-LEDGER.md` §7) also advertise `knowledge-ledger-v1`. Ledger records never become page text: replica sync accepts them, page replay and topic migration skip them, and only the current-decisions digest reads them. Producing them requires every participant to advertise this capability and one operator to write the shared policy `v2/knowledge-ledger.json`: preview with `llmwiki-maintain --knowledge-ledger enable`, apply with `--apply`, and inspect with `--knowledge-ledger status`. The ledger gate is independent of semantic topics and cannot be disabled once records exist, because readers must keep reading them.

Only newly captured session jobs receive this contract. Frozen older jobs and review
retries remain project scoped; the queue never mixes the two contracts. Semantic jobs
pin all accepted publication ancestry, preserving cross-project update ordering.
Activation rebuilds topic navigation without new records. It neither merges nor deletes
existing pages; each page gains source metadata on its first accepted semantic update.

All extraction, planning, editing and review stages share the
[durable knowledge policy](../../KNOWLEDGE-POLICY.md). Completed process records do
not become a second history archive. Optional `citationRetirements` name each exact
removed marker, its reason and surviving evidence or external record; the reviewer
must cover every retirement. Reviewed migration `retiredPages` can remove pure
process pages by exact hash and project. Replay prunes obsolete provenance and
uncited generated bundles, checks surviving Wiki links, and protects baseline
sources and human edits. Upgrade all participants before activating these fields;
older runtimes reject them and retain their old view.

The following append-segment behavior remains the compatibility path for old
immutable publications. A reviewed whole-vault migration can replace that legacy
projection with complete topic narratives.

An immutable publication is evidence, not a page boundary. Extraction first looks
for a same-project page with the same canonical topic and decision object. It sets
`targetPageId` for an existing match and supplies `decisionObject` for new claims.
The independent reviewer checks that routing as well as the evidence. Several
complementary claims from one publication can therefore contribute paragraphs to
one page; unrelated objects still produce separate pages.

Replay uses the reviewed target, or an exact normalized match of `projectId`,
`knowledgeTopic` and `knowledgeDecisionObject`. It never runs a model or guesses
semantic synonyms. Explicit targets may resolve older synonym labels; foreign,
missing, mismatched-object and ambiguous targets are held in replica conflicts.
A baseline page without `projectId` requires unique ownership in `projects.*.pages`.
New pages use a readable topic/object filename plus a stable identity suffix,
rather than one `record-*` filename per claim. Old publication target ids resolve
to their new topic destination during replay. Existing publications without
`decisionObject` remain readable under their original topics. Their cross-topic
targets were related-page hints and do not authorize merging different topics;
those contributions retain their own topic. This does not infer a semantic
migration or decision-object identity for legacy fragments.

Each accepted publication contributes one readable `sources/<project>-<date>-<id-prefix>.md`
quote bundle. Every paragraph keeps its own date, status, applicability, rationale
and exact line citation. A historical lesson does not change a decided page into
a historical page, or become a decision merely by sharing that page. Original
baseline prose is preserved. `knowledgePublicationRefs` prevents duplicate
paragraphs on identical replay; `knowledgeClaimIds` retains claim provenance.
The 12,000-character review budget also bounds a topic page: overflow fails the
staged generation for review, retaining the previous active generation.
Materialization writes only selected pages and rebuilds navigation/embeddings;
it does not run the normal approval command's whole-wiki link rewriting. The
local `.llmwiki/materialization-input.json` pins a stage's record set and project
ownership. Identical replay is supported; changed inputs require rebuilding
from the frozen baseline so retracted or conflicting content cannot linger.
The replica cache also includes project page ownership in its generation identity;
changing that mapping rebuilds the view even when publications are unchanged.
Determinism here covers topic pages and evidence sources. Operational index
timestamps and compiler activity state retain their normal generation times.

Only the designated shared materializer can project an updated baseline concept
page. Its first update requires the original baseline hash, later updates require
the ownership hash, and contribution retraction restores baseline prose rather
than deleting the baseline page. Baseline source files remain immutable. Human
edits block the shared batch before any file is changed. Unowned legacy fragments
still require an explicit, hash-checked migration with old-link compatibility;
upgrading the code alone does not migrate or delete the live vault.

### Reviewed legacy grouping

For a reviewed historical migration, place an optional `v2/topic-routes.json` in
the exchange. Its envelope is `{version: 1, baselineId, reviewedAt, groups}`;
each group has `projectId`, `topic`, `decisionObject`, and a nonempty `claimRefs`
array of `<full-publication-id>:<claim-index>`. Keep this business-specific file
outside Git. The shared exchange's write permission is its trust boundary.

The manifest can group only existing legacy claims without `decisionObject`.
Every reference must be unique and belong to the original project and baseline.
Unknown records, invalid schema, foreign projects, or symlinks fail the sync while
retaining the previous active view. Limits are 256 KiB, 1,000 groups and 5,000 refs.
Sync reads one snapshot for both its generation identity and worker input.
Reordering groups or references does not change the resulting content.

The manifest overrides old related-page hints only for its exact members; it
does not edit publication text, evidence, claim ids, or dates. Members of the
same group have been jointly reviewed and may coexist even without a causal
basis. New unobserved concurrent variants still require review. Removing the
manifest restores legacy routing on the next rebuild, so retain it after migration.

`deployment/migrate-shared.py --config <private-config> --plan <private-plan>`
preflights a private `{baselineId, files: {relativePath: sha256}}` inventory.
Only the enabled designated materializer can apply it with `--apply`. Stop old
shared writers and build/verify the new private generation first. The inventory
must cover all unowned generated legacy pages/sources and any replaced navigation.
The command reuses the normal durable projection transaction: prior files move
to hidden `.llmwiki-preserved-*.bak` backups and new pages enter normal navigation.
It does not rewrite hand-maintained inbound links; review their old-to-new mapping
as part of the migration. Once ownership exists, use normal sync for subsequent
updates or recovery rather than adopting the vault again.

## Routing non-repository work

`projects` maps stable IDs to `repos`, `paths`, `aliases`, `topicTerms`, and existing `pages`. Platform-specific domains can additionally require one of `requiredTerms` for initial topic binding. A path matches its descendants, with the longest match winning. Temporary chat directories should not be globally assigned to a business. In those directories, both an explicit business alias and a matching task term are required for initial binding. Short continuations and matching business follow-ups reuse the binding; an explicit platform switch changes domains. Unrelated or ambiguous multi-domain prompts receive no Wiki context. A third-party Git repository is excluded even when its prompt mentions an allowed business.

At `Stop`, a turn that was unbound at prompt submission gets a second routing pass over the validated, current-turn visible user and assistant messages. A repository path, configured repository name, GitHub repository/PR/issue/tree/blob/commit URL, or explicit business alias plus task/platform terms can establish one project. User and assistant messages can supply complementary business terms. The same ownership, path, and ambiguity checks still apply. Explicit exclusions, foreign/unverified repositories, general questions, and ambiguous prompt routes are never overridden. Successful late routing persists a session binding for supported follow-ups; it cannot retroactively inject context into the completed turn. Unmatched conversation text is not persisted in a new queue or session binding.

This pass uses no model and reads only the host-named bounded transcript (or complete host-supplied visible evidence). Prior turns, tool payloads, and hidden assistant phases cannot establish a route. A visible assistant repository hint establishes a topic, not proof of execution. If a repository appears only inside a tool's raw JavaScript, it is not inferred: the host has no structured execution-directory evidence for nested calls. Include the repository in the visible task/result to make that association explicit. GitHub page URLs are normalized to a strict repository identity before checking ownership.

Owned non-fork repositories without an explicit project mapping receive a stable `repo-owner-name` ID automatically. They initially have no Wiki context; accepted new pages become retrievable afterward. Their display label uses the verified repository name. Synced pages are discovered by frontmatter `projectId`; legacy filename matching is used only when ownership metadata is absent.

## Build and host installation

Run from a tested checkout with locked dependencies:

```sh
npm ci
npx tsc --noEmit -p extensions/knowledge-flow/tsconfig.json
python3 -m unittest discover -s extensions/knowledge-flow -p 'test_*.py'
node extensions/knowledge-flow/build.mjs /absolute/runtime/knowledge-flow
```

The built worker needs the compiler's pinned production dependencies in an ancestor `node_modules` directory. `build.mjs` copies the Python adapter, queue/reconcile worker (`capture.py`, `queue_worker.py`, `wake.py`), and maintenance command. Keep the runtime immutable and switch private configuration only after validation.

The private JSON configuration requires `version: 1`, `enabled`, absolute `wikiRoot`, `stateDir`, `node`, `worker`, and the routing tables. For v2, `exchange.materializerMachineId` is the one allowed shared-page writer; an absent value disables that projection. Model default for this installation is `gpt-5.6-luna`; its reasoning effort uses the Codex model default. Limits are `maxProposals: 5`, `maxPendingPerProject: 10`, `maxQueuedJobs: 30`, `maxJobBytes: 120000`, `maxProcessEventBytes: 600000`, and `maxDailyJobs: 300` model-processing attempts per UTC day. The byte limits are measured on UTF-8 serialized JSON, including the config/envelope overhead sent to Node. Oversized jobs are isolated before model invocation. The daily budget counts model-processing attempts, not sessions or queued jobs. Deferred jobs remain queued. When a project already holds `maxPendingPerProject` reviews, its unclaimed work waits in the queue before any batch claim, replica pin, model call or budget use (wake reason `review-queue-full`) while waiting work occupies at most half of `maxQueuedJobs`; already claimed batches continue so their results can finalize; beyond that the batch is recorded as a `review queue is full` hold, which `--retry-review` can reprocess once the project has room. Three failed attempts move a job to `failed/` for diagnosis instead of indefinitely blocking the queue. A saved result whose own content fails the publication contract (for example, one drafted before the current evidence-role rules) is not retried, because the same result would fail every time: the batch completes as a `needs_review` hold whose review names the violation, the rejected result stays in the batch audit, and `--retry-review` drafts it again under the current contract. Other publication failures keep their bounded backoff. If a multi-turn batch is interrupted after one source reaches a terminal state, the audit remains the authoritative frozen basis and the remaining sources are quarantined as `batch-failed` without a new model invocation.

Append the following command to the existing global hooks, preserving all unrelated entries:

```text
/absolute/python3 /absolute/runtime/knowledge-flow/hooks.py --config /absolute/private/knowledge-flow.json
```

Use synchronous `UserPromptSubmit` and `Stop` command hooks. `UserPromptSubmit` has an 8-second timeout and `additionalContextLimit: 1500`; the shared `Stop` path only performs bounded, quick enqueue even though its host timeout remains 600 seconds. Both produce JSON, and Stop produces no conversation text. The event worker consumes queued jobs on signals and during its 5-minute reconcile; idle runs do not invoke a model. The compiler's own ephemeral sessions are excluded to prevent recursion. Set `enabled: false` to disable the integration. Set `intakeEnabled: false` to retain retrieval while disabling Stop intake and queue draining. New installations default to reader; explicit `--writer` or `--publisher` enables independent v2 publication. A v2 contributor requires a declared legacy importer; omitted `legacyImporterMachineId` disables legacy imports on v2 writers.

**Codex requires trust for new or changed hook definitions through `/hooks`.** Installing JSON does not establish that the host has fired a hook. Validate actual events after native trust; direct adapter tests only establish the adapter behavior.

## Alma host adapter

Alma currently has no supported native hook registration surface. `deployment/alma-session.py` therefore reads a thread through Alma's paginated `alma thread messages --full --json` CLI protocol, keeps all visible user/assistant text with hashes, and submits one synthetic Stop event to this same adapter. It reuses the original route, queue lock, budgets, extraction, independent review, and compiler publication path. It never creates a parallel queue and it cannot retroactively inject context into an Alma turn; context must be obtained by an explicit `llmwiki` MCP call. A successful bridge process only proves that the request was parsed/submitted, so inspect the resulting state/audit record and Wiki evidence before claiming intake succeeded.

## Maintenance and review

The event worker is prepared explicitly with `deployment/install.py --event-driven` and activated with `deployment/install.py --event-worker-action enable`. Its v2 LaunchAgent watches `stateDir/queue`, the publication root and each participant directory, and the baseline. V1 uses publisher submission directories or contributor receipts. `RunAtLoad` plus a 5-minute Python reconcile recovers missed events and sleep; it also imports legacy proposals on the designated v2 migration machine. Idle runs do not call a model. There is no `KeepAlive` or `QueueDirectories`. Actionable review, failure, exchange, and replica error counts may trigger one deduplicated native notification; notification delivery depends on macOS permission and does not include business text. A shared projection conflict leaves the private `replica/current` usable, records `replica-errors/shared-materialization.json`, and makes `--check` fail until the next retry succeeds. Use the maintenance command for manual diagnosis or recovery:

```text
/absolute/python3 /absolute/runtime/knowledge-flow/maintenance.py --config /absolute/private/knowledge-flow.json --drain --check
```

Maintenance drains up to three jobs within the daily budget, checks configured routing cases and empty-scope isolation without calling a model, and writes `maintenance.json`. It lists unresolved review records and operational failures. Hook prompts and session caches expire after seven days; narrow accepted sources and audit history are retained. A human or agent can inspect the cited evidence in each review record. Once the user explicitly resolves a genuine conflict, the agent should apply that decision through the compiler's normal manual candidate-review path, then archive the pending record with `--resolve JOB_ID --action dismiss`. A rejected proposal uses `--action reject`. These actions preserve the review and disposition under `resolved/`. Re-running the automatic pipeline does not bypass its conflict gate. Never auto-approve merely because a record is old.

Already-published legacy records need a different recovery when a reviewed whole-page
revision has replaced their claims. After checking every old claim, its original evidence,
and the accepted replacement, retain that decision in the shared exchange's optional
`v2/publication-resolutions.json`:

```json
{
  "version": 1,
  "baselineId": "<full baseline SHA-256>",
  "reviewedAt": "2026-09-26T00:00:00Z",
  "resolutions": [{
    "recordId": "<full legacy publication id>",
    "coveredBy": "<full accepted whole-page publication id>",
    "claimMappings": [{"claimIndex": 0, "coveredByIndexes": [0]}],
    "reason": "Every original claim and its evidence were reviewed against the replacement."
  }]
}
```

The mapping must cover every old claim, stay within the same source project and target
page, and identify a whole-page revision. Shared-directory write permission is the
review trust boundary, as with `topic-routes.json`; structural checks do not establish
semantic equivalence. Normal sync rechecks that the replacement is fully published and
its mapped references are still present in the current page. Only then does it move the
old diagnostic to `resolvedConflicts`, keeping the original reason and review mapping.
Missing or held replacements and retired references keep the conflict active.
Because iCloud files can arrive out of order, a structurally valid receipt whose
publication packets have not both arrived stays inactive while normal sync continues.
The next sync validates the complete record pair once it becomes available; malformed
receipt structure or a mismatched baseline still fails closed.

This receipt never changes pages, publication packets, frozen bases, or the raw replay
result. Resolved legacy records remain outside `fullyVisibleRecordIds`; they do not
become new evidence for queued jobs. Removing the receipt restores the diagnostic on
the next sync. Older runtimes ignore this diagnostic receipt and continue reporting
the original conflict, so update each participant to obtain matching status reports.

The useful improvement loop is task → evidence → reviewed knowledge → later retrieval → observed correction. Routing regressions and errors are surfaced for targeted fixes. Semantic quality still needs periodic checks against actual decisions; successful structural tests do not prove semantic correctness.

## Synchronization and limits

Sync the immutable baseline and publications through the chosen file service. Each machine needs its executable, authentication, generated path configuration, and trusted hooks. Queues, credentials, and machine-specific paths are local. Stable project IDs must match between machines. V2 never shares compiler state or relies on a distributed lock; a local `flock` cannot coordinate two Macs. Only the configured materializer writes shared derived pages, and changing it requires stopping the old shared generator, waiting for iCloud synchronization, and updating every participant before enabling the new one. Only legacy proposal migration is restricted to one declared importer; changing it requires stopping the old importer, settling its pending work, and updating every participant before enabling the new one. See [the deployment migration procedure](../../deployment/README.md#共享-materializer-的切换与旧页迁移) for ownership-manifest and legacy-page handling.

Generation cleanup retains current, previous, and active batch references. A new batch persists its reference under the replica sync lock, so cleanup cannot remove its review basis. Model retry reuses that basis; export retry reuses the saved model result. Oversized baseline snapshots are rejected before any immutable write, and quarantined jobs cannot be revived by replaying the same Stop event.

Scope filtering is an optional retrieval feature, not a security ACL for every compiler API. The adapter uses the scoped context path; manual broad search or direct write commands retain their existing behavior. Background output cannot erase unrelated context already present in a conversation, and this integration does not modify other installed hooks or the host's separate memory system.
