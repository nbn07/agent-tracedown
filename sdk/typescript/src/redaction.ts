/**
 * Redaction hooks: scrub secrets and PII before payloads leave the process.
 *
 *     tdn.init({ project: "x", endpoint, redact: defaultRedactor })
 */
export const PATTERNS: [RegExp, string][] = [
  [/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g, "[EMAIL]"],
  [/\b(?:sk|pk|rk|tdn)[-_][A-Za-z0-9_-]{16,}\b/g, "[API_KEY]"],
  [/\bAKIA[0-9A-Z]{16}\b/g, "[AWS_KEY]"],
  // JWT: three base64url segments, the first starting with "eyJ".
  [/\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b/g, "[JWT]"],
  // IBAN: format only, no mod-97 checksum. Runs before CARD so the spaced
  // digit groups of an IBAN are not eaten by the card pattern first.
  [/\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b/g, "[IBAN]"],
  // IPv4: any dotted quad whose octets are all in 0-255 counts as an IP and
  // is redacted, even one that could be a version number (1.2.3.4) - the two
  // are syntactically identical and over-redacting is the safe direction.
  // A quad with an out-of-range octet (300.1.2.3) is left alone.
  [/\b(?:(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.){3}(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\b/g, "[IP]"],
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
