export function decisionIdFromSearch(search: string): string | null {
  const value = new URLSearchParams(search).get("decision")?.trim() || "";
  return /^dec_[A-Za-z0-9]+$/.test(value) ? value : null;
}
