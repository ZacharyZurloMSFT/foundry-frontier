/** Stable DOM id for a policy wording section, so an assistant citation can point at the text it cites. */
export function wordingAnchor(policyRef: string): string {
  return `wording-${policyRef.replace(/[^A-Za-z0-9_-]/g, "-")}`;
}

/** Opens every collapsed ancestor, scrolls to the cited wording and moves focus there. False when it is not on this page. */
export function revealWording(policyRef: string, root: Document = document): boolean {
  const target = root.getElementById(wordingAnchor(policyRef));
  if (!target) return false;
  for (let node: Element | null = target.parentElement; node; node = node.parentElement) {
    if (node instanceof HTMLDetailsElement) node.open = true;
  }
  target.scrollIntoView({ block: "center" });
  target.focus();
  return true;
}
