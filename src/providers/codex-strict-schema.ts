/**
 * Preserve optional tool fields across Codex's required-only wire protocol.
 *
 * Backports the nullable/omission contract from upstream 85c8b8f, retaining the
 * existing SDK strict conversion for references and schema keywords. Responses
 * are restored against the original schema: valid nulls remain data; only nulls
 * introduced to represent omitted properties are removed. Literal enum/default
 * payloads are never traversed as schemas. No caller-owned value is mutated.
 */
import Ajv from "ajv";
import { toStrictJsonSchema } from "openai/lib/transform.js";
import type { JSONSchema } from "openai/lib/jsonschema.js";

type Schema = Record<string, unknown>;
const SCHEMA_ID = "urn:llmwiki:codex-tool";

/** Return whether a value can carry JSON Schema keywords. */
function isSchema(value: unknown): value is Schema {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Visit schema keywords only, leaving const, enum and default data untouched. */
function visitChildren(node: Schema, visit: (child: unknown) => void): void {
  for (const key of ["additionalItems", "additionalProperties", "contains", "else", "if", "not", "propertyNames", "then"]) {
    visit(node[key]);
  }
  for (const key of ["allOf", "anyOf", "items", "oneOf", "prefixItems"]) {
    const children = node[key];
    if (Array.isArray(children)) children.forEach(visit);
    else if (children !== undefined) visit(children);
  }
  for (const key of ["$defs", "definitions", "dependentSchemas", "dependencies", "patternProperties", "properties"]) {
    const children = node[key];
    if (isSchema(children)) Object.values(children).forEach(visit);
  }
}

/** Use upstream's nullable representation without losing enums or constraints. */
function nullable(schema: unknown): unknown {
  if (isSchema(schema) && schema.type === "null") return schema;
  if (!isSchema(schema) || typeof schema.type !== "string" || Object.hasOwn(schema, "const")) {
    return { anyOf: [schema, { type: "null" }] };
  }
  const result: Schema = { ...schema, type: [schema.type, "null"] };
  if (Array.isArray(schema.enum)) result.enum = [...schema.enum, null];
  return result;
}

/** Require every property while admitting omission through an explicit null. */
function requireNullableProperties(node: unknown, visited = new Set<object>()): void {
  if (!isSchema(node) || visited.has(node)) return;
  visited.add(node);
  visitChildren(node, child => requireNullableProperties(child, visited));
  if (!isSchema(node.properties)) return;
  const required = Array.isArray(node.required) ? node.required : [];
  for (const [name, property] of Object.entries(node.properties)) {
    if (!required.includes(name)) node.properties[name] = nullable(property);
  }
  node.required = Object.keys(node.properties);
  node.additionalProperties = false;
}

/** Clone a tool schema into Codex's strict transport form. */
export function toStrictSchema(schema: Schema): Schema {
  const cloned = structuredClone(schema);
  requireNullableProperties(cloned);
  return toStrictJsonSchema(cloned as JSONSchema) as Schema;
}

/** Encode a JSON Pointer token for a URI fragment; property names are arbitrary JSON keys. */
function pointer(key: string | number): string {
  return encodeURIComponent(String(key).replace(/~/g, "~0").replace(/\//g, "~1"));
}

/** Read the original node; AJV's compiled schema may already have resolved a ref. */
function schemaAt(location: string, validator: Ajv): unknown {
  let node: unknown = validator.getSchema(SCHEMA_ID)?.schema;
  for (const part of location.slice(SCHEMA_ID.length + 1).split("/").slice(1)) {
    const key = decodeURIComponent(part).replace(/~1/g, "/").replace(/~0/g, "~");
    if (Array.isArray(node)) node = node[Number(key)];
    else if (isSchema(node)) node = node[key];
    else return undefined;
  }
  return node;
}

/** Restore against the schema at a pointer, sharing AJV's reference resolution. */
function restoreValue(value: unknown, location: string, validator: Ajv): unknown {
  const accepts = validator.getSchema(location);
  const schema = schemaAt(location, validator);
  if (!accepts || accepts(value) || !isSchema(schema)) return value;
  if (typeof schema.$ref === "string" && schema.$ref.startsWith("#")) {
    return restoreValue(value, SCHEMA_ID + schema.$ref, validator);
  }
  if (Array.isArray(value) && isSchema(schema.items)) {
    return value.map(item => restoreValue(item, `${location}/items`, validator));
  }
  value = restoreUnion(value, schema, location, validator);
  if (Array.isArray(schema.allOf)) {
    value = schema.allOf.reduce((current, _branch, index) =>
      restoreValue(current, `${location}/allOf/${index}`, validator), value);
  }
  return restoreProperties(value, schema, location, validator);
}

/** Select an original union branch only after its restored value validates. */
function restoreUnion(value: unknown, schema: Schema, location: string, validator: Ajv): unknown {
  for (const keyword of ["anyOf", "oneOf"]) {
    const branches = schema[keyword];
    if (!Array.isArray(branches)) continue;
    for (let index = 0; index < branches.length; index++) {
      const branch = `${location}/${keyword}/${index}`;
      const restored = restoreValue(value, branch, validator);
      if (validator.getSchema(branch)?.(restored)) return restored;
    }
  }
  return value;
}

/** Drop only originally invalid optional nulls and recurse through known fields. */
function restoreProperties(value: unknown, schema: Schema, location: string, validator: Ajv): unknown {
  if (!isSchema(value) || !isSchema(schema.properties)) return value;
  const properties = schema.properties;
  const required = Array.isArray(schema.required) ? schema.required : [];
  return Object.fromEntries(Object.entries(value).flatMap(([name, entry]) => {
    if (!Object.hasOwn(properties, name)) return [[name, entry]];
    const property = `${location}/properties/${pointer(name)}`;
    if (entry === null && !required.includes(name) && !validator.getSchema(property)?.(null)) return [];
    return [[name, restoreValue(entry, property, validator)]];
  }));
}

/** Undo transport-only nulls before the provider validates the original contract. */
export function dropNullOptionals(value: unknown, schema: Schema): unknown {
  const validator = new Ajv({ strict: false });
  validator.addSchema(schema, SCHEMA_ID);
  return restoreValue(value, `${SCHEMA_ID}#`, validator);
}
