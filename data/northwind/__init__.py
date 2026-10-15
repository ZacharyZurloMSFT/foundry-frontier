"""Northwind Industries manifest loader.

The demo is organized around one company (Northwind Industries) with four
departments, each backed by its own Foundry project inside the shared
AI Services account. This module is the single Python entry point for
resolving that structure — every script that used to hard-code
`data/seed` or `agents/triage.yaml` should route through here so the same
code works for Order Concierge today and the other three departments once
they land.

Usage:

    from data.northwind import load_company, load_department, list_departments

    company = load_company()
    fs = load_department("order-concierge")
    print(fs.seed_dir, fs.foundry_project)

All paths on the returned dataclasses are absolute — the loader anchors
them at the repo root regardless of the caller's cwd.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST = Path(__file__).resolve().parent / "company.yaml"


@dataclass(frozen=True)
class PlatformTab:
    id: str
    label: str
    hosted_in_project: str


@dataclass(frozen=True)
class Department:
    slug: str
    name: str
    tagline: str
    pillar: str
    icon: str
    brand_color: str
    foundry_project: str
    search_index: str
    cu_inbox_prefix: str
    enabled_tabs: tuple[str, ...]
    seed_dir: Path
    golden_path: Path
    prompts_dir: Path
    cu_analyzer_dir: Path | None
    customer_archetype: str


@dataclass(frozen=True)
class Company:
    slug: str
    name: str
    tagline: str
    brand_color: str
    hero: str
    platform_tabs: tuple[PlatformTab, ...]
    departments: tuple[Department, ...]

    def department(self, slug: str) -> Department:
        for d in self.departments:
            if d.slug == slug:
                return d
        raise KeyError(f"Unknown department: {slug}")


def _abs(path: str | None) -> Path | None:
    if path is None:
        return None
    p = Path(path)
    return p if p.is_absolute() else (_REPO_ROOT / p).resolve()


def _department(raw: dict[str, Any]) -> Department:
    return Department(
        slug=raw["slug"],
        name=raw["name"],
        tagline=raw["tagline"],
        pillar=raw["pillar"],
        icon=raw.get("icon", ""),
        brand_color=raw.get("brand_color", ""),
        foundry_project=raw["foundry_project"],
        search_index=raw["search_index"],
        cu_inbox_prefix=raw.get("cu_inbox_prefix", raw["slug"]),
        enabled_tabs=tuple(raw.get("enabled_tabs", [])),
        seed_dir=_abs(raw["seed_dir"]),  # type: ignore[arg-type]
        golden_path=_abs(raw["golden_path"]),  # type: ignore[arg-type]
        prompts_dir=_abs(raw["prompts_dir"]),  # type: ignore[arg-type]
        cu_analyzer_dir=_abs(raw.get("cu_analyzer_dir")),
        customer_archetype=raw.get("customer_archetype", ""),
    )


def load_company(path: Path | None = None) -> Company:
    """Parse company.yaml into a validated :class:`Company` object."""
    src = path or _MANIFEST
    with open(src, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    company_raw = raw["company"]
    tabs = tuple(PlatformTab(**t) for t in raw.get("platform_tabs", []))
    departments = tuple(_department(d) for d in raw["departments"])
    return Company(
        slug=company_raw["slug"],
        name=company_raw["name"],
        tagline=company_raw["tagline"],
        brand_color=company_raw.get("brand_color", ""),
        hero=company_raw.get("hero", ""),
        platform_tabs=tabs,
        departments=departments,
    )


def load_department(slug: str) -> Department:
    return load_company().department(slug)


def list_departments() -> list[str]:
    return [d.slug for d in load_company().departments]


__all__ = [
    "Company",
    "Department",
    "PlatformTab",
    "load_company",
    "load_department",
    "list_departments",
]
