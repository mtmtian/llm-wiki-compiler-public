/** Shared writer for real v3 embedding-store fixtures used by CLI and hook tests. */
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { EMBEDDINGS_FILE, LLMWIKI_DIR } from "../../src/utils/constants.js";
import { resolveEmbeddingFingerprint } from "../../src/utils/embeddings-store.js";

export interface V3StoreEntry {
  pageId: string;
  title: string;
  summary: string;
  embeddingTextHash: string;
  vector: number[];
}

export interface V3StoreChunk {
  pageId: string;
  title: string;
  chunkIndex: number;
  contentHash: string;
  text: string;
  vector: number[];
}

export interface V3StoreSpec {
  model: string;
  vector: number[];
  fingerprint?: string;
  entries: V3StoreEntry[];
  chunks: V3StoreChunk[];
}

/** Resolve the same provider fingerprint as a child CLI under a test env. */
export function fingerprintForEnv(env: NodeJS.ProcessEnv): string {
  const saved = Object.keys(env).map((key) => [key, process.env[key]] as const);
  Object.assign(process.env, env);
  try {
    return resolveEmbeddingFingerprint();
  } finally {
    for (const [key, value] of saved) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
}

/** Write a complete v3 store with the same shape emitted by the compiler. */
export async function writeV3EmbeddingStore(root: string, spec: V3StoreSpec): Promise<void> {
  const at = "2026-05-24T00:00:00.000Z";
  const store = {
    version: 3,
    model: spec.model,
    ...(spec.fingerprint ? { fingerprint: spec.fingerprint } : {}),
    dimensions: spec.vector.length,
    entries: spec.entries.map((entry) => ({ ...entry, updatedAt: at })),
    chunks: spec.chunks.map((chunk) => ({ ...chunk, updatedAt: at })),
  };
  await mkdir(path.join(root, LLMWIKI_DIR), { recursive: true });
  await writeFile(path.join(root, EMBEDDINGS_FILE), JSON.stringify(store, null, 2), "utf-8");
}
