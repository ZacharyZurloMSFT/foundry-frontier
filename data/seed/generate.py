"""Generate the active insurance PDFs/manifest locally without model/cloud calls.

Default domain is insurance. Source procedures, canonical fixture policy wording
and synthetic record snapshots become the active knowledge corpus.
Explicit --domain retail preserves the historical Azure-model generation path.
Existing retail PDFs remain on disk but are not in the insurance manifest.
"""
from __future__ import annotations

import json
import argparse
import html
import os
import random
import sys
from pathlib import Path

# Legacy retail generation reuses settings + demo_data.
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "apps" / "api"))

from app.demo_data import SKUS, RETURN_WINDOWS  # noqa: E402

OUT = Path(__file__).resolve().parent / "generated"
OUT.mkdir(parents=True, exist_ok=True)

MANIFEST = OUT / "manifest.json"


def _client():
    from openai import AzureOpenAI
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from app.settings import get_settings
    s = get_settings()
    endpoint = s.foundry_endpoint or os.environ["FOUNDRY_ENDPOINT"]
    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
    )
    return AzureOpenAI(
        azure_endpoint=endpoint,
        azure_ad_token_provider=token_provider,
        api_version="2024-10-21",
    )


def _generate(prompt: str, system: str, max_tokens: int = 1600) -> str:
    from app.settings import get_settings
    client = _client()
    s = get_settings()
    # gpt-5.x rejects the legacy `max_tokens` param; use `max_completion_tokens`
    # which is accepted by both gpt-4 and gpt-5 families on api-version 2024-10-21+.
    resp = client.chat.completions.create(
        model=s.worker_deployment,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        max_completion_tokens=max_tokens,
        temperature=0.4,
    )
    return resp.choices[0].message.content or ""


def _render_pdf(path: Path, title: str, body: str) -> None:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    doc = SimpleDocTemplate(str(path), pagesize=letter, title=title)
    styles = getSampleStyleSheet()
    flow = [Paragraph(html.escape(title), styles["Title"]), Spacer(1, 12)]
    for para in body.split("\n\n"):
        flow.append(Paragraph(html.escape(para).replace("\n", "<br/>"), styles["BodyText"]))
        flow.append(Spacer(1, 6))
    doc.build(flow)


# ── Corpus definitions ────────────────────────────────────────────────────
# Each entry becomes one PDF blob + one manifest row. Keep the mix small
# (~20 docs) so azd up finishes quickly and Search indexing is snappy.

_POLICY_DOCS = [
    {
        "policy_ref": "RETURN-POLICY-STD-2026",
        "title": "Northwind Standard Return Policy (2026)",
        "doc_type": "policy",
        "prompt": (
            "Write Northwind Industries' standard return policy for 2026. Cover: "
            f"return windows per category ({json.dumps(RETURN_WINDOWS)}), "
            "condition requirements (unopened vs opened), "
            "refund method (original tender), "
            "restocking fees (0% for gold-tier, 10% for others on non-defective returns), "
            "who pays return shipping (Northwind for defective; customer for change-of-mind), "
            "and the policy id RETURN-POLICY-STD-2026 that agents must cite."
        ),
    },
    {
        "policy_ref": "SHIPPING-SLA-STD-2026",
        "title": "Northwind Shipping SLA (2026)",
        "doc_type": "policy",
        "prompt": (
            "Write Northwind's shipping SLA for 2026. Cover: standard 7-day delivery "
            "commitment, expedited 3-day option for gold-tier, carrier is NW-Freight, "
            "SLA remedies for missed delivery (full refund + 10% goodwill credit), "
            "urgency tiers P1..P4 with clear definitions "
            "(P1=safety/fraud, P2=time-sensitive, P3=normal, P4=fyi), "
            "and the policy id SHIPPING-SLA-STD-2026."
        ),
    },
    {
        "policy_ref": "WARRANTY-STD-2026",
        "title": "Northwind Warranty Terms (2026)",
        "doc_type": "policy",
        "prompt": (
            "Write Northwind's product warranty terms for 2026. Cover: 12-month "
            "standard warranty (24 months for gold-tier), what's covered (manufacturing "
            "defects, DOA), what's not (accidental damage, wear-and-tear), how to "
            "file a claim, expected turnaround (5 business days for RMA approval), "
            "and the policy id WARRANTY-STD-2026."
        ),
    },
    {
        "policy_ref": "REFUND-AUTH-2026",
        "title": "Northwind Refund Authorization Matrix (2026)",
        "doc_type": "policy",
        "prompt": (
            "Write Northwind's refund authorization matrix for 2026. Frontline agents "
            "may auto-approve refunds under $50. Supervisors handle $50-$249. Managers "
            "handle $250+. Any goodwill credit (regardless of amount) requires supervisor "
            "review. Trust and Safety handles suspected fraud or account takeover. "
            "Cite the policy id REFUND-AUTH-2026 which the resolution-approver agent "
            "uses to route drafted resolutions."
        ),
    },
]

