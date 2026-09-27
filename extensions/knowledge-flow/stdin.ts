/**
 * Bounded JSON input helpers for the private knowledge workflow process.
 * Bytes are accumulated before UTF-8 decoding so a multibyte character split
 * across stream chunks is decoded exactly once at the process boundary.
 */

/** Ordinary workflow requests are deliberately kept small. */
export const DEFAULT_EVENT_BYTE_LIMIT = 600_000;

/** Materialization carries immutable records and therefore has a larger cap. */
export const MATERIALIZE_EVENT_BYTE_LIMIT = 32_000_000;

/** Select the raw-byte cap used by an operation. */
export function eventByteLimit(operation: string | undefined): number {
  return operation === "materialize" ? MATERIALIZE_EVENT_BYTE_LIMIT : DEFAULT_EVENT_BYTE_LIMIT;
}

/** Read and parse one bounded JSON event from an arbitrary async byte stream. */
export async function readBoundedJson<T>(
  chunks: AsyncIterable<Uint8Array>,
  maximum: number,
): Promise<T> {
  const buffers: Buffer[] = [];
  let size = 0;
  for await (const chunk of chunks) {
    const bytes = Buffer.from(chunk);
    size += bytes.byteLength;
    if (size > maximum) throw new Error("Knowledge event is too large");
    buffers.push(bytes);
  }
  return JSON.parse(Buffer.concat(buffers).toString("utf8")) as T;
}
