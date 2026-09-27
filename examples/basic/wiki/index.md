# Knowledge Wiki

## Concepts

- **[[Change Detection]]** — Using SHA-256 hashes to identify which sources have been modified since the last compilation run
- **[[Compilation Pipeline]]** — A multi-stage process including ingestion, change detection, concept extraction, page generation, interlink resolution, and index generation
- **[[Concept Extraction]]** — The process where an LLM analyzes sources to identify and extract key concepts worth documenting
- **[[Cross-Source Concepts]]** — Concepts that appear in multiple sources, tracked through semantic dependency analysis to trigger recompilation when any contributing source changes
- **[[Incremental Compilation]]** — A technique that only reprocesses changed sources to save time and API costs, tracked through SHA-256 hashes
- **[[Knowledge Compilation]]** — The process of using LLMs to transform messy, unstructured information into clean, structured, interlinked reference material
- **[[Wikilinks]]** — A linking format using double brackets to connect related concepts across wiki pages, compatible with tools like Obsidian

_7 pages | Generated 2026-04-06T07:54:06.985Z_