_PRODUCT_DOCS_BY_SKU = [
    {
        "sku": sku["sku"],
        "category": sku["category"],
        "title": f"Product Catalog — {sku['name']} ({sku['sku']})",
        "doc_type": "product",
        "prompt": (
            f"Write a one-page product catalog entry for the Northwind {sku['name']} "
            f"(SKU {sku['sku']}, category {sku['category']}, ${sku['price']:.2f}). "
            "Include: dimensions, materials, care instructions, warranty class "
            "(standard 12-month), 3 common customer questions with answers, "
            "and 2 known issues the CX team should know about (e.g., a small production "
            "run had a plating defect resolved by RMA)."
        ),
    }
    for sku in SKUS[:6]
]

_PRECEDENT_DOCS = [
    {
        "policy_ref": f"CASE-2026-{i:04d}",
        "title": title,
        "doc_type": "precedent",
        "prompt": prompt,
    }
    for i, (title, prompt) in enumerate(
        [
            (
                "Precedent: Shipping SLA breach — full refund + goodwill credit",
                "Write a resolved-case precedent doc. A gold-tier customer's order "
                "sat at the Chicago DC for 9 days (SLA is 7). CX approved a full "
                "refund plus $20 goodwill credit citing SHIPPING-SLA-STD-2026. "
                "Include the case id CASE-2026-0001 and note this precedent is "
                "citable by the resolution-draft agent."
            ),
            (
                "Precedent: Damaged-on-arrival kitchen skillet — RMA + reship",
                "Write a resolved-case precedent for a cast-iron skillet that "
                "arrived cracked. CX approved a same-SKU reship (no cost to "
                "customer) plus an RMA for the damaged unit, citing "
                "RETURN-POLICY-STD-2026. Case id CASE-2026-0002."
            ),
            (
                "Precedent: Change of mind on unopened home item — 30-day full refund",
                "Write a resolved-case precedent for a silver-tier customer returning "
                "an unopened merino throw blanket 12 days after purchase. CX approved "
                "a full refund (customer pays return shipping) citing "
                "RETURN-POLICY-STD-2026. Case id CASE-2026-0003."
            ),
            (
                "Precedent: Cancel unshipped order — 100% refund",
                "Write a resolved-case precedent for a customer canceling a "
                "processing-status order before it shipped. CX approved a full refund "
                "with no restocking fee, citing RETURN-POLICY-STD-2026. Case id "
                "CASE-2026-0004."
            ),
            (
                "Precedent: Suspected account takeover — hold + Trust & Safety",
                "Write a resolved-case precedent for a suspected account takeover "
                "(new shipping address on a gold-tier account, high-value order). "
                "CX escalated to Trust & Safety and paused the shipment. Cite "
                "REFUND-AUTH-2026 for the escalation path. Case id CASE-2026-0005."
            ),
            (
                "Precedent: Warranty defect after 6 months — RMA approved",
                "Write a resolved-case precedent for a customer whose espresso "
                "machine developed a heating-element defect 6 months post-purchase. "
                "CX approved a warranty RMA per WARRANTY-STD-2026 (still within the "
                "12-month window). Case id CASE-2026-0006."
            ),
        ],
        start=1,
    )
]

_FAQ_DOCS = [
    {
        "policy_ref": "FAQ-STATUS-2026",
        "title": "FAQ: Order Status Quick Reference",
        "doc_type": "faq",
        "prompt": (
            "Write a one-page FAQ for the Order Concierge agents to reference "
            "when handling order-status questions. Include: how to interpret "
            "each status (processing, in_transit, delivered, returned, canceled), "
            "when to offer a proactive resolution (SLA within 24h of breach), "
            "and when to escalate (>3 status changes in 24h = investigate). "
            "Cite policy id FAQ-STATUS-2026."
        ),
    },
    {
        "policy_ref": "FAQ-EMPATHY-2026",
        "title": "FAQ: Empathy Macros",
        "doc_type": "faq",
        "prompt": (
            "Write a one-page empathy-macros reference for CX agents. Provide 4 "
            "short opening lines for different sentiments (angry, frustrated, "
            "worried, calm) and 4 short closing lines that confirm the action taken. "
            "Cite policy id FAQ-EMPATHY-2026."
        ),
    },
]

CORPUS = _POLICY_DOCS + _PRODUCT_DOCS_BY_SKU + _PRECEDENT_DOCS + _FAQ_DOCS

_SYS_POLICY = (
    "You are a policy writer for Northwind Industries. Produce clean, "
    "structured policy prose with numbered sections. Use plain language. "
    "Always end with a line 'Policy reference: <id>' matching the id you "
    "were given."
)
_SYS_CATALOG = (
    "You are a product catalog copywriter for Northwind Industries. Write "
    "professional, factual product descriptions. Include sections: Overview, "
    "Specifications, Care, Warranty, Common Questions, Known Issues."
)


def _system_for(entry: dict) -> str:
    return _SYS_CATALOG if entry["doc_type"] == "product" else _SYS_POLICY


