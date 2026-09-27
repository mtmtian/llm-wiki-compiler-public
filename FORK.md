# Self-maintained repository and releases

This repository keeps a small, reviewable set of changes on top of the upstream
llmwiki compiler. It contains source code and reusable deployment tooling.
Project knowledge, full routing policy, machine identity, credentials, queues,
transcripts, and runtime state belong outside Git. Do not include private host
configuration in upstream contributions.

## Repository and runtime

- `upstream` refers to `https://github.com/atomicstrata/llm-wiki-compiler.git`.
- `origin` refers to the self-maintained repository used for development and release.
- Use the repository's configured maintained branch as the target for reviewed
  changes. Keep any upstream mirror fast-forward-only if the repository uses one.
- Build runtime installations from a reviewed commit into a separate local
  directory. Development rebuilds must not replace a running MCP installation.
- Copy the tested `dist/`, package manifest, lockfile, and license into that
  runtime, then run `npm ci --omit=dev --ignore-scripts`. Record the commit and
  file hashes in a local build manifest. An install from unpinned dependency
  ranges alone does not reproduce the dependency set just tested.
- Each host keeps its own runtime and model configuration. For a shared vault,
  each writer or publisher may publish an immutable record and rebuild private
  state. Only the configured `exchange.materializerMachineId` may write shared
  derived Obsidian pages. The committed `deployment/knowledge-flow.json` does
  not choose participants or a materializer.

The npm package version remains the upstream version. Use the source commit and
local build manifest to distinguish a self-maintained runtime from an upstream
package.

## Current patch set

The Codex provider normalizes structured-output schemas before submitting them
to the CLI. This replaces an installation-directory hotfix that an npm upgrade
would overwrite. It retains validation against the caller's original schema.

Optional declared fields become required on the wire, keeping their original
types. This is a restricted subset of the original schema, not a promise of
support for every JSON Schema construct or free-form dictionary. If upstream
introduces an equivalent fix, verify it and remove this patch.

The viewer packaging test accepts both the earlier array report and npm 12's
package-name-keyed report. It still checks the same required runtime assets;
this is a test-runner compatibility change, not a compiler behavior change.

## Knowledge workflow boundary

The optional implementation lives in `extensions/knowledge-flow/`; see its
README for deployment, scope rules, limits, and host trust requirements. Core
changes add `allowedPageIds` to context retrieval and expose the existing locked
approval helper. Shared deployment defaults live in `deployment/`; each host
provides project mappings and stable identity through private files outside Git.

1. At project task start, retrieve a short project overview and a bounded set of
   relevant accepted pages. Historical snapshots do not establish current state.
2. At assistant turn completion, propose zero to five durable changes when the
   turn contains substantive evidence. Prompt-unbound turns may resolve a project
   from the current visible conversation; archival is not a trigger. Explicit
   decisions, verified constraints, and corrections qualify; routine summaries
   do not. Update an existing record before creating another page.
3. Keep intake proposals outside `sources/` and `wiki/`. Check source evidence,
   date, scope, status, duplicates, and contradictions before compilation. Bound
   the queue and stop intake when it is full.
4. Review exact supporting evidence and existing scoped pages in a separate
   model call. Promote accepted content through the locked review path; only
   conflicts and uncertain claims require operator clarification.
5. Corrections and later outcomes drive revisions with evidence and prior
   decisions retained. Generated summaries are not independent sources.

Keep host triggers and project policy in the workflow layer. Reuse the
compiler's CLI/SDK, review candidates, and source references. Change compiler
behavior only when a reproducible test shows an API or behavior gap. Verify
actual host event delivery after trusting hooks; `watch` is not a substitute for
those checks.

When a vault is synchronized between machines, configure exactly one background
materializer. iCloud is not a distributed lock. A writer or publisher that is
not the materializer may publish its own immutable records and rebuild private
state but never writes shared derived pages. A contributor only submits
proposals; a reader only rebuilds private state. Neither role writes shared
derived pages. Example IDs such as `peer-a` and `peer-b` in fixtures are fake;
real machine IDs belong only in external private configuration.

If `exchange.materializerMachineId` is absent, shared projection is disabled.
The designated materializer retries projection on every replica sync, including
a sync whose record digest did not change. A shared-file conflict is recorded in
private `replica-errors/shared-materialization.json`; the already-built local
`current` view remains usable while `--check` reports failure. Ownership hashes
live in private `stateDir/shared-materialization.json`, with an interruptible
plan in `stateDir/shared-materialization-pending.json`. Replaced or withdrawn
files are moved beside their target to hidden `.llmwiki-preserved-*.bak`
backups. They are not Markdown and are never collected or deleted automatically.
Human edits are reported as conflicts and are never overwritten.

Pages produced by an older runtime without ownership metadata may need manual
migration review before enabling a materializer. Do not claim automatic
compatibility with unowned files.

## Integrating an upstream release

The public repository starts from an independent source snapshot. It does not
share Git ancestry with the upstream repository or any earlier private checkout.
The upstream source baseline is release `v1.3.0`, commit
`34ca1df97b3e60a6700048c48c7cf70c92a9bfdb`. Preserve the upstream license and
attribution. Review changes between that public baseline and a selected upstream
release, then port the needed changes on an integration branch:

```sh
git fetch upstream --tags
git switch personal/stable
git switch -c integration/upstream-VERSION
git diff v1.3.0 UPSTREAM_RELEASE_TAG --stat
# Review and port the selected upstream changes before running these checks.
npm ci --ignore-scripts
npx tsc --noEmit
npm run build
npm test
npx tsc --noEmit -p extensions/knowledge-flow/tsconfig.json
python3 -m unittest discover -s extensions/knowledge-flow -p 'test_*.py'
npx fallow
```

Replace the version placeholders. Update this baseline after a reviewed upstream
integration. Inspect changes to data formats, review policy, provider arguments,
and retrieval. Import source changes through PRs; importing old private branches
or merging unrelated histories would also import their historical contents.
Real Codex smoke tests require the explicit `RUN_CODEX_LIVE_SMOKE=1` opt-in;
before a runtime upgrade, separately verify structured output with the host's
configured model when that live validation is authorized.

Resolve conflicts by behavior, not by retaining one side wholesale. Remove
patches now covered upstream. Test on a disposable copy of a wiki, including
review isolation, citation retrieval, stale decisions, and bounded context.
After checks and review pass, advance the maintained branch, build a new
versioned runtime, and switch the host-local launcher. Preserve the previous
runtime and pre-migration vault snapshot for rollback. Never merge and deploy an
upstream update in the same unattended operation.

If the repository also keeps an upstream mirror branch, advance it separately
with a fast-forward-only update. Do not force-push shared history.
