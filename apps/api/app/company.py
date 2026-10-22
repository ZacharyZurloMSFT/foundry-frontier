"""Northwind company manifest helpers.

Loads `data/northwind/company.yaml` at startup and exposes:

- `get_company_dict()` — the manifest as a plain dict.
- `resolve_department(slug, default_slug="order-concierge")` — maps a department
  slug to its manifest entry, used to stamp telemetry with the owning Foundry project.

The manifest is bundled into the container at `azd package` time via a
prepackage hook (see azure.yaml) which copies `data/northwind/` into
`apps/api/app/_northwind/`. On the host during local dev we walk up
the tree and find the canonical `data/northwind/`. Either path works.
"""
from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

# Candidate roots we might find the manifest under, in preference order.
# 1) Bundled copy inside the image (apps/api/app/_northwind/company.yaml).
# 2) Repo root during local dev (walks up from this file).
_APP_ROOT = Path(__file__).resolve().parent
_HERE_BUNDLE = _APP_ROOT / "_northwind" / "company.yaml"

_manifest_path: Path | None = None
_manifest_parent: Path | None = None

if _HERE_BUNDLE.exists():
    _manifest_path = _HERE_BUNDLE
    # Point sys.path at apps/api/app so `from _northwind import ...` works if
    # we ever load the __init__.py loader from the bundled copy.
    _manifest_parent = _APP_ROOT
else:
    for parent in _APP_ROOT.parents:
        candidate = parent / "data" / "northwind" / "company.yaml"
        if candidate.exists():
            _manifest_path = candidate
            _manifest_parent = parent
            break

if _manifest_parent is not None:
    sys.path.insert(0, str(_manifest_parent))

# Try the canonical data.northwind package first (repo-root path). If we're
# running against the bundled in-image copy, fall back to a small inline
# loader that reads the yaml directly — the bundle intentionally skips the
# __init__.py loader to keep the image dependencies minimal.
try:
    from data.northwind import Company, Department  # type: ignore[attr-defined]
    from data.northwind import load_company as _load_company  # type: ignore[attr-defined]
    _HAVE_PACKAGE_LOADER = True
except ImportError:
    _HAVE_PACKAGE_LOADER = False
    Company = None  # type: ignore[assignment,misc]
    Department = None  # type: ignore[assignment,misc]
    _load_company = None  # type: ignore[assignment]


DEFAULT_DEPARTMENT_SLUG = "order-concierge"


def _load_from_yaml(path: Path) -> dict[str, Any]:
    import yaml
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@lru_cache
def _raw_manifest() -> dict[str, Any]:
    if _manifest_path is None:
        raise RuntimeError(
            "Northwind company manifest not found. Expected either "
            "apps/api/app/_northwind/company.yaml (bundled) or "
            "data/northwind/company.yaml (repo root)."
        )
    return _load_from_yaml(_manifest_path)


@lru_cache
def get_company_dict() -> dict[str, Any]:
    """Return the manifest as a plain dict (works in the container even when
    the data.northwind Python package isn't bundled)."""
    if _HAVE_PACKAGE_LOADER:
        c = _load_company()  # type: ignore[misc]
        return {
            "slug": c.slug,
            "name": c.name,
            "tagline": c.tagline,
            "brand_color": c.brand_color,
            "hero": c.hero,
            "platform_tabs": [
                {"id": t.id, "label": t.label, "hosted_in_project": t.hosted_in_project}
                for t in c.platform_tabs
            ],
            "departments": [
                {
                    "slug": d.slug, "name": d.name, "tagline": d.tagline,
                    "pillar": d.pillar, "icon": d.icon,
                    "brand_color": d.brand_color,
                    "foundry_project": d.foundry_project,
                    "search_index": d.search_index,
                    "enabled_tabs": list(d.enabled_tabs),
                    "customer_archetype": d.customer_archetype,
                }
                for d in c.departments
            ],
        }
    # Bundled-in-image path — parse yaml directly.
    raw = _raw_manifest()
    company = raw["company"]
    return {
        "slug": company["slug"],
        "name": company["name"],
        "tagline": company["tagline"],
        "brand_color": company.get("brand_color", ""),
        "hero": company.get("hero", ""),
        "platform_tabs": [
            {"id": t["id"], "label": t["label"],
             "hosted_in_project": t["hosted_in_project"]}
            for t in raw.get("platform_tabs", [])
        ],
        "departments": [
            {
                "slug": d["slug"], "name": d["name"], "tagline": d["tagline"],
                "pillar": d["pillar"], "icon": d.get("icon", ""),
                "brand_color": d.get("brand_color", ""),
                "foundry_project": d["foundry_project"],
                "search_index": d["search_index"],
                "enabled_tabs": list(d.get("enabled_tabs", [])),
                "customer_archetype": d.get("customer_archetype", ""),
            }
            for d in raw["departments"]
        ],
    }


def resolve_department(header_value: str | None,
                       default_slug: str = DEFAULT_DEPARTMENT_SLUG) -> dict[str, Any]:
    """Normalize + look up a department slug. Unknown / missing slugs fall back
    to the Order Concierge default. Returns the department dict from the manifest
    (works with or without the package loader)."""
    company = get_company_dict()
    slug = (header_value or "").strip().lower() or default_slug
    for d in company["departments"]:
        if d["slug"] == slug:
            return d
    for d in company["departments"]:
        if d["slug"] == default_slug:
            return d
    raise RuntimeError(
        f"Neither '{slug}' nor default '{default_slug}' is a known department."
    )