def generate_insurance() -> None:
    corpus_path = Path(__file__).resolve().parent / "insurance-corpus.json"
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    manifest = []
    for entry in corpus:
        source = _ROOT / entry["source"]
        body = source.read_text(encoding="utf-8")
        if section := entry.get("section"):
            if section not in body:
                raise ValueError(f"Missing source section {section}: {source}")
            body = section + body.split(section, 1)[1]
        pdf = OUT / f"{entry['id']}.pdf"
        _render_pdf(pdf, entry["title"], body)
        manifest.append({
            "id": entry["id"], "doc_type": entry["doc_type"],
            "title": entry["title"], "policy_ref": entry["policy_ref"],
            "business_domain": "insurance", "fictional": True,
            "source_version": entry.get("source_version", "1.0"), "effective_date": "2026-01-01",
            "jurisdiction": entry.get("jurisdiction", "Fictional Washington/Colorado personal auto"),
            "source_uri": entry["source"], "path": f"generated/{pdf.name}",
            "text": body,
        })
        print(f"  + {pdf.name}")
    fixture_path = _ROOT / "data" / "northwind" / "insurance-fixtures.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    if fixture.get("business_domain") != "insurance" or not fixture.get("synthetic"):
        raise ValueError("Insurance generation requires the canonical synthetic insurance fixture.")
    for wording in fixture["policy_wordings"]:
        title = wording["title"]
        body = (
            f"FICTIONAL NORTHWIND INSURANCE. {fixture['disclaimer']}\n\n"
            f"Source: {wording['wording_id']}; version {wording['version']}; "
            f"jurisdiction {wording['state']}; effective {wording['effective_from']} "
            f"through {wording['effective_to']}.\n\n" +
            "\n\n".join(f"Section {section['section_id']}: {section['text']}\n"
                        f"Citation: {section['policy_ref']}\nSource URI: {section['source_uri']}"
                        for section in wording["sections"])
        )
        pdf = OUT / f"{wording['wording_id'].lower()}.pdf"
        _render_pdf(pdf, title, body)
        manifest.append({
            "id": wording["wording_id"].lower(), "doc_type": "policy", "title": title,
            "policy_ref": wording["wording_id"], "business_domain": "insurance", "fictional": True,
            "source_version": wording["version"], "effective_date": wording["effective_from"],
            "jurisdiction": f"Fictional {wording['state']} personal auto",
            "source_uri": f"urn:northwind:synthetic:policy:{wording['wording_id']}",
            "path": f"generated/{pdf.name}", "text": body,
        })
        print(f"  + {pdf.name}")
    for claim in fixture["claims"]:
        policy = next(p for p in fixture["policies"] if p["policy_id"] == claim["policy_id"])
        holder = next(h for h in fixture["policyholders"] if h["policyholder_id"] == claim["policyholder_id"])
        title = f"Northwind Insurance - Synthetic record snapshot {claim['claim_id']}"
        body = (
            f"FICTIONAL DEMO RECORD SNAPSHOT, dataset {fixture['dataset_version']}. "
            f"{fixture['disclaimer']}\n\n"
            f"Jurisdiction {policy['state']}; policy period {policy['effective_from']} "
            f"through {policy['effective_to']}; source form {policy['wording_id']}.\n\n"
            f"Record citations: {claim['claim_id']}, {policy['policy_id']}, {holder['policyholder_id']}. "
            "These are records, not versioned wording section references.\n\n"
            + json.dumps({"policyholder": holder, "policy": policy, "claim": claim}, indent=2)
        )
        doc_id = f"nw-record-{claim['claim_id'].lower()}"
        pdf = OUT / f"{doc_id}.pdf"
        _render_pdf(pdf, title, body)
        manifest.append({
            "id": doc_id, "doc_type": "claim_record", "title": title,
            "policy_ref": policy["wording_id"], "business_domain": "insurance", "fictional": True,
            "source_version": fixture["dataset_version"], "effective_date": policy["effective_from"],
            "jurisdiction": f"Fictional {policy['state']} personal auto",
            "source_uri": f"urn:northwind:synthetic:claim:{claim['claim_id']}",
            "path": f"generated/{pdf.name}", "text": body,
        })
        print(f"  + {pdf.name}")
    MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(manifest)} insurance sources -> {MANIFEST}; no model or cloud calls.")


def generate_retail() -> None:
    manifest: list[dict] = []
    random.seed(42)
    for entry in CORPUS:
        body = _generate(entry["prompt"], _system_for(entry), max_tokens=1400)
        safe = entry["title"].lower().replace(" ", "-").replace("/", "-")
        pdf = OUT / f"{safe}.pdf"
        _render_pdf(pdf, entry["title"], body)
        manifest.append({
            "id": f"retail-{safe}",
            "doc_type": entry["doc_type"],
            "title": entry["title"],
            "policy_ref": entry.get("policy_ref"),
            "sku": entry.get("sku"),
            "category": entry.get("category"),
            "path": f"generated/{pdf.name}",
            "text": body,
            "business_domain": "retail",
        })
        print(f"  + {pdf.name}")

    MANIFEST.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(manifest)} docs → {MANIFEST}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", choices=("insurance", "retail"),
                        default=os.environ.get("NORTHWIND_BUSINESS_DOMAIN", "insurance"))
    args = parser.parse_args()
    generate_insurance() if args.domain == "insurance" else generate_retail()
