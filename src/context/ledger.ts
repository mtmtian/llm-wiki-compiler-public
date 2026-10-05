/** Read only generation-sealed reviewed claims. Corrupt or missing projections never fall back to live host state. */
import { createHash } from "node:crypto";
import { lstat, readFile, realpath } from "node:fs/promises";
import path from "node:path";
import { z } from "zod";

const PROJECTION = ".llmwiki/reviewed-claims.json";
const MANIFEST = ".llmwiki/projection-manifest.json";
const MAX_PROJECTION_BYTES = 32 * 1024 * 1024;
const hash = z.string().regex(/^[0-9a-f]{64}$/);
const claimReference = z.string().regex(/^[0-9a-f]{64}:(?:0|[1-9]\d*)$/);
const quoteSchema = z.object({ evidenceId: z.string().min(1), kind: z.enum(["user", "assistant", "artifact"]), quote: z.string().min(1),
  locator: z.string(), observedAt: z.string(), sha256: hash, originalSha256: hash.optional() });
const claimSchema = z.object({ claimRef: claimReference, recordId: hash, equivalentPageRefs: z.array(claimReference),
  projectId: z.string().min(1), projectLabel: z.string(), title: z.string().min(1), topic: z.string(), decisionObject: z.string(),
  text: z.string().min(1), kind: z.enum(["decision", "fact", "constraint", "lesson"]),
  status: z.enum(["decided", "historical", "uncertain"]), useWhen: z.string(), rationale: z.string(),
  recordedAt: z.string(), targetPageId: z.string().nullable(), superseded: z.boolean(), quotes: z.array(quoteSchema).min(1),
}).refine(claim => claim.claimRef.startsWith(claim.recordId + ":"));
const projectionSchema = z.object({ version: z.literal(2), generationId: z.string(), claims: z.array(claimSchema),
  superseded: z.array(claimSchema), rejectedRecordIds: z.array(hash) });
const manifestSchema = z.object({ version: z.literal(2), generationId: z.string(),
  consumerFiles: z.array(z.object({ path: z.string(), sha256: hash })) });

export type ReviewedClaim = z.infer<typeof claimSchema>;
export type ReviewedClaimProjection = z.infer<typeof projectionSchema>;
export interface ReviewedClaimsResult { projection: ReviewedClaimProjection | null; warning?: string }

/** Pin the root once and consume the same bytes whose digest is covered by its generation seal. */
export async function readReviewedClaims(root: string): Promise<ReviewedClaimsResult> {
  try {
    const pinned = await realpath(root);
    const bytes = await sealedFile(pinned, PROJECTION);
    const manifestBytes = await sealedFile(pinned, MANIFEST);
    if (!bytes) {
      if (manifestBytes && manifestSchema.parse(JSON.parse(manifestBytes.toString())).consumerFiles.some(item => item.path === PROJECTION)) {
        throw new Error("sealed projection missing");
      }
      return { projection: null };
    }
    if (!manifestBytes) throw new Error("missing seal");
    const manifest = manifestSchema.parse(JSON.parse(manifestBytes.toString()));
    const entries = manifest.consumerFiles.filter(item => item.path === PROJECTION);
    if (entries.length !== 1 || entries[0].sha256 !== digest(bytes)) throw new Error("projection digest mismatch");
    const projection = projectionSchema.parse(JSON.parse(bytes.toString()));
    if (projection.generationId !== manifest.generationId || manifest.generationId !== path.basename(pinned)) throw new Error("generation mismatch");
    const references = [...projection.claims, ...projection.superseded].map(claim => claim.claimRef);
    if (new Set(references).size !== references.length) throw new Error("duplicate claim reference");
    return { projection };
  } catch { return { projection: null, warning: "reviewed-claims-invalid" }; }
}

/** Reject symlinks and oversized inputs before reading bytes from a consumer path. */
async function sealedFile(root: string, relative: string): Promise<Buffer | null> {
  const filename = path.join(root, relative);
  try {
    const directory = await lstat(path.dirname(filename));
    const info = await lstat(filename);
    if (!directory.isDirectory() || directory.isSymbolicLink() || !info.isFile() || info.isSymbolicLink()
      || info.size > MAX_PROJECTION_BYTES) throw new Error("unsafe projection file");
    return await readFile(filename);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw error;
  }
}

/** Hash content as read; the source evidence hash inside a quote may cover more than the quoted window. */
export function reviewedClaimRevision(claim: ReviewedClaim): string { return digest(Buffer.from(JSON.stringify(claim))); }

function digest(bytes: Buffer): string { return createHash("sha256").update(bytes).digest("hex"); }
