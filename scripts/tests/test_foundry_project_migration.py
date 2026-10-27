import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

SPEC = importlib.util.spec_from_file_location(
    "foundry_project_migration", Path(__file__).parents[1] / "migrate-foundry-project.py"
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def migration(*, apply=False, reviewed=False, expected=None, stage="default"):
    instance = module.Migration.__new__(module.Migration)
    instance.account = "/accounts/existing"
    instance.args = SimpleNamespace(
        stage=stage, target="project-b", previous_default="project-a",
        apply=apply, default_callers_reviewed=reviewed, expected_default=expected,
    )
    return instance


def test_default_is_dry_run_without_apply():
    instance = migration()
    instance.request = Mock(side_effect=[
        {"properties": {}}, {"properties": {"defaultProject": "project-a"}}
    ])
    assert instance.set_default() == {
        "planned_default": "project-b", "current_default": "project-a"
    }
    assert all(call.args[0] == "GET" for call in instance.request.call_args_list)


def test_default_requires_review_of_existing_callers():
    instance = migration(apply=True)
    instance.request = Mock(side_effect=[
        {"properties": {}}, {"properties": {"defaultProject": "project-a"}}
    ])
    with pytest.raises(ValueError, match="Review default-routed"):
        instance.set_default()
    assert all(call.args[0] == "GET" for call in instance.request.call_args_list)


def test_default_refuses_concurrent_default_change():
    instance = migration(apply=True, reviewed=True, expected="project-a")
    instance.request = Mock(side_effect=[
        {"properties": {}}, {"properties": {"defaultProject": "someone-elses-project"}}
    ])
    with pytest.raises(ValueError, match="Default changed concurrently"):
        instance.set_default()
    assert all(call.args[0] == "GET" for call in instance.request.call_args_list)


def test_minimal_default_patch_and_effective_project_flag():
    instance = migration(apply=True, reviewed=True, expected="project-a")
    instance.request = Mock(side_effect=[
        {"properties": {}},
        {"properties": {"defaultProject": "project-a"}},
        {},
        {"properties": {"defaultProject": "project-b"}},
        {"properties": {"isDefault": True}},
    ])
    result = instance.set_default()
    assert result["project_is_default"] is True
    assert result["previous_default"] == "project-a"
    patches = [call for call in instance.request.call_args_list if call.args[0] == "PATCH"]
    assert len(patches) == 1
    assert patches[0].args[2] == {"properties": {"defaultProject": "project-b"}}


def test_rollback_targets_original_default_without_deleting_projects():
    instance = migration(apply=True, reviewed=True, stage="rollback")
    instance.request = Mock(side_effect=[
        {"properties": {}},
        {"properties": {"defaultProject": "project-b"}},
        {},
        {"properties": {"defaultProject": "project-a"}},
        {"properties": {"isDefault": True}},
    ])
    assert instance.set_default()["default_project"] == "project-a"
    assert not any(call.args[0] == "DELETE" for call in instance.request.call_args_list)


def test_source_and_target_projects_must_be_named_explicitly(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "migrate-foundry-project.py", "inspect", "--subscription", "s", "--tenant", "t",
        "--resource-group", "r", "--account", "a",
    ])
    with pytest.raises(SystemExit) as raised:
        module.main()
    assert raised.value.code == 2
    assert "--source" in capsys.readouterr().err
