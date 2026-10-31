/**
 * Redaction hooks: scrub secrets and PII before payloads leave the process.
 *
 *     tdn.init({ project: "x", endpoint, redact: defaultRedactor })
 */
export const PATTERNS: [RegExp, string][] = [
  [/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g, "[EMAIL]"],
  [/\b(?:sk|pk|rk|tdn)[-_][A-Za-z0-9_-]{16,}\b/g, "[API_KEY]"],
  [/\bAKIA[0-9A-Z]{16}\b/g, "[AWS_KEY]"],
  [/\b(?:\d[ -]?){13,16}\b/g, "[CARD]"],
  [/\+?\d{1,2}[ .-]?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}\b/g, "[PHONE]"],
];

export function redactText(text: string): string {
  for (const [pattern, repl] of PATTERNS) text = text.replace(pattern, repl);
  return text;
}

/** Recursively redact strings inside objects and arrays. */
export function defaultRedactor(value: unknown): unknown {
  if (typeof value === "string") return redactText(value);
  if (Array.isArray(value)) return value.map(defaultRedactor);
  if (value && typeof value === "object" && Object.getPrototypeOf(value) === Object.prototype) {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, defaultRedactor(v)]));
  }
  return value;
}
