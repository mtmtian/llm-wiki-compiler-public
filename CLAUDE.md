# llmwiki

A knowledge compiler CLI. Raw sources in, interlinked wiki out.

## Development Guidelines

### Code Style & Standards

- Code files (TypeScript, Python, JavaScript, shell, SQL) must be smaller than 400 lines excluding comments. Once 400 is exceeded, initiate a refactor. **Markdown and other prose/config files (`.md`, `.mdx`, `.yaml`, `.json`, `.toml`) are exempt** — long planning documents, roadmaps, and reference catalogs are allowed to grow as substance requires.
- Functions must be smaller than 40 lines excluding comments and the catch/finally blocks of try/catch sections. If a function exceeds that, refactor it.
- test files must be smaller than 400 lines excluding comments. Once 400 is exceeded, split the tests into multiple files.
- tests must be smaller than 40 lines excluding comments and the catch/finally blocks of try/catch sections. If a test exceeds that, refactor it, or separate it into multiple tests.


### clean code rules

- Meaningful Names: Name variables and functions to reveal their purpose, not just their value.
- One Function, One Responsibility: Functions should do one thing.
- Avoid Magic Numbers: Replace hard-code values with named constants to give them meaning.
- Use Descriptive Booleans: Boolean names should state a condition, not just its value.
- Keep Code DRY: Duplicate code means duplicate bugs. Try and reuse logic where it makes sense.
- Avoid Deep Nesting: Flatten your code flow to improve clarity and reduce cognitive load.
- Comment Why, Not What: Explain the intention behind your code, not the obvious mechanics.
- Limit Function Arguments: Too many parameters confuse. Group related data into objects.
- Code Should Be Self-Explanatory: Well-written code needs fewer comments because it reads like a story.

### Comments and Documentation

- include a substantial JSDoc comment at the top of each file. For python files, use google style docstrings
- Write clear comments for complex logic
- Document public APIs and functions
- Use JSDoc comments for functions
- Keep comments up-to-date with code changes
- Document any non-obvious behavior

### Pre-Commit Checks

Before committing any work, and before considering any task complete, you must:

1. `npx tsc --noEmit` — type-check passes
2. `npm run build` — build succeeds
3. `npm test` — all tests pass
4. `fallow` — run the fallow codebase health analyzer. Fix all issues it reports (dead code, duplication, complexity). Use `fallow fix --dry-run` to preview auto-fixes, then `fallow fix --yes` to apply. Fix any remaining issues manually. Do not commit until fallow reports no issues.

### PR, Review, and Merge

Follow [AGENTS.md — PR, Review, and Merge](AGENTS.md#pr-review-and-merge-all-machines)
and the shared [CI.md workflow](CI.md#4-两台机器共用的-pr-与合并流程) on every machine:
create the PR, run `thermo-nuclear-code-quality-review` on the final committed
HEAD/base, wait for its automatic CI, then use `npm run pr:merge` with those
reviewed SHAs and existing merge authorization. Refresh review and CI when the
code or base changes. Do not bypass the shared command or infer merge permission
from a green check.

## General Rules

- This is a public repository. Code, docs, test fixtures, commit messages, PR titles and bodies, and review records must not contain private business information: real business project names or paths, real machine IDs, host metrics or runtime snapshots, user decisions, or business goals. Use neutral or synthetic examples and write "no baseline provided" instead of real numbers; real data stays in private host state outside Git. Check the diff and PR text for it before pushing. See [deployment/PUBLIC-REPOSITORY.md](deployment/PUBLIC-REPOSITORY.md).

- Wiki extraction and curation follow [KNOWLEDGE-POLICY.md](KNOWLEDGE-POLICY.md): retain durable knowledge and necessary evidence, and retire reviewed redundant process records without creating history archives.

- First think through the problem, read the codebase for relevant files.
- Make every task and code change you do as simple as possible. We want to avoid making any massive or complex changes. Every change should impact as little code as possible. Everything is about simplicity.
- Never speculate about code you have not opened. If the user references a specific file, you MUST read the file before answering. Make sure to investigate and read relevant files BEFORE answering questions about the codebase. Never make any claims about code before investigating unless you are certain of the correct answer - give grounded and hallucination-free answers.
