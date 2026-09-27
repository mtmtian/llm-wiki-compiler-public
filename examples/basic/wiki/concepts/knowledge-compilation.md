---
title: Knowledge Compilation
summary: The process of using LLMs to transform messy, unstructured information into clean, structured, interlinked reference material
sources:
  - knowledge-compilation.md
createdAt: "2026-04-06T07:53:54.299Z"
updatedAt: "2026-04-06T07:53:54.299Z"
---

# Knowledge Compilation

Knowledge compilation is an approach that uses Large Language Models to transform messy, unstructured information into clean, structured, and interlinked reference material. The concept operates like a compiler for knowledge: raw sources go in, and an organized wiki comes out.

## Overview

Most knowledge exists scattered across documents, articles, notes, and conversations, making it difficult to find specific information when needed. Knowledge compilation addresses this by processing these disparate sources and producing a structured wiki where every concept has its own page, properly linked to related concepts.

## How It Works

The knowledge compilation process follows a multi-stage pipeline:

1. **Ingestion**: Raw sources such as URLs, files, and documents are collected into a sources directory
2. **[[Change Detection]]**: SHA-256 hashes identify which sources have been modified since the last compilation
3. **[[Concept Extraction]]**: An LLM analyzes each changed source to extract key concepts
4. **Page Generation**: The LLM creates a structured wiki page for each identified concept
5. **Interlink Resolution**: Concept mentions across pages are wrapped in [[wikilinks]]
6. **Index Generation**: A comprehensive table of contents is built from all concept pages

## [[Incremental Compilation]]

Knowledge compilation employs [[incremental compilation]] principles similar to code compilers. Only sources that have changed require reprocessing, which saves both time and API costs. The system maintains source hashes in a state file and completely skips unchanged sources during compilation runs.

## [[Cross-Source Concepts]]

When multiple sources discuss the same concept, the compiler detects this overlap through semantic dependency tracking. If one source changes, it triggers recompilation of shared concepts using content from all contributing sources, ensuring consistency across the knowledge base.

## Output Format

The compilation process generates content compatible with Obsidian and similar tools. Each concept page includes:

- YAML frontmatter with metadata
- Proper [[wikilinks]] for cross-references
- Source attribution
- Timestamps for tracking changes
- Structured content with clear headings and organization

## Sources

- knowledge-compilation.md
