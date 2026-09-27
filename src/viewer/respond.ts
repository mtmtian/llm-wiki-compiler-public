/**
 * How the viewer's HTTP layer writes a response.
 *
 * Extracted from `server.ts` when the page handlers moved to `api-pages.ts`:
 * both files need the same JSON writers and the same render-failure envelope,
 * and duplicating them is exactly how two routes end up disagreeing about the
 * error shape a client is promised.
 *
 * Every error goes out through {@link writeJsonError}'s single
 * `{ error: { code, message } }` envelope, and {@link tryRenderBody} swallows
 * the thrown text so a render or sanitize failure surfaces as the spec's
 * `render_failed` rather than leaking internals to the client.
 */

import type { ServerResponse } from "http";
import { renderPageHtml } from "./render.js";
import type { ViewerSnapshot } from "./types.js";

/** Write a JSON response body with the given status. */
export function writeJson(res: ServerResponse, status: number, body: unknown): void {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.end(JSON.stringify(body));
}

/** Standard `{ error: { code, message } }` envelope. */
export function writeJsonError(
  res: ServerResponse,
  status: number,
  code: string,
  message: string,
): void {
  writeJson(res, status, { error: { code, message } });
}

/** Write the spec's exact `render_failed` 500 envelope. */
export function writeRenderFailed(res: ServerResponse): void {
  writeJsonError(res, 500, "render_failed", "Could not render page.");
}

/**
 * Wrap the renderer in a catch and return null on any thrown error.
 * Render or sanitize failures must emit the spec's `render_failed`
 * envelope rather than leak the raw thrown text — see {@link writeRenderFailed}.
 */
export function tryRenderBody(
  body: string,
  snapshot: ViewerSnapshot,
  isLoopback: boolean,
): { html: string } | null {
  try {
    return renderPageHtml(body, snapshot, { isLoopback });
  } catch {
    return null;
  }
}
