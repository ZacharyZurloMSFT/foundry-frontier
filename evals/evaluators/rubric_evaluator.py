"""Reusable LLM-as-judge rubric evaluator.

Loads a `.prompty` rubric file, calls a judge model via AzureOpenAI, and
returns a normalized score/reason/pass triple. Shape matches Foundry's
built-in evaluators (`CoherenceEvaluator`, `TaskAdherenceEvaluator`) so it
plugs into `azure.ai.evaluation.evaluate()` batch runs and the same
Observability dashboards.

Usage:
    from evaluators import RubricEvaluator
    ev = RubricEvaluator(
        model_config={"azure_endpoint": ..., "azure_deployment": ..., "api_version": ...},
        prompty_path="evaluators/rubrics/triage_rubric.prompty",
        name="triage_rubric",
    )
    result = ev(query="chiller alarm at seattle", response='{"site_id":...}')
    # -> {"triage_rubric_score": 5, "triage_rubric_reason": "...", "triage_rubric_pass": True}
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

try:
    import prompty
    import prompty.azure  # noqa: F401 — registers the azure invoker
    _HAVE_PROMPTY = True
except ImportError:
    _HAVE_PROMPTY = False


PASS_THRESHOLD = 4


class RubricEvaluator:
    """LLM-as-judge evaluator driven by a `.prompty` rubric.

    The rubric is expected to produce a JSON object with at least a `score`
    field (int 1-5) and a `reason` field (str). Anything else in the JSON
    is passed through under `<name>_raw`.
    """

    def __init__(
        self,
        *,
        model_config: dict[str, Any],
        prompty_path: str | Path,
        name: str,
        pass_threshold: int = PASS_THRESHOLD,
    ) -> None:
        if not _HAVE_PROMPTY:
            raise ImportError(
                "prompty is required for RubricEvaluator. "
                "Install with: pip install prompty"
            )
        self._prompty_path = Path(prompty_path)
        if not self._prompty_path.exists():
            raise FileNotFoundError(f"Rubric not found: {self._prompty_path}")
        self._model_config = dict(model_config)
        self.name = name
        self.pass_threshold = pass_threshold

    # Public score/reason/pass keys — kept consistent so batch aggregation works.
    @property
    def score_key(self) -> str:
        return f"{self.name}_score"

    @property
    def reason_key(self) -> str:
        return f"{self.name}_reason"

    @property
    def pass_key(self) -> str:
        return f"{self.name}_pass"

    def __call__(
        self,
        *,
        query: str,
        response: str,
        context: str = "",
    ) -> dict[str, Any]:
        try:
            raw = prompty.execute(
                str(self._prompty_path),
                inputs={
                    "query": query or "",
                    "response": response or "",
                    "context": context or "",
                },
                configuration={
                    "azure_endpoint": self._model_config.get("azure_endpoint"),
                    "azure_deployment": self._model_config.get("azure_deployment"),
                    "api_version": self._model_config.get("api_version"),
                },
            )
        except Exception as exc:  # noqa: BLE001 — one bad row must not crash the batch
            return {
                self.score_key: None,
                self.reason_key: f"judge_error: {exc}",
                self.pass_key: False,
                "error": str(exc),
            }
        return self._parse(raw)

    def _parse(self, raw: Any) -> dict[str, Any]:
        payload = _coerce_json(raw)
        if payload is None:
            return {
                self.score_key: None,
                self.reason_key: f"unparseable_judge_output: {str(raw)[:200]}",
                self.pass_key: False,
                "error": "unparseable_judge_output",
            }
        score = payload.get("score")
        reason = payload.get("reason", "")
        try:
            score_int = int(score) if score is not None else None
        except (TypeError, ValueError):
            score_int = None
        out: dict[str, Any] = {
            self.score_key: score_int,
            self.reason_key: str(reason),
            self.pass_key: score_int is not None and score_int >= self.pass_threshold,
        }
        extras = {k: v for k, v in payload.items() if k not in {"score", "reason"}}
        if extras:
            out[f"{self.name}_raw"] = extras
        return out

    # azure.ai.evaluation.evaluate() calls this to aggregate per-row results.
    def _aggregate_results(self, results: list[dict[str, Any]]) -> dict[str, Any]:
        scores = [r.get(self.score_key) for r in results if isinstance(r.get(self.score_key), (int, float))]
        passes = [r.get(self.pass_key) for r in results if isinstance(r.get(self.pass_key), bool)]
        return {
            f"{self.name}.mean_score": round(sum(scores) / len(scores), 3) if scores else 0.0,
            f"{self.name}.pass_rate": round(sum(1 for p in passes if p) / len(passes), 3) if passes else 0.0,
            f"{self.name}.n_scored": len(scores),
            f"{self.name}.n_errors": sum(1 for r in results if r.get("error")),
        }


_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}")


def _coerce_json(raw: Any) -> dict[str, Any] | None:
    """Judge output can arrive as a dict, a JSON string, or prose with a JSON blob."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                return obj
        except (ValueError, TypeError):
            pass
        m = _JSON_BLOCK_RE.search(raw)
        if m:
            try:
                obj = json.loads(m.group(0))
                if isinstance(obj, dict):
                    return obj
            except (ValueError, TypeError):
                return None
    return None


def load_agent_rubric(
    agent_name: str,
    model_config: dict[str, Any],
    *,
    rubrics_dir: str | Path | None = None,
    pass_threshold: int = PASS_THRESHOLD,
) -> RubricEvaluator:
    """Resolve `rubrics/{agent_name}_rubric.prompty` and return a RubricEvaluator.

    The judge deployment is taken from `model_config["azure_deployment"]`, which
    callers typically set from `FOUNDRY_JUDGE_DEPLOYMENT` (falling back to
    `FOUNDRY_WORKER_DEPLOYMENT`).
    """
    base = Path(rubrics_dir) if rubrics_dir else Path(__file__).parent / "rubrics"
    path = base / f"{agent_name}_rubric.prompty"
    return RubricEvaluator(
        model_config=model_config,
        prompty_path=path,
        name=f"{agent_name}_rubric",
        pass_threshold=pass_threshold,
    )


def judge_model_config_from_env() -> dict[str, Any] | None:
    """Build a judge model config from the Foundry env vars the eval pipelines share.

    Prefers FOUNDRY_JUDGE_DEPLOYMENT, then FOUNDRY_PROMPT_AGENT_DEPLOYMENT (gpt-4o
    class — supports the classic max_tokens param that built-in evaluators use),
    then FOUNDRY_WORKER_DEPLOYMENT as a last resort.
    Returns None for missing configuration or known incompatible deployment
    names; gate callers must report evaluation as unavailable, not successful.
    """
    endpoint = os.environ.get("FOUNDRY_ENDPOINT")
    if not endpoint:
        return None
    deployment = (
        os.environ.get("FOUNDRY_JUDGE_DEPLOYMENT")
        or os.environ.get("FOUNDRY_PROMPT_AGENT_DEPLOYMENT")
        or os.environ.get("FOUNDRY_WORKER_DEPLOYMENT")
    )
    if not deployment:
        return None
    # azure-ai-evaluation 1.1.0 sends max_tokens, not max_completion_tokens.
    # An opaque deployment alias still needs operator verification of its model.
    if deployment.lower().startswith(("gpt-5", "o1", "o3", "o4")):
        return None
    return {
        "azure_endpoint": endpoint,
        "azure_deployment": deployment,
        "api_version": os.environ.get("FOUNDRY_API_VERSION", "2024-10-21"),
    }
