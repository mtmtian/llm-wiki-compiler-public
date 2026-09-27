# Knowledge Compilation

The idea of "knowledge compilation" is that LLMs can take messy, unstructured information and compile it into clean, structured, interlinked reference material. Think of it like a compiler for knowledge: raw sources in, organized wiki out.

## Why This Matters

Most knowledge lives in scattered documents, articles, notes, and conversations. Finding what you need means searching through all of it. A knowledge compiler processes these sources and produces a wiki where every concept has its own page, linked to related concepts.

## How It Works

The compilation pipeline has several stages:

1. **Ingestion**: Raw sources (URLs, files, documents) are collected into a sources directory.
2. **Change Detection**: SHA-256 hashes identify which sources have changed since the last compile.
3. **Concept Extraction**: An LLM reads each changed source and extracts the key concepts.
4. **Page Generation**: For each concept, the LLM generates a wiki page with proper structure.
5. **Interlink Resolution**: Concept mentions across pages are wrapped in [[wikilinks]].
6. **Index Generation**: A table of contents is built from all concept pages.

## Incremental Compilation

Like a code compiler, only changed sources need reprocessing. This saves both time and API costs. The system tracks source hashes in a state file and skips unchanged sources entirely.

## Cross-Source Concepts

When multiple sources discuss the same concept, the compiler detects this overlap through semantic dependency tracking. Changes to one source trigger recompilation of shared concepts using content from all contributing sources.

## Obsidian Compatibility

The output format uses YAML frontmatter and [[wikilinks]], making it directly compatible with Obsidian and similar tools. Each concept page includes metadata like title, summary, source attribution, and timestamps.
