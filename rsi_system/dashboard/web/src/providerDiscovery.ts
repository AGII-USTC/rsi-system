/** Small, side-effect-free helpers shared by the provider form and tests. */
export function discoveryDraftReady(baseUrl: string, apiKey: string): boolean {
  return Boolean(baseUrl.trim() && apiKey.trim());
}

// Keep the temporary key out of the deduplication token itself. The token is
// held only in a component ref, but using a compact digest also prevents an
// accidental UI/debug dump from exposing it.
function digest(value: string): string {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return `${value.length}:${hash >>> 0}`;
}

export function discoveryRequestKey(api: string, baseUrl: string, apiKey: string): string {
  return `${api}\u0000${baseUrl.trim()}\u0000${digest(apiKey)}`;
}
