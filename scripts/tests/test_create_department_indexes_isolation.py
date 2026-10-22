"""Customer help articles must never reach the department (HR/IT) search index during provisioning."""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("create_department_indexes", ROOT / "scripts" / "create-department-indexes.py")
indexes = importlib.util.module_from_spec(SPEC)
sys.modules.setdefault("create_department_indexes", indexes)
SPEC.loader.exec_module(indexes)


class RecordingContainer:
    def __init__(self):
        self.names = []

    def upload_blob(self, name, **_):
        self.names.append(name)


class RecordingService:
    def __init__(self):
        self.container = RecordingContainer()

    def get_container_client(self, _name):
        return self.container


def uploaded(dept):
    service = RecordingService()
    count = indexes._upload_department_markdown(service, dept)
    assert count == len(service.container.names)
    return service.container.names


def test_the_hr_department_index_only_receives_top_level_seed_files():
    dept = next(d for d in indexes.load_company().departments if d.slug == "employee-help-desk")
    seed = ROOT / "data" / "northwind" / "departments" / "employee-help-desk" / "seed"
    customer = {path.name for path in (seed / "customer").glob("*.md")}
    top_level = {path.name for path in seed.glob("*.md") if path.name.upper() != "README.MD"}
    assert len(customer) == 12 and top_level and not customer & top_level
    names = uploaded(dept)
    assert {name.split("/", 1)[1] for name in names} == top_level
    assert all(name.startswith("employee-help-desk/") and name.count("/") == 1 for name in names)


def test_every_other_department_still_uploads_its_flat_seed_files():
    for dept in indexes.load_company().departments:
        if dept.slug in indexes._SKIP_SLUGS:
            continue
        flat = {path.name for path in dept.seed_dir.glob("*.md") if path.name.upper() != "README.MD"}
        assert {name.split("/", 1)[1] for name in uploaded(dept)} == flat, dept.slug
