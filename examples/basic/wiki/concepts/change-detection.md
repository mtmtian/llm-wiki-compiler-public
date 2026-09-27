---
title: Change Detection
summary: Using SHA-256 hashes to identify which sources have been modified since the last compilation run
sources:
  - knowledge-compilation.md
createdAt: "2026-04-06T07:53:56.456Z"
updatedAt: "2026-04-06T07:53:56.456Z"
---

# Change Detection

Change Detection is a crucial component of the [[knowledge compilation]] pipeline that determines which sources need reprocessing during [[Incremental Compilation]]. It uses cryptographic hashing to efficiently identify modified content and avoid unnecessary work.

## Purpose

Change Detection serves as an optimization mechanism for [[knowledge compilation]] systems. Rather than reprocessing all sources on every compilation run, it identifies only the sources that have actually changed since the last compilation. This approach saves both processing time and API costs when working with large knowledge bases.

## How It Works

The system uses **SHA-256 hashes** to create unique fingerprints for each source document. These hashes are stored in a state file that tracks the compilation history. During each compilation run:

1. The system calculates SHA-256 hashes for all current sources
2. These new hashes are compared against the stored hashes from the previous compilation
3. Sources with different hashes are marked as changed and queued for reprocessing
4. Unchanged sources are skipped entirely

## Integration with [[Compilation Pipeline]]

Change Detection operates as the second stage of the [[knowledge compilation]] pipeline, immediately after **Ingestion**. It acts as a filter that determines which sources proceed to the **Concept Extraction** stage, ensuring that only modified content triggers expensive LLM processing.

## State Management

The system maintains compilation state through a dedicated state file that preserves hash information between runs. This persistent tracking enables true [[Incremental Compilation]], where the system can resume work efficiently after interruptions or when processing sources that are updated at different intervals.

## Cross-Source Dependencies

When sources share concepts, Change Detection must account for semantic dependencies. If one source changes and affects a concept that appears in multiple sources, the system may need to recompile pages for that concept using content from all contributing sources, not just the changed one.

## Sources

- knowledge-compilation.md
