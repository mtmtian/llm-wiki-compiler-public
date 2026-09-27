/**
 * llmwiki viewer — shared stat-card primitive.
 *
 * A stat card is four stacked pieces — label, badge, big value, sub-line —
 * built by `buildStatCard` from a plain "card" descriptor (`key`, `label`,
 * `badge`, `value(model)`, `sub(model)`, plus the optional `warnWhenNonZero`
 * / `calmWhenZero` / `badgeWhenCalm` state flags) and a `model` object the
 * two functions read from. The shape of `model` is the caller's choice —
 * the Overview dashboard (`viewer-dashboard.js`) projects the `/api/pages`
 * envelope into a nested `{ counts, graph, ... }` object — and a caller
 * only has to honour the descriptor contract above.
 *
 * The Overview dashboard is the sole caller. This lives in its own module
 * rather than back inside viewer-dashboard.js because that file is already
 * at the project's file-size budget, and because the warn/calm state logic
 * is a self-contained rule with its own `is-warn`/`is-calm` CSS
 * counterparts in viewer-dashboard.css. (The health route was the second
 * caller until the Nebula health screen replaced its five stat cards with
 * the CONTENTS strip — see viewer-health.js.)
 *
 * `warn` and `calm` are mutually exclusive per card: a card opts into
 * `warnWhenNonZero`, `calmWhenZero`, both, or neither, and `statCardState`
 * below returns at most one of the two for a given value.
 */

import { el } from "./viewer-dom.js";

/** True when a card's warnWhenNonZero flag is set and its value has something to report. */
function isCardWarn(card, value) {
  return card.warnWhenNonZero === true && value > 0;
}

/** True when a card's calmWhenZero flag is set and its value is clear. */
function isCardCalm(card, value) {
  return card.calmWhenZero === true && value === 0;
}

/**
 * Resolve a card's state from its value: "warn" and "calm" are mutually
 * exclusive (see the file header and the CSS rules' own comments in
 * viewer-dashboard.css); anything else is "neutral" (plain informational
 * cards, and any signal card that opted into neither flag).
 *
 * @param {object} card - A card descriptor (see file header).
 * @param {number} value - That card's computed value.
 * @returns {"warn"|"calm"|"neutral"}
 */
function statCardState(card, value) {
  if (isCardWarn(card, value)) return "warn";
  if (isCardCalm(card, value)) return "calm";
  return "neutral";
}

/** Resolve a card's badge text for its current state (see badgeWhenCalm's own comment). */
function statCardBadgeText(card, state) {
  return state === "calm" && card.badgeWhenCalm ? card.badgeWhenCalm : card.badge;
}

/**
 * Build one stat card: a label + badge head, a big value, and a sub-line.
 *
 * @param {object} card - A card descriptor: `key`, `label`, `badge`,
 *   `value(model)`, `sub(model)`, and the optional `warnWhenNonZero` /
 *   `calmWhenZero` / `badgeWhenCalm` state flags (see file header).
 * @param {object} model - Whatever object `card.value`/`card.sub` read
 *   from — shape is the caller's choice.
 * @returns {HTMLElement}
 */
export function buildStatCard(card, model) {
  const value = card.value(model);
  const state = statCardState(card, value);
  const wrap = el("div", `stat-card${state === "neutral" ? "" : ` is-${state}`}`);
  wrap.dataset.stat = card.key;
  const head = el("div", "stat-head");
  head.appendChild(el("span", "stat-label", card.label));
  head.appendChild(el("span", "stat-badge", statCardBadgeText(card, state)));
  wrap.appendChild(head);
  wrap.appendChild(el("div", "stat-value", String(value)));
  wrap.appendChild(el("div", "stat-sub", card.sub(model)));
  return wrap;
}
