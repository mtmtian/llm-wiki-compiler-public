/** Small JSON-schema builders shared by whole-topic planning and correction contracts. */

/** Create a non-empty string schema with a bounded maximum length. */
export function schemaText(maxLength: number) {
  return { type: "string", minLength: 1, maxLength };
}

/** Create an exact object schema while allowing an explicit set of optional properties. */
export function schemaObject(properties: Record<string, unknown>, optional: Record<string, unknown> = {}) {
  return { type: "object", additionalProperties: false, properties: { ...properties, ...optional }, required: Object.keys(properties) };
}

/** Create an array schema capped at its caller's allowed item count. */
export function schemaArray(items: unknown, maxItems: number) {
  return { type: "array", items, maxItems };
}
