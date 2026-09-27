/**
 * @file test/fixtures/pipeline-project.ts
 * @description A newsroom project that exercises every lifecycle SHAPE the
 * Pipeline panel has to draw, in one profile.
 *
 * It is the SHIPPED newsroom template's entity types with two edits, not a
 * hand-written copy of them, so the fixture cannot drift from the profile the
 * product actually installs:
 *   - `articles` gains `killed` — a declared state (the lifecycle field's enum
 *     must equal the state set, so it has to appear there) that no transition
 *     reaches from `draft`. That is what the panel draws as unreachable.
 *   - `bylines` loses its order — no terminal state and no edges, so nothing
 *     puts its two states in a sequence and the panel must imply none.
 *   - `desks` is untouched: the plain ordered case.
 *
 * The seeded pages make two collectors disagree on purpose. `articles` and
 * `bylines` each get pages that fail their field contract but still carry a
 * lifecycle value, so the UNFILTERED state tally exceeds the VALID page count by
 * exactly the number of rejected pages — the finding the panel's third column
 * reports.
 */

import { newsroomEntities, newsroomRelations } from "../../src/profile/templates/builtin/newsroom.js";
import type { EntityTypeDef, FieldDef, LifecycleDef, ProfilePack } from "../../src/profile/types.js";
import { writeMarkdownPage } from "./profile-fixtures.js";

/** The state `articles` declares but no transition can reach. */
const UNREACHABLE_STATE = "killed";

/** The frontmatter field every newsroom type carries its lifecycle state in. */
const STAGE_FIELD = "stage";

/**
 * The type's `stage` enum field. Throws rather than defaulting: this fixture is
 * an EDIT of the shipped template, so a template that stopped declaring one must
 * fail loudly here instead of quietly seeding a different shape than intended.
 */
function stageEnum(def: EntityTypeDef): FieldDef & { enum: string[] } {
  const field = def.fields?.[STAGE_FIELD];
  if (field?.type !== "enum" || !field.enum) throw new Error("newsroom template lost its stage enum");
  return { ...field, enum: field.enum };
}

/** The type's declared lifecycle, for the same reason and with the same failure. */
function declaredLifecycle(def: EntityTypeDef): LifecycleDef {
  if (!def.lifecycle) throw new Error("newsroom template lost its lifecycle");
  return def.lifecycle;
}

/** The shipped `articles` type plus a declared state nothing transitions into. */
function withUnreachableState(def: EntityTypeDef): EntityTypeDef {
  const stage = stageEnum(def);
  const lifecycle = declaredLifecycle(def);
  return {
    ...def,
    fields: { ...def.fields, [STAGE_FIELD]: { ...stage, enum: [...stage.enum, UNREACHABLE_STATE] } },
    lifecycle: { ...lifecycle, transitions: { ...lifecycle.transitions, [UNREACHABLE_STATE]: [] } },
  };
}

/** The shipped `bylines` type with its order removed: two states, no edges, no terminal. */
function withoutDeclaredOrder(def: EntityTypeDef): EntityTypeDef {
  const states = stageEnum(def).enum;
  return {
    ...def,
    lifecycle: {
      ...declaredLifecycle(def),
      terminal: [],
      transitions: Object.fromEntries(states.map((state) => [state, []])),
    },
  };
}

/** The profile pack: three entity types, one relation type, three lifecycle shapes. */
export const PIPELINE_PROFILE: ProfilePack = {
  schemaVersion: 1,
  profileId: "newsroom",
  profileVersion: "0.1.0",
  displayName: "Newsroom",
  entities: {
    articles: withUnreachableState(newsroomEntities.articles),
    desks: newsroomEntities.desks,
    bylines: withoutDeclaredOrder(newsroomEntities.bylines),
  },
  relations: newsroomRelations,
};

/** One page: its entity directory, slug, and frontmatter lines. */
interface PipelinePage {
  directory: string;
  slug: string;
  frontmatter: string;
}

/**
 * The seeded corpus. The two `headline`-less articles and the `reporter`-less
 * byline are the deliberate rejects: each still declares a lifecycle state, so
 * each lands in the tally and NOT in the valid count.
 */
const PAGES: PipelinePage[] = [
  { directory: "wiki/articles", slug: "harbour-lease", frontmatter: "headline: Harbour lease\nstage: draft" },
  { directory: "wiki/articles", slug: "school-meals", frontmatter: "headline: School meals\nstage: draft" },
  { directory: "wiki/articles", slug: "transit-levy", frontmatter: "headline: Transit levy\nstage: draft" },
  { directory: "wiki/articles", slug: "arena-deal", frontmatter: "headline: Arena deal\nstage: edited" },
  { directory: "wiki/articles", slug: "ferry-terminal", frontmatter: "headline: Ferry terminal\nstage: published" },
  { directory: "wiki/articles", slug: "night-bus", frontmatter: "headline: Night bus\nstage: published" },
  { directory: "wiki/articles", slug: "missing-headline", frontmatter: "stage: draft" },
  { directory: "wiki/articles", slug: "spiked-story", frontmatter: `stage: ${UNREACHABLE_STATE}` },
  { directory: "wiki/desks", slug: "city-desk", frontmatter: "name: City Desk\nstage: active" },
  { directory: "wiki/desks", slug: "investigations", frontmatter: "name: Investigations\nstage: active" },
  { directory: "wiki/desks", slug: "weekend-edition", frontmatter: "name: Weekend Edition\nstage: archived" },
  { directory: "wiki/bylines", slug: "ruth-carey", frontmatter: "reporter: Ruth Carey\nstage: confirmed" },
  { directory: "wiki/bylines", slug: "sam-oyelaran", frontmatter: "reporter: Sam Oyelaran\nstage: confirmed" },
  { directory: "wiki/bylines", slug: "dev-anand", frontmatter: "reporter: Dev Anand\nstage: confirmed" },
  { directory: "wiki/bylines", slug: "no-reporter", frontmatter: "stage: pending" },
];

/** Write every page of {@link PAGES} under `root`. */
export async function seedPipelinePages(root: string): Promise<void> {
  for (const page of PAGES) {
    await writeMarkdownPage(root, page.directory, page.slug, `---\n${page.frontmatter}\n---\nBody.\n`);
  }
}
