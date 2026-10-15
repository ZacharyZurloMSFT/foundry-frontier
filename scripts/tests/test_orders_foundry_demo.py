import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "orders_foundry_demo", Path(__file__).parents[1] / "setup-orders-foundry-demo.py"
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_native_evaluation_definition_uses_precomputed_outputs_only():
    definition = module.evaluation_definition()
    assert definition["data_source_config"]["type"] == "custom"
    assert set(definition["data_source_config"]["item_schema"]["required"]) == {
        "query", "response", "ground_truth"
    }
    criterion = definition["testing_criteria"][0]
    assert criterion["evaluator_name"] == "builtin.f1_score"
    assert criterion["data_mapping"]["response"] == "{{item.response}}"
    assert "target" not in definition
    assert "initialization_parameters" not in criterion
