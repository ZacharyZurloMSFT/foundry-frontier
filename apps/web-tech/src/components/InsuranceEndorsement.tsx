export default function InsuranceEndorsement({ endorsement }: { endorsement: string | Record<string, unknown> }) {
  if (typeof endorsement === "string") return <>{endorsement}</>;
  const text = (key: string) => typeof endorsement[key] === "string" ? endorsement[key] as string : undefined;
  const version = text("version");
  const from = text("effective_from");
  const through = text("effective_to");
  return <>
    <strong>{text("title") ?? text("endorsement_id") ?? "Endorsement record"}</strong>
    <p>{[text("endorsement_id"), version && `Version ${version}`, text("state"),
      from && `Effective ${from}${through ? ` through ${through}` : ""}`].filter(Boolean).join(" · ")}</p>
    {text("description") && <p>{text("description")}</p>}
    {text("policy_ref") && <p className="claims-loop-note">Wording citation: <code>{text("policy_ref")}</code></p>}
    <p className="claims-loop-note">Endorsement terms require human review; listing an endorsement does not establish applicability or approve a claim.</p>
    <details><summary>Structured endorsement record</summary>
      <pre className="claims-json">{JSON.stringify(endorsement, null, 2)}</pre>
    </details>
  </>;
}
