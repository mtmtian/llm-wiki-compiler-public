---
title: Compilation Pipeline
summary: A multi-stage process including ingestion, change detection, concept extraction, page generation, interlink resolution, and index generation
sources:
  - knowledge-compilation.md
createdAt: "2026-04-06T07:53:56.984Z"
updatedAt: "2026-04-06T07:53:56.984Z"
---

# Compilation Pipeline

The compilation pipeline is the core process in [[knowledge compilation]] that transforms raw, unstructured information into organized, interlinked wiki pages. It operates like a traditional code compiler but for knowledge sources, processing documents through multiple stages to produce structured reference material.

## Pipeline Stages

The compilation pipeline consists of six main stages that process sources sequentially:

### 1. Ingestion
Raw sources including URLs, files, and documents are collected into a designated sources directory. This stage establishes the input corpus for compilation.

### 2. [[Change Detection]]
The system uses SHA-256 hashes to identify which sources have been modified since the last compilation run. This enables incremental processing by tracking source states in a dedicated state file.

### 3. [[Concept Extraction]]
An LLM analyzes each changed source document to identify and extract key concepts contained within. This stage determines what knowledge elements will become individual wiki pages.

### 4. Page Generation
For each extracted concept, the LLM generates a properly structured wiki page with appropriate formatting, content organization, and metadata.

### 5. Interlink Resolution
Concept mentions across different pages are automatically wrapped in [[wikilinks]] to create connections between related topics, building the knowledge graph structure.

### 6. Index Generation
A comprehensive table of contents is constructed from all concept pages, providing navigation and overview of the compiled knowledge base.

## Incremental Processing

The pipeline implements [[Incremental Compilation]] similar to code compilers. Only sources that have changed since the last run require reprocessing, which provides significant benefits:

- Reduced processing time for large knowledge bases
- Lower API costs when using LLM services
- Efficient handling of frequent updates to source material

## Cross-Source Concept Handling

When multiple sources discuss the same concept, the pipeline employs semantic dependency tracking to manage overlapping content. If one source changes, the system triggers recompilation of shared concepts using content from all contributing sources to maintain accuracy and completeness.

## Output Format

The pipeline generates output compatible with Obsidian and similar knowledge management tools. Each concept page includes:

- YAML frontmatter with metadata
- [[wikilinks]] for cross-references
- Source attribution
- Timestamps for tracking compilation history

## Sources

knowledge-compilation.md
