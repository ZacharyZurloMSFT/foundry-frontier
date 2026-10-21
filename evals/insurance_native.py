"""Bounded native smoke plans and an explicitly authorized SDK executor.

No uploads, dataset registration, grader registration, agent updates or training.
Hosted batch concurrency is bounded by total rows of each sequential job, not by
an invented SDK sampling/concurrency option. Verified target bounds must include
all internal tool/model calls and retries, including non-token charges.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from evals.insurance import DIMENSIONS, FIXTURE, rubric
from evals.insurance_review import (
    check_execution_review_expiry, lineage_record, read_json, rows, sha256,
    verify_code_lineage, verify_pack, verify_receipt, write_new,
)

ENDPOINT = "https://aisvc-zurlo-northwind.services.ai.azure.com/api/projects/nw-insurance-claims"
TENANT = "043c812b-d828-4850-8da0-cfda17c314d4"
PREFIX = "azureai://accounts/aisvc-zurlo-northwind/projects/nw-insurance-claims/data/"
PROMPTS = {"triage": "triage.yaml", "lookup": "tech_lookup.yaml", "policy_wording": "manual_retrieval.yaml"}


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def plain(value):
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    for method in ("model_dump", "as_dict", "to_dict"):
        if hasattr(value, method):
            return plain(getattr(value, method)())
    return value


def positive(value, name, *, integer=False):
    if isinstance(value, bool) or (integer and type(value) is not int):
        raise ValueError(f"{name} must be a positive {'integer' if integer else 'number'}")
    try:
        number = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"Invalid {name}") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError(f"{name} must be positive and finite")
    return int(number) if integer else number


def config_template(pack: Path) -> dict:
    manifest = verify_pack(pack)
    return {
        "schema_version": 2, "project_endpoint": ENDPOINT, "tenant_id": TENANT,
        "fixture_sha256": manifest["fixture_sha256"],
        "max_samples": 8, "max_concurrency": 3, "max_total_tokens": 200000, "cost_cap_usd": "1.00",
        "target_model": {"provider": "Azure OpenAI", "deployment": "gpt-4o",
                         "model_name": "", "model_version": "", "sku": "",
                         "input_usd_per_million": None, "output_usd_per_million": None,
                         "pricing_verified": False, "pricing_reference": "", "pricing_verified_at": ""},
        "judge_model": {"deployment": "gpt-4o", "model_name": "", "model_version": "", "sku": "",
                        "input_usd_per_million": None, "output_usd_per_million": None,
                        "pricing_verified": False, "pricing_reference": "", "pricing_verified_at": ""},
        "bounds": {"verified": False, "evidence": "", "target_input_tokens_total_per_case": None,
                   "target_output_tokens_total_per_case": None, "target_non_token_usd_per_case": None,
                   "judge_input_tokens_per_call": None, "judge_output_tokens_per_call": 512,
                   "judge_calls_per_dimension_per_case": None,
                   "judge_non_token_usd_per_case": None,
                   "acknowledge_no_service_hard_cap": False},
        "datasets": {
            role: {
                "artifact": f"native-smoke-{role}.jsonl", "dataset_id": "",
                "uploaded_sha256": manifest["files_sha256"][f"native-smoke-{role}.jsonl"],
                "uploaded_bytes_verified": False, "uploaded_bytes_evidence": "",
                "target": {"type": "azure_ai_agent", "name": "", "version": ""},
                "agent_source_sha256": "", "agent_config_sha256": "",
                "prompt_sha256": manifest["prompt_sources_sha256"][str(Path("agents") / "insurance" / prompt)],
                "prompt_binding_verified": False, "prompt_binding_evidence": "",
                "internal_concurrency_bounded": False,
            } for role, prompt in PROMPTS.items()
        },
    }


def definition(role: str, judge: dict, bounds: dict) -> dict:
    criteria = []
    for dimension in DIMENSIONS:
        prompt = rubric(role, dimension)
        # ScoreModelGrader enforces its own scalar output; preserve 1-5 rubric semantics.
        prompt = prompt[:prompt.index("Return ONLY")]
        criteria.append({
            "type": "score_model", "name": "insurance-" + dimension, "model": judge["deployment"],
            "range": [1.0, 5.0], "pass_threshold": 5.0,
            "input": [{"role": "system", "content": prompt}, {"role": "user", "content":
                "Query: {{item.query}}\nContext: {{item.context}}\nExpected: {{item.expected_behavior}}\n"
                "Ground truth: {{item.ground_truth}}\nActual response: {{sample.output_text}}"}],
            "sampling_params": {"max_completions_tokens": bounds["judge_output_tokens_per_call"],
                                "temperature": 0},
        })
    return {
        "name": "northwind-insurance-" + role + "-smoke-v2",
        "data_source_config": {"type": "custom", "include_sample_schema": True, "item_schema": {
            "type": "object", "required": ["example_id", "query", "expected_behavior", "context", "ground_truth"],
            "properties": {name: {"type": "string"} for name in
                           ("example_id", "query", "expected_behavior", "context", "ground_truth")},
        }},
        "testing_criteria": criteria,
    }


def build_plan(pack: Path, config: dict, approval: dict | None = None, *,
               execution_code_review: Path | None = None, execution_code_review_sha256: str | None = None,
               approval_receipt_path: Path | None = None) -> dict:
    if approval_receipt_path is not None:
        actual_approval = read_json(approval_receipt_path)
        if approval is not None and actual_approval != approval:
            raise ValueError("Supplied approval differs from the exact bound artifact approval receipt")
        approval = actual_approval
    manifest = verify_code_lineage(
        pack, Path(__file__).resolve().parents[1], execution_code_review=execution_code_review,
        execution_code_review_sha256=execution_code_review_sha256, approval_receipt_path=approval_receipt_path)
    if config.get("schema_version") != 2 or config.get("project_endpoint") != ENDPOINT or config.get("tenant_id") != TENANT:
        raise ValueError("Wrong native config schema/project/tenant")
    if config.get("fixture_sha256") != manifest["fixture_sha256"] or sha256(FIXTURE) != manifest["fixture_sha256"]:
        raise ValueError("Golden fixture changed; prepare and approve a NEW immutable pack")
    limits = {k: positive(config[k], k, integer=True) for k in ("max_samples", "max_concurrency", "max_total_tokens")}
    if limits["max_samples"] > 8 or limits["max_concurrency"] > 3 or limits["max_total_tokens"] > 200000:
        raise ValueError("Smoke caps are <=8 samples, <=3 concurrent cases, <=200000 total tokens")
    cap = positive(config["cost_cap_usd"], "cost_cap_usd")
    if cap > Decimal("1.00"):
        raise ValueError("Initial smoke conditional cost cap is $1; broader budgets need separate implementation/review")
    blockers = []
    models = {}
    for key in ("target_model", "judge_model"):
        model = config[key]
        required = ("deployment", "model_name", "model_version", "sku", "pricing_reference", "pricing_verified_at")
        if model.get("pricing_verified") is not True or not all(isinstance(model.get(k), str) and model[k].strip() for k in required):
            blockers.append(key + " live identity/current pricing verification required")
        try:
            models[key] = (positive(model.get("input_usd_per_million"), "input price"),
                           positive(model.get("output_usd_per_million"), "output price"))
        except ValueError:
            blockers.append(key + " finite verified positive prices required")
    bounds = config["bounds"]
    if bounds.get("verified") is not True or not bounds.get("evidence") or bounds.get("acknowledge_no_service_hard_cap") is not True:
        blockers.append("Verified TOTAL target/judge calls, retries, tokens and service charges required; not an Azure billing cap")
    numeric = {}
    for key in ("target_input_tokens_total_per_case", "target_output_tokens_total_per_case",
                "judge_input_tokens_per_call", "judge_output_tokens_per_call", "judge_calls_per_dimension_per_case"):
        try:
            numeric[key] = positive(bounds.get(key), key, integer=True)
        except ValueError:
            blockers.append("Missing positive token/call bound: " + key)
    for key in ("target_non_token_usd_per_case", "judge_non_token_usd_per_case"):
        try:
            if isinstance(bounds.get(key), bool):
                raise ValueError()
            value = Decimal(str(bounds.get(key)))
            if not value.is_finite() or value < 0:
                raise ValueError()
            numeric[key] = value
        except Exception:
            blockers.append("Verified nonnegative service charge bound required: " + key)
    jobs = {}
    total = 0
    specs = config.get("datasets")
    if not isinstance(specs, dict) or not specs or set(specs) - set(PROMPTS):
        raise ValueError("Native smoke supports only selected read-only triage/lookup/policy_wording roles")
    for role, spec in specs.items():
        artifact = f"native-smoke-{role}.jsonl"
        if spec.get("artifact") != artifact:
            raise ValueError("Use exact complete role-specific smoke artifact; no subset replay or inline copies")
        seed = rows(pack / artifact)
        if not seed or any(r["target_role"] != role or r["side_effects_possible"] or r["tier"] != "smoke" for r in seed):
            raise ValueError("Native smoke refuses any draft/orchestrator/side-effectful request")
        if len(seed) > limits["max_concurrency"]:
            raise ValueError("Complete role batch exceeds case concurrency cap; do not claim unsupported SDK concurrency control")
        total += len(seed)
        if not re.fullmatch(re.escape(PREFIX) + r"[^/]+/versions/[^/]+", str(spec.get("dataset_id", ""))):
            blockers.append(role + ": manually uploaded registered dataset ID required")
        if (spec.get("uploaded_sha256") != manifest["files_sha256"][artifact]
                or spec.get("uploaded_bytes_verified") is not True or not spec.get("uploaded_bytes_evidence")):
            blockers.append(role + ": remote bytes must be verified against exact approved local artifact")
        target = spec.get("target") or {}
        if target.get("type") != "azure_ai_agent" or not target.get("name") or not re.fullmatch(r"[0-9]+", str(target.get("version", ""))):
            blockers.append(role + ": verified exact agent name/version required")
        for key in ("agent_source_sha256", "agent_config_sha256", "prompt_sha256"):
            if not re.fullmatch(r"[a-f0-9]{64}", str(spec.get(key, ""))):
                blockers.append(role + ": missing " + key)
        expected_prompt = manifest["prompt_sources_sha256"][str(Path("agents") / "insurance" / PROMPTS[role])]
        if (spec.get("prompt_sha256") != expected_prompt or spec.get("prompt_binding_verified") is not True
                or not spec.get("prompt_binding_evidence") or spec.get("internal_concurrency_bounded") is not True):
            blockers.append(role + ": runtime prompt/source and internal concurrency binding required")
        jobs[role] = {
            "artifact": artifact, "artifact_sha256": manifest["files_sha256"][artifact], "selected_rows": len(seed),
            "example_ids": [r["example_id"] for r in seed], "target": target,
            "expected_item_sha256": {
                r["example_id"]: digest({k: r[k] for k in ("example_id", "query", "context", "expected_behavior", "ground_truth")})
                for r in seed},
            "source_sha256": spec.get("agent_source_sha256"), "config_sha256": spec.get("agent_config_sha256"),
            "prompt_sha256": spec.get("prompt_sha256"),
            "create": definition(role, config["judge_model"], bounds),
            "run": {"name": f"insurance-{role}-smoke-v2", "data_source": {
                "type": "azure_ai_target_completions", "source": {"type": "file_id", "id": spec.get("dataset_id")},
                "target": target, "input_messages": {"type": "template", "template": [
                    {"type": "message", "role": "user", "content": {"type": "input_text", "text": "{{item.query}}"}}]},
            }},
        }
    if total > limits["max_samples"]:
        raise ValueError("Complete approved role batches exceed sample cap")
    token_bound, cost_bound = None, None
    if len(numeric) == 7 and len(models) == 2:
        calls = len(DIMENSIONS) * numeric["judge_calls_per_dimension_per_case"]
        target_in, target_out = numeric["target_input_tokens_total_per_case"], numeric["target_output_tokens_total_per_case"]
        judge_in, judge_out = calls * numeric["judge_input_tokens_per_call"], calls * numeric["judge_output_tokens_per_call"]
        token_bound = total * (target_in + target_out + judge_in + judge_out)
        cost_bound = total * (
            (target_in * models["target_model"][0] + target_out * models["target_model"][1]
             + judge_in * models["judge_model"][0] + judge_out * models["judge_model"][1]) / Decimal(1000000)
            + numeric["target_non_token_usd_per_case"] + numeric["judge_non_token_usd_per_case"])
        if token_bound > limits["max_total_tokens"] or cost_bound > cap:
            blockers.append("Conditional total token/cost bound exceeds configured cap")
    artifacts = [job["artifact"] for job in jobs.values()]
    if approval is None:
        blockers.append("Explicit hash-bound user evaluation acceptance and bounded evaluation budget required")
    else:
        verify_receipt(pack, approval, artifacts, "evaluate")
        budget = approval.get("evaluation_budget") or {}
        if budget.get("approved") is not True or budget.get("acknowledge_conditional_bound_not_service_billing_cap") is not True:
            blockers.append("User must explicitly approve the bounded evaluation budget, separately from artifact upload")
        try:
            for key in limits:
                if limits[key] > positive(budget.get(key), key, integer=True):
                    blockers.append("User budget cap exceeded: " + key)
            if cap > positive(budget.get("cost_cap_usd"), "approved cost cap"):
                blockers.append("Configured USD cap exceeds explicit user evaluation budget")
        except ValueError as exc:
            blockers.append(str(exc))
    return {
        "schema_version": 2, "execution_kind": "native_sdk_smoke_plan_not_executed",
        "project_endpoint": ENDPOINT, "tenant_id": TENANT, "manifest_sha256": sha256(pack / "manifest.json"),
        "config_sha256": digest(config), "fixture_sha256": manifest["fixture_sha256"],
        "source_code_sha256": manifest["source_code_sha256"], **lineage_record(manifest), "jobs": jobs,
        "target_model": config["target_model"], "judge_model": config["judge_model"],
        "bounds": bounds, "selected_samples": total, **limits, "cost_cap_usd": str(cap),
        "conditional_token_upper_bound": token_bound,
        "conditional_cost_upper_bound_usd": str(cost_bound) if cost_bound is not None else None,
        "apply_blockers": blockers, "training_approved": False, "release_approved": False,
        "sdk_interface": [
            "p.get_openai_client(max_retries=0, timeout=30)",
            "client.evals.create(**job['create'], metadata=identity)",
            "client.evals.runs.create(eval_id=eval_id, **job['run'], metadata=identity)",
            "client.evals.runs.retrieve(eval_id=eval_id, run_id=run_id)",
            "client.evals.runs.output_items.list(eval_id=eval_id, run_id=run_id, limit=100, after=cursor)",
        ],
        "limitations": ["Model rubrics cannot establish server grounding without tool/queue evidence.",
                        "Native smoke is diagnostic, not a five-dimension release approval.",
                        "No SDK hard total cost cap; bounds conditional on independently verified all-call behavior.",
                        "No automatic retry/replay of POSTs or draft-producing cases.",
                        "Reconcile actual usage/non-token charges before approving broader evaluation."],
    }


def _includes(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and _includes(actual[k], value) for k, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(_includes(a, b) for a, b in zip(actual, expected))
    return actual == expected


def sample_text(sample: dict) -> str | None:
    direct = sample.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    parts = []
    output = sample.get("output")
    if not isinstance(output, list):
        return None
    for message in output:
        if not isinstance(message, dict) or message.get("role") not in (None, "assistant"):
            continue
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(item["text"] for item in content if isinstance(item, dict) and isinstance(item.get("text"), str))
    text = "\n".join(parts)
    return text if text.strip() else None


def record_output_evidence(job: dict, entry: dict, receipt: dict):
    """Retain unknown/error cases too, before result validation can raise."""
    output = entry["output_items"]
    results = []
    for example_id in job["example_ids"]:
        found = [item for item in output if (item.get("datasource_item") or {}).get("example_id") == example_id]
        item = found[0] if len(found) == 1 else {}
        sample = item.get("sample") or {}
        text = sample_text(sample)
        scores = {}
        for dimension in DIMENSIONS:
            name = "insurance-" + dimension
            matches = [score for score in item.get("results", []) if score.get("name") == name]
            score = matches[0] if len(matches) == 1 else {}
            value = score.get("score")
            reason = score.get("reason") or score.get("reasoning")
            if not reason and isinstance(score.get("sample"), dict):
                reason = score["sample"].get("reason") or score["sample"].get("output")
            valid = (text is not None and item.get("status") in ("pass", "fail", "completed")
                     and not sample.get("error") and not score.get("error")
                     and type(value) in (int, float) and 1 <= value <= 5
                     and type(score.get("passed")) is bool and bool(reason))
            status = "unknown" if not valid else ("pass" if value == 5 and score["passed"] else "fail")
            scores[dimension] = {"status": status, "score": value,
                                 "reason": reason or "No complete native output/grader reason exposed; unknown, not pass."}
        results.append({
            "example_id": example_id, "native_output_item_id": item.get("id"),
            "response": text, "response_id": sample.get("response_id"),
            "actual_model": sample.get("model"), "usage": sample.get("usage"),
            "scores_and_reasons": item.get("results", []), "gate_scores": scores,
            "missing_provenance": ([] if sample.get("response_id") else
                                   ["Actual response ID not exposed; parent must correlate server traces, never substitute output-item ID."])
                                  + ([] if sample.get("model") else ["Actual model ID not exposed by native sample."]),
        })
    entry["cases"] = results
    receipt["failure_clusters"] = {
        f"{dimension}:{status}": [
            case["example_id"] for previous in receipt["jobs"].values() for case in previous.get("cases", [])
            if case["gate_scores"][dimension]["status"] == status]
        for dimension in DIMENSIONS for status in ("fail", "unknown")
    }
    receipt["aggregate_metrics"] = None


def execute_sdk(project, client, plan: dict, receipt: dict, save, *, timeout_seconds=300, poll_seconds=5):
    """Parents may call this after explicit consent. Ambiguous writes are never replayed."""
    if plan["apply_blockers"]:
        raise ValueError("Native execution blocked: " + "; ".join(plan["apply_blockers"]))
    if receipt.get("plan_sha256") != digest(plan):
        raise ValueError("Native receipt must bind the complete prepared/executed source plan")
    check_execution_review_expiry(plan)
    receipt.update(lineage_record(plan))
    deadline = time.monotonic() + timeout_seconds
    if receipt.get("jobs"):
        raise ValueError("Existing attempt must be manually reconciled; never automatically replay requests")
    receipt["preflight"] = {"checked_at": now(), "models": {}, "targets": {}}
    for key in ("target_model", "judge_model"):
        model = plan[key]
        actual = plain(project.deployments.get(model["deployment"]))
        actual = {
            **actual,
            "model_name": actual.get("model_name", actual.get("modelName")),
            "model_version": actual.get("model_version", actual.get("modelVersion")),
        }
        if any(actual.get(k) != model[k] for k in ("model_name", "model_version")) or (actual.get("sku") or {}).get("name") != model["sku"]:
            raise ValueError("Live model identity/SKU differs from verified pricing")
        receipt["preflight"]["models"][key] = {
            "deployment": model["deployment"], "model_name": actual["model_name"],
            "model_version": actual["model_version"], "sku": actual["sku"]["name"]}
    for role, job in plan["jobs"].items():
        target = job["target"]
        live = plain(project.agents.get_version(target["name"], target["version"]))
        metadata = live.get("metadata") or {}
        if (live.get("version") != target["version"] or live.get("status") != "active"
                or metadata.get("source_sha256") != job["source_sha256"]
                or metadata.get("config_sha256") != job["config_sha256"]):
            raise ValueError("Live agent version/source/config differs from consent-bound plan")
        definition_value = live.get("definition") or {}
        env = definition_value.get("environment_variables", definition_value.get("environmentVariables", {})) or {}
        deployment = (definition_value.get("model") or env.get("MODEL_DEPLOYMENT_NAME")
                      or env.get("TRIAGE_MODEL_DEPLOYMENT") or env.get("ORDERS_MODEL_DEPLOYMENT")
                      or env.get("AZURE_AI_MODEL_DEPLOYMENT_NAME"))
        if deployment != plan["target_model"]["deployment"]:
            raise ValueError("Live provider deployment does not match pinned target model")
        if (definition_value.get("kind", definition_value.get("type")) == "hosted"
                and env.get("NORTHWIND_BUSINESS_DOMAIN") != "insurance"):
            raise ValueError("Live hosted business domain is not insurance; retail fine-tunes/configs are incompatible")
        dataset_id = job["run"]["data_source"]["source"]["id"]
        name, version = dataset_id[len(PREFIX):].split("/versions/")
        dataset = plain(project.datasets.get(name, version))
        properties = dataset.get("properties") or {}
        kind = dataset.get("type", dataset.get("kind", properties.get("kind")))
        if dataset.get("id") != dataset_id or kind != "uri_file":
            raise ValueError("Live dataset must be the approved exact single-file URI registration, not a folder/batch")
        receipt["preflight"]["targets"][role] = {
            "agent_name": target["name"], "agent_version": live["version"], "source_sha256": metadata["source_sha256"],
            "config_sha256": metadata["config_sha256"], "actual_provider_deployment": deployment,
            "dataset_id": dataset["id"], "dataset_kind": kind}
    save()
    for role, job in plan["jobs"].items():
        if time.monotonic() >= deadline:
            raise TimeoutError("Native deadline reached before submission")
        check_execution_review_expiry(plan)
        identity = {"insurance_plan_sha256": receipt["plan_sha256"], "insurance_role": role,
                    "insurance_scope": "evaluation_only_no_training"}
        entry = {"state": "eval_submission_started", "identity": identity}
        receipt["jobs"][role] = entry
        save()
        evaluation = plain(client.evals.create(**job["create"], metadata=identity))
        entry.update(eval_id=evaluation["id"], state="definition_readback_pending")
        save()
        live_eval = plain(client.evals.retrieve(eval_id=entry["eval_id"]))
        if (live_eval.get("id") != entry["eval_id"] or not _includes(live_eval.get("metadata"), identity)
                or not _includes(live_eval, job["create"])):
            raise ValueError("Native evaluation definition differs from recorded criteria")
        entry.update(state="run_submission_started", definition_readback_verified=True)
        save()
        check_execution_review_expiry(plan)
        run = plain(client.evals.runs.create(eval_id=entry["eval_id"], **job["run"], metadata=identity))
        entry.update(run_id=run["id"], state="submitted")
        save()
        while time.monotonic() < deadline:
            run = plain(client.evals.runs.retrieve(eval_id=entry["eval_id"], run_id=entry["run_id"]))
            if (run.get("id") != entry["run_id"] or run.get("eval_id") != entry["eval_id"]
                    or not _includes(run.get("metadata"), identity)
                    or not _includes(run.get("data_source"), job["run"]["data_source"])):
                raise ValueError("Native run read-back IDs, target, dataset or provenance mismatch")
            entry["native_run"] = run
            entry["state"] = run["status"]
            save()
            if run["status"] in ("failed", "canceled", "cancelled"):
                raise RuntimeError("Native service run failed/cancelled; no further jobs submitted")
            if run["status"] == "completed":
                break
            time.sleep(min(poll_seconds, max(0, deadline - time.monotonic())))
        else:
            raise TimeoutError("Native wait timeout; reconcile known run, do not resubmit")
        output, cursor = [], None
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("Native results deadline")
            kwargs = {"eval_id": entry["eval_id"], "run_id": entry["run_id"], "limit": 100}
            if cursor:
                kwargs["after"] = cursor
            page = client.evals.runs.output_items.list(**kwargs)
            batch = plain(page)
            output.extend(batch["data"])
            if len(output) > job["selected_rows"]:
                raise ValueError("Native output exceeded bounded sample count")
            more = page.has_next_page() if hasattr(page, "has_next_page") else batch.get("has_more", False)
            if not more:
                break
            next_cursor = batch["data"][-1]["id"] if batch["data"] else None
            if not next_cursor or next_cursor == cursor:
                raise ValueError("Native output pagination did not advance")
            cursor = next_cursor
        entry["output_items"] = output
        entry["output_items_sha256"] = digest(output)
        entry["state"] = "results_recorded"
        record_output_evidence(job, entry, receipt)
        save()
        if (len(output) != job["selected_rows"] or len({i["id"] for i in output}) != len(output)
                or {i.get("datasource_item", {}).get("example_id") for i in output} != set(job["example_ids"])
                or any(i.get("run_id") != entry["run_id"] or i.get("eval_id") != entry["eval_id"] for i in output)):
            raise ValueError("Native outputs incomplete or not bound to every expected case")
        if any(digest({k: item["datasource_item"].get(k) for k in
                       ("example_id", "query", "context", "expected_behavior", "ground_truth")})
               != job["expected_item_sha256"][item["datasource_item"]["example_id"]] for item in output):
            raise ValueError("Native read-back query/context/gold differs from exact approved artifact")
        expected_graders = {"insurance-" + d for d in DIMENSIONS}
        if any(item.get("status") not in ("pass", "fail", "completed")
               or (item.get("sample") or {}).get("error") or sample_text(item.get("sample") or {}) is None
               or {r.get("name") for r in item.get("results", [])} != expected_graders
               or any(r.get("error") or r.get("status") == "error"
                      or type(r.get("score")) not in (int, float) or not 1 <= r["score"] <= 5
                      or type(r.get("passed")) is not bool
                      for r in item.get("results", []))
               for item in output):
            raise ValueError("Native result has missing output/grader or error; never mark this passed")
        entry.update(state="completed", actual_provider_deployment=plan["target_model"]["deployment"],
                     agent_version=job["target"]["version"])
        save()
    receipt.update(state="completed", completed_at=now(), native_run_completed=True,
                   release_approved=False, training_approved=False,
                   note="Native model-judged smoke only. Strict offline trace/queue gates and human release still required.")
    all_cases = [case for job in receipt["jobs"].values() for case in job["cases"]]
    receipt["native_judge_smoke_passed"] = not any(receipt["failure_clusters"].values())
    receipt["missing_provenance_cases"] = [case["example_id"] for case in all_cases if case["missing_provenance"]]
    receipt["aggregate_metrics"] = {
        dimension: {"cases": len(all_cases), "pass_rate": sum(
            case["gate_scores"][dimension]["status"] == "pass" for case in all_cases) / len(all_cases),
                    "required_pass_rate": 1.0}
        for dimension in DIMENSIONS}
    save()


def execute(pack: Path, config: dict, approval_path: Path, receipt_path: Path, confirmation: str, *,
            timeout_seconds=300, execution_code_review: Path | None = None,
            execution_code_review_sha256: str | None = None):
    approval = read_json(approval_path)
    approval_hash = sha256(approval_path)
    if confirmation != approval_hash:
        raise ValueError("--confirm must equal the exact user approval receipt SHA256")
    plan = build_plan(pack, config, approval, execution_code_review=execution_code_review,
                      execution_code_review_sha256=execution_code_review_sha256, approval_receipt_path=approval_path)
    if plan["apply_blockers"]:
        raise ValueError("; ".join(plan["apply_blockers"]))
    if sha256(approval_path) != approval_hash:
        raise ValueError("Artifact approval receipt changed while preparing execution")
    receipt = {"schema_version": 2, "state": "preflight", "created_at": now(), "plan": plan,
               "plan_sha256": digest(plan), "approval_receipt_sha256": approval_hash,
               **lineage_record(plan),
               "approval_receipt_path": str(approval_path.resolve()), "approver": approval["approver"],
               "approval_mode": approval["approval_mode"],
               "jobs": {}, "native_run_completed": False, "training_approved": False, "release_approved": False}
    write_new(receipt_path, receipt)  # Exclusive: never overwrite or replay another attempt.

    def save():
        receipt["updated_at"] = now()
        with receipt_path.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(receipt, stream, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())

    try:
        from azure.ai.projects import AIProjectClient
        from azure.identity import AzureDeveloperCliCredential

        with AzureDeveloperCliCredential(tenant_id=TENANT) as credential, \
                AIProjectClient(endpoint=ENDPOINT, credential=credential, allow_preview=True,
                                retry_total=0, connection_timeout=10, read_timeout=30) as project, \
                project.get_openai_client(max_retries=0, timeout=30) as client:
            execute_sdk(project, client, plan, receipt, save, timeout_seconds=timeout_seconds)
    except Exception as exc:
        receipt.update(state="interrupted_reconcile_do_not_replay", error_type=type(exc).__name__)
        save()  # Do not persist exception strings, HTTP headers, SAS URLs or tokens.
        raise
    return receipt
