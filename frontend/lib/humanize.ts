/**
 * Deterministic, formatting-only key humanisation.
 *
 * Separators become single spaces and the first character is upper-cased.
 * Nothing else is altered: no alias table, no dictionary, no model. It is a
 * pure function of its input, so the same durable key always renders the same
 * label and a refresh can never produce a different one.
 */
export function humanizeKey(key: string): string {
  const spaced = String(key ?? '').replace(/[_-]+/g, ' ').replace(/\s+/g, ' ').trim();
  if (spaced === '') return String(key ?? '');
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}
