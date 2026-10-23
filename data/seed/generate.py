"""Generate the insurance PDFs/manifest locally without model/cloud calls.

Source procedures, canonical fixture policy wording and synthetic record
snapshots become the active knowledge corpus. `--domain insurance` is the
only domain and the default; the option remains for existing callers.
"""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

OUT = Path(__file__).resolve().parent / "generated"

MANIFEST = OUT / "manifest.json"


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


def generate_insurance() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", choices=("insurance",), default="insurance")
    parser.parse_args(argv)
    generate_insurance()


if __name__ == "__main__":
    main()
