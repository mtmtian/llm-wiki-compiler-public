---
title: Concept Extraction
summary: The process where an LLM analyzes sources to identify and extract key concepts worth documenting
sources:
  - knowledge-compilation.md
createdAt: "2026-04-06T07:54:03.462Z"
updatedAt: "2026-04-06T07:54:03.462Z"
---

# Concept Extraction

Concept extraction is a key stage in the [[Knowledge Compilation]] pipeline where a Large Language Model analyzes source documents to identify and extract the main concepts, topics, and ideas contained within them.

## Overview

During the concept extraction phase, the LLM reads each source that has been flagged as changed during [[Change Detection]] and systematically identifies the key concepts present in the material. This process transforms unstructured text into a structured list of discrete concepts that can each become individual wiki pages.

## Role in the Pipeline

Concept extraction occurs as the third stage in the [[Knowledge Compilation]] process, following Ingestion and [[Change Detection]]. The extracted concepts then feed into the subsequent Page Generation stage, where each identified concept becomes the basis for a structured wiki page.

## Cross-Source Concept Recognition

The extraction process is designed to recognize when concepts appear across multiple sources. This capability enables [[Cross-Source Concepts]] functionality, where the system can detect semantic overlap and ensure that concepts discussed in different documents are properly unified in the final wiki output.

## Integration with Semantic Tracking

Concept extraction works closely with Semantic Dependency Tracking to identify relationships between concepts both within individual sources and across the entire knowledge base. This ensures that when sources change, all related concepts are properly updated during recompilation.

## Output

The result of concept extraction is a structured list of concepts that serves as the foundation for generating individual wiki pages. Each extracted concept will ultimately become a page with proper [[Wikilinks]] connecting it to related concepts throughout the knowledge base.

## Sources

- knowledge-compilation.md
