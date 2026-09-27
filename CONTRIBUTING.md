# Contributing to llmwiki

Thanks for your interest in contributing! This guide covers the fork-and-PR workflow we use for all contributions.

## Getting Started

1. **Fork** the repository on GitHub
2. **Clone** your fork locally:
   ```bash
   git clone https://github.com/<your-username>/llm-wiki-compiler.git
   cd llm-wiki-compiler
   ```
3. **Install dependencies:**
   ```bash
   npm install
   ```
4. **Create a feature branch:**
   ```bash
   git checkout -b feature/<your-feature-name>
   ```

## Branch Naming

- `feature/<name>` — new features
- `fix/<name>` — bug fixes
- `codex/<name>` — agent-assisted changes

## Development

### Build and Test

```bash
npm run build    # Compile TypeScript
npm test         # Run all tests
npm run dev      # Watch mode for development
```

**Automatic checks via git hooks:**

After `npm install`, husky wires up hooks that run automatically:

- **pre-commit** — `npm run fallow:ci` (strict codebase health) and `npx tsc --noEmit` (type check)
- **pre-push** — `npm run build` and `npm test`

The pre-push hook clears Git's repository-specific environment variables before
tests create temporary repositories, preserving the checkout's HEAD and index.

If a hook fails, fix the underlying issue rather than bypassing with `--no-verify`. Use `fallow fix --yes` to auto-fix unused exports, then address remaining issues manually.

You can also run the full suite manually:

```bash
npx tsc --noEmit   # Type-check
npm run typecheck:tests # Type-check tests against the per-file baseline
npm run build       # Build
npm test            # Tests
npx fallow          # Codebase health (dead code, duplication, complexity)
npm run fallow:ci   # Full-tree analysis with the lockfile-pinned Fallow, as in CI
```

**A note on `npm run fallow:ci`:** CI and local checks analyze the full checkout,
using Fallow from `npm ci`. The check does not fetch or infer a base branch, so
the personal fork is checked even when it differs from upstream `main`.

There is also one known parity gap that no flag closes: fallow's clone-detection occasionally returns different results across platforms (CI Linux x64 vs macOS arm64). When CI flags a clone you can't reproduce locally, dedupe by intent and re-push — it's not a bug in your branch.

### Code Style

- Follow the conventions in `CLAUDE.md`
- **File size limit:** 400 lines (excluding comments). Refactor if exceeded.
- **Function size limit:** 40 lines (excluding comments and catch/finally blocks).
- Use TypeScript with proper types — avoid `any`.
- Include JSDoc comments on all exported functions and at the top of each file.
- Write meaningful variable and function names that reveal purpose.

### Writing Tests

- Place tests in the `test/` directory
- Use Vitest (already configured)
- Tests should not depend on timing or external services
- Keep test files under 400 lines; split if needed

### Test Type-checking

Vitest runs tests but does not check their TypeScript types. Run
`npm run typecheck:tests` as well. It checks nested tests, fixtures, and imported
production code without emitting JavaScript or declarations. TypeScript is
pinned so the recorded diagnostics are comparable across machines.

The existing backlog is recorded per file in `test-typecheck-baseline.json`.
New errors fail, including errors in previously clean files; reducing errors in
another file does not provide an allowance. When you fix an existing error, run
`npm run typecheck:tests:update` and include the reduced baseline in your PR.
That command refuses increases. To see diagnostic locations and messages, run
`npx tsc -p tsconfig.test.json`; this raw command also reports the known backlog.

The `static-checks` CI job compares the proposed baseline with the PR's base
commit, so editing an allowance upward cannot hide a new failure. To reproduce
that comparison locally, run `npm run typecheck:tests -- --base-ref origin/personal/stable`
(use the actual target branch for other forks). Compiler-version or configuration changes need
an explicit baseline migration and review; routine updates do not accept them.
`--init` is only for initial baseline creation, not for clearing failures.

### CI Gate

The single `CI Gate` check waits for source/extension/test type checks, release
documentation, builds, all Linux and macOS test jobs, and full-tree Fallow.
Failures, skipped jobs, and cancellations cannot pass the final gate. It runs
on pull requests and pushes to `personal/stable` and `main`, plus merge queues
and manual dispatches. The separate test-typecheck workflow is consolidated
into this workflow so the final gate covers its result too.
Manual dispatches produce `CI Gate (manual)` because their comparison baseline
is configurable; only the automatic `CI Gate` should be required for merging.

See [CI standards and merge workflow](CI.md) for commands, business invariants,
the shared merge guard, and release checks that require real machines.
This private fork uses Actions plus `npm run pr:merge` on its current GitHub
plan. Native branch protection is optional when the plan supports it; the local
guard only enforces checks for callers that use it.

Ordinary tests use fake model providers. The real Codex subscription smoke
test is opt-in and is not a required PR check. Run it only when real model
access is intended:

```bash
RUN_CODEX_LIVE_SMOKE=1 npm test -- test/provider-codex-agent-live.test.ts
```

## Submitting a Pull Request

1. Push your branch to your fork
2. Open a PR against `personal/stable` on this personal fork (`main` upstream)
3. In your PR description:
   - Describe **what** the change does and **why**
   - Reference the issue number if applicable (e.g., "Closes #3")
   - Include instructions on how to test the change
4. Follow the review and merge procedure below; creating the PR is not approval to merge.

## Review Process

1. After creating the PR, run `thermo-nuclear-code-quality-review` on its committed
   HEAD against the target base. Record the reviewer, full HEAD/base SHAs,
   conclusion, findings, and validation evidence in the PR description or a
   linked review. Both machines follow this repository rule.
2. Resolve blockers, commit fixes, rerun affected checks, and refresh the final
   review. If the target branch advances, incorporate it into the PR branch and
   refresh review and CI evidence for that combination.
3. Wait for the latest automatic PR CI run and every job, including `CI Gate`,
   to succeed. Historical or manual green runs do not qualify.
4. An authorized maintainer uses `npm run pr:merge` with the recorded review SHAs.
   It creates a merge commit, preserving the reviewed commits. It does not use
   admin bypass, delete branches, or turn on auto-merge.

The exact command, failure handling, and concurrency limits are documented in
[CI.md](CI.md#4-两台机器共用的-pr-与合并流程). Do not merge through an alternative
entry point to bypass a refusal from the guard.

## Questions?

Open an issue or start a discussion — we're happy to help.
