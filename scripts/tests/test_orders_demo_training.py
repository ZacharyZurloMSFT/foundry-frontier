import copy
import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "orders_demo_training", Path(__file__).parents[1] / "submit-orders-approved-demo-training.py"
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def fixtures():
    approval = {
        "authorization": {"synthetic_labels_accepted_for_demo": True, "global_processing_permitted": True,
                          "hourly_custom_model_hosting_permitted": False, "max_incremental_cost_usd": 10},
        "account": "aisvc-zurlo-northwind", "training_project": "nw-claims-warranty",
        "training": {"tier": "Developer", "epochs": 1, "max_experiments": 1,
                     "conservative_training_cost_usd": 1.26, "reserved_cost_usd": 2,
                     "model": "gpt-4.1-mini", "version": "2025-04-14"},
    }
    imported = {"account": "aisvc-zurlo-northwind", "project": "nw-claims-warranty",
                "local_file_uploaded": False, "files": [
                    {"dataset": "01-triage-training", "records": 200, "id": "file-train"},
                    {"dataset": "02-triage-validation", "records": 50, "id": "file-validation"},
                ]}
    return approval, imported


def test_exact_single_epoch_developer_plan_uses_imported_files():
    approval, imported = fixtures()
    plan = module.approved_plan(approval, imported)
    assert plan["training_file"] == "file-train"
    assert plan["validation_file"] == "file-validation"
    assert plan["extra_body"] == {"trainingType": "developerTier"}
    assert plan["method"]["supervised"]["hyperparameters"]["n_epochs"] == 1


@pytest.mark.parametrize("field,value", [("tier", "GlobalStandard"), ("epochs", 3),
                                       ("max_experiments", 2), ("conservative_training_cost_usd", 11)])
def test_training_cannot_expand_the_authorized_scope(field, value):
    approval, imported = fixtures()
    approval["training"][field] = value
    with pytest.raises(ValueError):
        module.approved_plan(approval, imported)


def test_no_local_upload_or_project_fallback():
    approval, imported = fixtures()
    altered = copy.deepcopy(imported)
    altered["local_file_uploaded"] = True
    with pytest.raises(ValueError):
        module.approved_plan(approval, altered)
    altered = copy.deepcopy(imported)
    altered["project"] = "northwind-orders"
    with pytest.raises(ValueError):
        module.approved_plan(approval, altered)
