# Basic Example

Real output from running llmwiki on a single source document about knowledge compilation.

## What's here

```
sources/
  knowledge-compilation.md    ← the input (one markdown file)

wiki/
  concepts/                   ← 7 concept pages extracted by the LLM
    change-detection.md
    compilation-pipeline.md
    concept-extraction.md
    cross-source-concepts.md
    incremental-compilation.md
    knowledge-compilation.md
    wikilinks.md
  index.md                    ← auto-generated table of contents
```

One source in, seven interlinked concept pages out. Browse the `wiki/` directory to see the compiled output, or open it in Obsidian for navigable `[[wikilinks]]`.

## Reproduce it yourself

```bash
# run from the repo root
llmwiki ingest examples/basic/sources/knowledge-compilation.md
llmwiki compile
llmwiki query "what is knowledge compilation?"
```

Output will vary since it's LLM-generated, but the structure will match.
