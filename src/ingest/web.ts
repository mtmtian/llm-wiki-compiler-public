/**
 * Web URL ingestion module.
 * Fetches a URL, extracts readable content using Mozilla Readability,
 * and converts the result to clean markdown via Turndown.
 *
 * Throws descriptive errors on network failures or when the page
 * cannot be parsed into readable content.
 */

import { JSDOM } from "jsdom";
import { Readability } from "@mozilla/readability";
import TurndownService from "turndown";

interface WebIngestResult {
  title: string;
  content: string;
}

/** Fetch a URL and return its readable content as markdown. */
async function fetchAndParse(url: string): Promise<Response> {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`Failed to fetch ${url}: HTTP ${response.status}`);
  }
  return response;
}

/** Extract readable content from raw HTML using Readability. */
function extractReadableContent(html: string, url: string): { title: string; htmlContent: string } {
  const dom = new JSDOM(html, { url });
  const reader = new Readability(dom.window.document);
  const article = reader.parse();

  if (!article || !article.content) {
    throw new Error(`Could not extract readable content from ${url}`);
  }

  return {
    title: article.title || "Untitled",
    htmlContent: article.content,
  };
}

/** Convert HTML to clean markdown using Turndown. */
function convertToMarkdown(html: string): string {
  const turndown = new TurndownService({ headingStyle: "atx" });
  return turndown.turndown(html);
}

/**
 * Ingest a web URL and return its content as markdown.
 * @param url - The URL to fetch and convert.
 * @returns An object with the extracted title and markdown content.
 * @throws On network failure or unparseable content.
 */
export default async function ingestWeb(url: string): Promise<WebIngestResult> {
  const response = await fetchAndParse(url);
  const html = await response.text();
  const { title, htmlContent } = extractReadableContent(html, url);
  const content = convertToMarkdown(htmlContent);

  return { title, content };
}
