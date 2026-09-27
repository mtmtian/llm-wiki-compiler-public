/** Explicit prose labels guide retrieval without inventing validity dates or changing stored publications. */
export type SectionTemporalStatus = "current" | "historical" | "unspecified";
const HISTORICAL_HEADING = /^(历史|决策历史|此前|当时|旧(?:方案|规则|决定|实现|策略|版本)|已(?:失效|废弃|替代)|history|historical|previous|superseded|retired)/i;
const CURRENT_HEADING = /^(当前|现行|current|active)/i;

/** A page's historical status cannot be promoted by a heading saying current. */
export function sectionTemporalStatus(heading: string, pageStatus: unknown): SectionTemporalStatus {
  const labels = heading.split(" / ").map(label => label.trim());
  if (pageStatus === "historical" || labels.some(label => HISTORICAL_HEADING.test(label))) return "historical";
  return labels.some(label => CURRENT_HEADING.test(label)) ? "current" : "unspecified";
}

/** Mixed current/history questions retain both; explicit dates are not silently interpreted as validity bounds. */
export function taskTemporalIntent(prompt: string): SectionTemporalStatus {
  const current = /现在|目前|当前|现行|如今|\b(now|today|current)\b/i.test(prompt);
  const historical = /当时|以前|此前|历史|过去|原来|旧方案|\b(historical|previous|formerly)\b|used to/i.test(prompt);
  if (current === historical) return "unspecified";
  return current ? "current" : "historical";
}
