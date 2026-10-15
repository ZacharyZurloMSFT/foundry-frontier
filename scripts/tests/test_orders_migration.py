import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

SPEC = importlib.util.spec_from_file_location(
    "orders_migration", Path(__file__).parents[1] / "migrate-orders-project.py"
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def migration(*, apply=False, reviewed=False, expected=None, stage="default"):
    instance = module.Migration.__new__(module.Migration)
    instance.account = "/accounts/existing"
    instance.args = SimpleNamespace(
        stage=stage, target="northwind-orders", previous_default="nw-claims-warranty",
        apply=apply, default_callers_reviewed=reviewed, expected_default=expected,
    )
    return instance


def test_default_is_dry_run_without_apply():
    instance = migration()
    instance.request = Mock(side_effect=[
        {"properties": {}}, {"properties": {"defaultProject": "nw-claims-warranty"}}
    ])
    assert instance.set_default() == {
        "planned_default": "northwind-orders", "current_default": "nw-claims-warranty"
    }
    assert all(call.args[0] == "GET" for call in instance.request.call_args_list)


def test_default_requires_review_of_existing_callers():
    instance = migration(apply=True)
    instance.request = Mock(side_effect=[
        {"properties": {}}, {"properties": {"defaultProject": "nw-claims-warranty"}}
    ])
    with pytest.raises(ValueError, match="Review default-routed"):
        instance.set_default()
    assert all(call.args[0] == "GET" for call in instance.request.call_args_list)


def test_default_refuses_concurrent_default_change():
    instance = migration(apply=True, reviewed=True, expected="nw-claims-warranty")
    instance.request = Mock(side_effect=[
        {"properties": {}}, {"properties": {"defaultProject": "someone-elses-project"}}
    ])
    with pytest.raises(ValueError, match="Default changed concurrently"):
        instance.set_default()
    assert all(call.args[0] == "GET" for call in instance.request.call_args_list)


def test_minimal_default_patch_and_effective_project_flag():
    instance = migration(apply=True, reviewed=True, expected="nw-claims-warranty")
    instance.request = Mock(side_effect=[
        {"properties": {}},
        {"properties": {"defaultProject": "nw-claims-warranty"}},
        {},
        {"properties": {"defaultProject": "northwind-orders"}},
        {"properties": {"isDefault": True}},
    ])
    result = instance.set_default()
    assert result["project_is_default"] is True
    assert result["previous_default"] == "nw-claims-warranty"
    patches = [call for call in instance.request.call_args_list if call.args[0] == "PATCH"]
    assert len(patches) == 1
    assert patches[0].args[2] == {"properties": {"defaultProject": "northwind-orders"}}


def test_rollback_targets_original_default_without_deleting_projects():
    instance = migration(apply=True, reviewed=True, stage="rollback")
    instance.request = Mock(side_effect=[
        {"properties": {}},
        {"properties": {"defaultProject": "northwind-orders"}},
        {},
        {"properties": {"defaultProject": "nw-claims-warranty"}},
        {"properties": {"isDefault": True}},
    ])
    assert instance.set_default()["default_project"] == "nw-claims-warranty"
    assert not any(call.args[0] == "DELETE" for call in instance.request.call_args_list)
