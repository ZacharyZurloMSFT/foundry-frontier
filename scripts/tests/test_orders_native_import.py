import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "orders_native_import", Path(__file__).parents[1] / "import-orders-native-training.py"
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_native_normalization_does_not_change_record_identity():
    bom = b'\xef\xbb\xbf{"messages":[{"role":"user","content":"fictional"}]}\n'
    normalized = b'{ "messages": [ { "content": "fictional", "role": "user" } ] }\r\n'
    assert module.records(bom) == module.records(normalized)
    assert module.semantic_hash(module.records(bom)) == module.semantic_hash(module.records(normalized))


def test_blob_import_preserves_azure_source_and_encodes_filename():
    source = "https://fictional.blob.core.windows.net/existing?sv=fixture&sig=fixture"
    result = module.blob_url(source, "inputs/order examples.jsonl")
    assert result == (
        "https://fictional.blob.core.windows.net/existing/inputs/order%20examples.jsonl"
        "?sv=fixture&sig=fixture"
    )
    with pytest.raises(ValueError):
        module.blob_url("https://external.invalid/existing?sig=fixture", "data.jsonl")
    with pytest.raises(ValueError):
        module.blob_url("https://fictional.blob.core.windows.net/existing", "data.jsonl")
