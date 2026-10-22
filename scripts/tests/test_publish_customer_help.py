import copy
import hashlib
import importlib.util
import json
import re
import shutil
import sys
import types
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("publish_customer_help", ROOT / "scripts" / "publish-customer-help.py")
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)

SEED = Path("data") / "northwind" / "departments" / "employee-help-desk" / "seed" / "customer"
KEY = "explicit-memory-only"
HOST = "srch-zurlo-northwind.search.windows.net"
ARTICLES = {
    "claims-lifecycle.md": "HELP-CLAIMS-001", "report-a-loss.md": "HELP-CLAIMS-002",
    "evidence-checklist.md": "HELP-EVIDENCE-001", "how-documents-are-reviewed.md": "HELP-EVIDENCE-002",
    "deductibles-and-limits.md": "HELP-COVERAGE-001", "reading-your-policy.md": "HELP-COVERAGE-002",
    "rental-and-towing.md": "HELP-RENTAL-001", "billing-and-payments.md": "HELP-BILLING-001",
    "privacy-and-your-data.md": "HELP-PRIVACY-001", "contacting-us.md": "HELP-CONTACT-001",
    "glossary.md": "HELP-GLOSSARY-001", "using-the-assistants.md": "HELP-ASSIST-001",
}
TOPICS = ["assistants", "billing", "claims", "contact", "coverage", "evidence", "glossary", "privacy", "rental"]
DOCUMENT_FIELDS = ["id", "title", "content", "source_uri", "version", "topic", "effective_from", "audience",
                   "publisher", "business_domain", "synthetic"]
FOREIGN_FILTER = ("publisher ne 'northwind-help-center-customer-v1' or business_domain ne 'insurance' "
                  "or synthetic ne true or audience ne 'customer'")


@pytest.fixture
def corpus(tmp_path):
    shutil.copytree(ROOT / SEED, tmp_path / SEED)
    return tmp_path


def swap(old, new):
    def change(text):
        assert old in text, "the corruption must apply to the article"
        return text.replace(old, new, 1)
    return change


def retitle(heading):
    def change(text):
        return re.sub(r"(?m)^# .*$", lambda _match: heading, text, count=1)
    return change


def after_title(extra):
    def change(text):
        return re.sub(r"(?m)^(# .*)$", lambda match: match.group(1) + "\n\n" + extra, text, count=1)
    return change


def resize(size):
    def change(text):
        front, body = re.match(r"(---\n.*?\n---\n)(.*)\Z", text, re.DOTALL).groups()
        body = body.strip()
        return front + body + "\n\n" + "x" * (size - len(body) - 2) + "\n"
    return change


def use_transport(monkeypatch, handle):
    original = httpx.Client
    monkeypatch.setattr(publisher.httpx, "Client",
                        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))


# --- Preview and document model ---------------------------------------------------------------------------

def test_preview_is_offline_writes_nothing_and_reports_twelve_articles(monkeypatch):
    monkeypatch.setenv("HELP_CENTER_PUBLISH_AUTH", "admin_key")
    monkeypatch.setenv("HELP_CENTER_SEARCH_ADMIN_KEY", KEY)
    monkeypatch.setattr(publisher.httpx, "Client", lambda **_: pytest.fail("Preview must not create network clients"))
    monkeypatch.setattr(Path, "write_text", lambda *_args, **_kwargs: pytest.fail("Preview must not write files"))
    monkeypatch.setattr(Path, "write_bytes", lambda *_args, **_kwargs: pytest.fail("Preview must not write files"))
    before = {path.name: path.read_bytes() for path in (ROOT / SEED).glob("*.md")}
    result = publisher.publish()
    assert set(result) == {"mode", "subscription", "resource_group", "search_service", "index", "documents",
                           "topics", "audiences", "content_sha256"}
    assert result["mode"] == "preview" and result["documents"] == 12
    assert result["subscription"] == "137c905b-7734-479e-acc5-ab8222462936"
    assert result["resource_group"] == "rg-northwind-data" and result["search_service"] == "srch-zurlo-northwind"
    assert result["index"] == "nw-insurance-help-customer-v1"
    assert result["topics"] == TOPICS and result["audiences"] == ["customer"]
    assert result["content_sha256"] == hashlib.sha256(
        json.dumps(publisher.documents(), sort_keys=True).encode()).hexdigest()
    assert publisher.publish() == result and KEY not in json.dumps(result)
    assert {path.name: path.read_bytes() for path in (ROOT / SEED).glob("*.md")} == before


def test_cli_preview_prints_one_json_summary_and_exits_zero_without_network(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["publish-customer-help.py"])
    monkeypatch.setattr(publisher.httpx, "Client", lambda **_: pytest.fail("Preview must not create network clients"))
    assert publisher.main() == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["mode"] == "preview" and output.err == ""


def test_documents_have_exactly_the_specified_fields_and_urn_sources():
    docs = publisher.documents()
    assert [doc["id"] for doc in docs] == [ARTICLES[name] for name in sorted(ARTICLES)]
    assert len({doc["title"] for doc in docs}) == len({doc["source_uri"] for doc in docs}) == 12
    for doc in docs:
        assert list(doc) == DOCUMENT_FIELDS
        assert doc["title"].startswith("Fictional Northwind help: ")
        assert doc["content"].startswith("# " + doc["title"] + "\n") and doc["content"] == doc["content"].strip()
        assert "\r" not in doc["content"] and not doc["content"].startswith("---")
        assert doc["version"] == "1" and doc["effective_from"] == "2026-01-01"
        assert doc["source_uri"] == f"urn:northwind:synthetic:help-center:{doc['id']}:v{doc['version']}"
        assert doc["topic"] in publisher.TOPICS and len(doc["content"]) < 12000
        assert (doc["audience"], doc["publisher"], doc["business_domain"], doc["synthetic"]) == (
            "customer", "northwind-help-center-customer-v1", "insurance", True)


def test_index_body_matches_the_customer_help_schema():
    body = publisher.index_body()
    assert set(body) == {"name", "description", "fields"}
    assert body["name"] == "nw-insurance-help-customer-v1" and body["description"] == "northwind-help-center-customer-v1"
    assert [field["name"] for field in body["fields"]] == DOCUMENT_FIELDS
    fields = {field["name"]: field for field in body["fields"]}
    assert fields["id"] == {"name": "id", "type": "Edm.String", "key": True, "filterable": True}
    for name in ("title", "content", "source_uri", "version", "topic", "effective_from", "audience",
                 "publisher", "business_domain"):
        assert fields[name]["type"] == "Edm.String" and "key" not in fields[name]
        assert fields[name]["searchable"] is (name in ("title", "content"))
        assert fields[name]["filterable"] is (name in ("publisher", "business_domain", "audience", "topic"))
    assert fields["synthetic"] == {"name": "synthetic", "type": "Edm.Boolean", "filterable": True}


def test_publisher_parses_front_matter_without_a_yaml_dependency():
    source = (ROOT / "scripts" / "publish-customer-help.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(?:import|from)\s+yaml\b", source, re.MULTILINE)


def test_front_matter_parser_strips_quotes_and_accepts_bom_and_crlf(corpus):
    path = corpus / SEED / "glossary.md"
    text = path.read_text(encoding="utf-8")
    text = text.replace('version: "1"', "version: '1'").replace("effective_from: 2026-01-01",
                                                               'effective_from: "2026-01-01"')
    path.write_bytes(b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode())
    doc = next(doc for doc in publisher.documents(corpus) if doc["id"] == "HELP-GLOSSARY-001")
    assert doc["version"] == "1" and doc["effective_from"] == "2026-01-01"
    assert "\r" not in doc["content"] and doc["content"].startswith("# Fictional Northwind help: ")


def test_readme_and_non_markdown_files_are_not_articles(corpus):
    (corpus / SEED / "README.md").write_text("Not an article, and without front matter.", encoding="utf-8")
    (corpus / SEED / "notes.txt").write_text("Not markdown.", encoding="utf-8")
    assert [doc["id"] for doc in publisher.documents(corpus)] == [doc["id"] for doc in publisher.documents()]


def test_publish_reads_the_requested_root_and_hash_tracks_content(corpus):
    assert publisher.publish(root=corpus)["content_sha256"] == publisher.publish()["content_sha256"]
    path = corpus / SEED / "glossary.md"
    path.write_text(path.read_text(encoding="utf-8") + "\nAnother plain sentence.\n", encoding="utf-8")
    assert publisher.publish(root=corpus)["content_sha256"] != publisher.publish()["content_sha256"]


# --- Strict corpus validation -----------------------------------------------------------------------------

CORRUPTIONS = [
    pytest.param(swap("topic: claims", "topic: sales"), "topic", id="topic-not-allowed"),
    pytest.param(swap('version: "1"', 'version: "v1"'), "version", id="version-with-prefix"),
    pytest.param(swap('version: "1"', 'version: "1."'), "version", id="version-trailing-dot"),
    pytest.param(swap('version: "1"', 'version: ""'), "version", id="version-empty"),
    pytest.param(swap('version: "1"', 'version: "' + "1." * 17 + '1"'), "version", id="version-too-long"),
    pytest.param(swap("effective_from: 2026-01-01", "effective_from: 01/01/2026"), "effective_from", id="date-not-iso"),
    pytest.param(swap("effective_from: 2026-01-01", "effective_from: 2026-02-30"), "effective_from",
                 id="date-impossible"),
    pytest.param(swap("id: HELP-CLAIMS-001", "id: help-claims-001"), "id must match", id="id-lowercase"),
    pytest.param(swap("id: HELP-CLAIMS-001", "id: HELP-CLAIMS-1"), "id must match", id="id-short-number"),
    pytest.param(retitle("# Northwind help: A different prefix"), "H1", id="h1-wrong-prefix"),
    pytest.param(lambda text: text.replace("\n# Fictional", "\n## Fictional", 1), "H1", id="h1-demoted"),
    pytest.param(retitle("# Fictional Northwind help: "), "H1", id="h1-without-title"),
    pytest.param(retitle("# Fictional Northwind help: " + "A" * 55), "title must be at most 80",
                 id="title-too-long"),
    pytest.param(after_title("# Second heading"), "only one H1", id="second-h1"),
    pytest.param(after_title("<b>Bold</b> text."), "HTML", id="html-tag"),
    pytest.param(after_title("<!-- hidden -->"), "HTML", id="html-comment"),
    pytest.param(after_title("Read [the guide](https://example.invalid/guide)."), "links", id="markdown-link"),
    pytest.param(after_title("See https://example.invalid/guide."), "links", id="bare-url"),
    pytest.param(after_title("See www.example.invalid for more."), "links", id="www-url"),
    pytest.param(after_title("![diagram](diagram.png)"), "links", id="image"),
    pytest.param(after_title("Coverage is approved."), "decision", id="forbidden-coverage-phrase"),
    pytest.param(after_title("YOUR CLAIM IS APPROVED."), "decision", id="forbidden-claim-phrase-uppercase"),
    pytest.param(swap("topic: claims\n", ""), "front matter", id="front-matter-missing-key"),
    pytest.param(swap("topic: claims\n", "topic: claims\naudience: customer\n"), "front matter",
                 id="front-matter-extra-key"),
    pytest.param(swap("topic: claims\n", "topic: claims\ntopic: claims\n"), "front matter",
                 id="front-matter-duplicate-key"),
    pytest.param(swap("topic: claims\n", "topic claims\n"), "front matter", id="front-matter-not-key-value"),
    pytest.param(swap("effective_from: 2026-01-01\n---\n", "effective_from: 2026-01-01\n"), "front matter",
                 id="front-matter-unclosed"),
    pytest.param(lambda text: text.split("\n---\n", 1)[1], "front matter", id="front-matter-missing"),
    pytest.param(lambda text: text[:text.index("# Fictional")] + "# Fictional Northwind help: Empty\n", "empty",
                 id="body-empty"),
    pytest.param(resize(12000), "under 12000", id="body-at-limit"),
    pytest.param(resize(20000), "under 12000", id="body-oversized"),
]


@pytest.mark.parametrize(("change", "message"), CORRUPTIONS)
def test_invalid_articles_are_rejected_with_a_short_error(corpus, change, message):
    path = corpus / SEED / "claims-lifecycle.md"
    path.write_text(change(path.read_text(encoding="utf-8")), encoding="utf-8")
    with pytest.raises(ValueError, match=message) as error:
        publisher.documents(corpus)
    assert len(str(error.value)) < 300 and "claims-lifecycle.md" in str(error.value)
    with pytest.raises(ValueError, match=message):
        publisher.publish(root=corpus)


def test_body_just_under_the_limit_is_accepted(corpus):
    path = corpus / SEED / "claims-lifecycle.md"
    path.write_text(resize(11999)(path.read_text(encoding="utf-8")), encoding="utf-8")
    doc = next(doc for doc in publisher.documents(corpus) if doc["id"] == "HELP-CLAIMS-001")
    assert len(doc["content"]) == 11999


def test_title_and_version_at_the_runtime_limits_are_accepted(corpus):
    path = corpus / SEED / "claims-lifecycle.md"
    text = retitle("# Fictional Northwind help: " + "A" * 54)(path.read_text(encoding="utf-8"))
    path.write_text(swap('version: "1"', 'version: "' + "1." * 15 + '11"')(text), encoding="utf-8")
    doc = next(doc for doc in publisher.documents(corpus) if doc["id"] == "HELP-CLAIMS-001")
    assert len(doc["title"]) == 80 and len(doc["version"]) == 32
    assert doc["source_uri"] == f"urn:northwind:synthetic:help-center:HELP-CLAIMS-001:v{doc['version']}"


def test_missing_article_is_rejected(corpus):
    (corpus / SEED / "glossary.md").unlink()
    with pytest.raises(ValueError, match=r"exactly the 12 expected articles; missing \['HELP-GLOSSARY-001'\], "
                                          r"unexpected \[\]"):
        publisher.documents(corpus)


def test_extra_article_is_rejected(corpus):
    text = (corpus / SEED / "claims-lifecycle.md").read_text(encoding="utf-8")
    (corpus / SEED / "extra.md").write_text(text.replace("HELP-CLAIMS-001", "HELP-CLAIMS-099"), encoding="utf-8")
    with pytest.raises(ValueError, match=r"missing \[\], unexpected \['HELP-CLAIMS-099'\]"):
        publisher.documents(corpus)


def test_duplicate_article_id_is_rejected(corpus):
    shutil.copy(corpus / SEED / "claims-lifecycle.md", corpus / SEED / "claims-lifecycle-copy.md")
    with pytest.raises(ValueError, match="must be unique"):
        publisher.documents(corpus)


def test_reused_id_in_a_different_article_is_rejected(corpus):
    path = corpus / SEED / "report-a-loss.md"
    path.write_text(path.read_text(encoding="utf-8").replace("HELP-CLAIMS-002", "HELP-CLAIMS-001"), encoding="utf-8")
    with pytest.raises(ValueError, match="must be unique"):
        publisher.documents(corpus)


def test_empty_and_missing_directories_are_rejected(corpus, tmp_path_factory):
    for path in (corpus / SEED).glob("*.md"):
        path.unlink()
    with pytest.raises(ValueError, match="exactly the 12 expected articles"):
        publisher.documents(corpus)
    with pytest.raises(ValueError, match="directory is missing"):
        publisher.documents(tmp_path_factory.mktemp("no-corpus"))


# --- Real articles ----------------------------------------------------------------------------------------

WORD = re.compile(r"[A-Za-z0-9$]+(?:['.,-][A-Za-z0-9$]+)*")
DECISION_PHRASES = re.compile(r"\b(?:approved|denied|will pay|guaranteed)\b", re.IGNORECASE)
NEGATION = re.compile(r"\b(?:no|not|never|nothing|neither|nor|cannot|without)\b|n't\b", re.IGNORECASE)
CONTRAST = re.compile(r"\b(?:but|however|although|though|except|unless|yet)\b", re.IGNORECASE)
DANGLING_OPENER = re.compile(
    r"^(?:It|They|These|Those|Such|Also|However|Then|Otherwise|Additionally|Instead)\b"
    r"|^(?:This|That)\s+(?:is|means|helps|does|can|will|may|was)\b")
MARKUP = re.compile(r"<(?:[A-Za-z]|/[A-Za-z]|!|\?)|\]\(|\]\[|://|\bwww\.|^\s*\|", re.MULTILINE)


def asserted_decisions(text):
    """Decision phrases that no explicit negation governs earlier in the same sentence."""
    found = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        for match in DECISION_PHRASES.finditer(sentence):
            before = sentence[:match.start()]
            negations = list(NEGATION.finditer(before))
            if negations and not CONTRAST.search(before[negations[-1].end():]):
                continue
            found.append(sentence.strip())
            break
    return found


@pytest.mark.parametrize("sentence", [
    "Nothing is approved, denied, settled or paid automatically.",
    "The portal never says that an amount is guaranteed.",
    "A schedule figure is not a guaranteed amount.",
    "The assistants do not decide anything and cannot say that a loss is approved.",
])
def test_decision_check_allows_explicit_negations(sentence):
    assert asserted_decisions(sentence) == []


@pytest.mark.parametrize("sentence", [
    "Your claim is approved.",
    "The claim was denied after review.",
    "We will pay the repair shop.",
    "Payment is guaranteed.",
    "APPROVED.",
    "The portal does not decide coverage, but your claim is approved.",
    "The portal does not send email. Your claim is approved.",
])
def test_decision_check_flags_assertions(sentence):
    assert asserted_decisions(sentence)


def test_shipped_directory_holds_exactly_the_twelve_articles():
    assert sorted(path.name for path in (ROOT / SEED).iterdir()) == sorted(ARTICLES)
    docs = {doc["id"]: doc for doc in publisher.documents()}
    assert sorted(docs) == sorted(ARTICLES.values())
    assert sorted({doc["topic"] for doc in docs.values()}) == TOPICS == sorted(publisher.TOPICS)


@pytest.mark.parametrize("name", sorted(ARTICLES))
def test_real_article_is_plain_self_contained_help_text(name):
    text = (ROOT / SEED / name).read_text(encoding="utf-8")
    doc = next(doc for doc in publisher.documents() if doc["id"] == ARTICLES[name])
    assert text.isascii() and "\t" not in text and "\ufeff" not in text
    body = doc["content"]
    lines = [line for line in body.splitlines() if line.strip()]
    prose = [line for line in lines if not line.startswith("#")]
    words = len(WORD.findall(re.sub(r"(?m)^\s*(?:[-*]|\d+\.)\s+", "", "\n".join(lines[1:]))))
    assert 250 <= words <= 450 and len(body) < 12000
    assert 3 <= sum(line.startswith("## ") for line in lines) <= 8
    assert not MARKUP.search(body) and not any(line.startswith("###") for line in lines)
    assert asserted_decisions(body) == []
    for line in prose:
        item = re.sub(r"^\s*(?:[-*]|\d+\.)\s+", "", line)
        assert item.endswith(".") and len(item) <= 240, f"Keep each sentence on one short line: {item}"
        assert not any(DANGLING_OPENER.search(sentence) for sentence in re.split(r"(?<=[.!?])\s+", item)), item
    closing = prose[-1]
    assert closing.count(".") == 1 and not closing.startswith(("-", "*"))
    assert "synthetic demonstration" in closing and "not a coverage decision" in closing


# --- Apply: isolated index, ownership guards and verified upload ------------------------------------------

@pytest.fixture
def mock_search(monkeypatch):
    monkeypatch.setenv("HELP_CENTER_PUBLISH_AUTH", "admin_key")
    monkeypatch.setenv("HELP_CENTER_SEARCH_ADMIN_KEY", KEY)
    calls = []

    def install(existing=None, foreign=False, results=None):
        def handle(request):
            calls.append(request)
            if request.method == "GET":
                return httpx.Response(404 if existing is None else 200, json=existing)
            body = json.loads(request.content)
            if request.method == "PUT":
                assert request.headers["if-none-match"] == "*"
                return httpx.Response(201, json=body)
            if request.url.path.endswith("/docs/search"):
                return httpx.Response(200, json={"value": [{"id": "foreign"}] if foreign else []})
            assert request.url.path.endswith("/docs/index")
            rows = [{"key": doc["id"], "status": True, "statusCode": 201} for doc in body["value"]]
            return httpx.Response(200, json={"value": rows if results is None else results(rows)})
        use_transport(monkeypatch, handle)
        return calls
    return install


@pytest.fixture
def entra(monkeypatch):
    identity = pytest.importorskip("azure.identity")
    events = []

    class FakeCredential:
        def __init__(self, **kwargs):
            events.append(("created", kwargs))

        def get_token(self, scope):
            events.append(("scope", scope))
            return types.SimpleNamespace(token="entra-token-in-memory")

        def close(self):
            events.append(("closed",))

    monkeypatch.setattr(identity, "AzureDeveloperCliCredential", FakeCredential)
    return events


def test_apply_creates_a_fresh_owned_index_and_uploads_all_twelve(mock_search):
    calls = mock_search()
    result = publisher.publish(apply=True)
    assert result["mode"] == "applied" and result["documents"] == 12
    assert [request.method for request in calls] == ["GET", "PUT", "POST"]
    assert all(request.url.scheme == "https" and request.url.host == HOST for request in calls)
    assert all(request.url.path.startswith("/indexes/nw-insurance-help-customer-v1") for request in calls)
    assert all(request.url.params["api-version"] == "2026-05-01-preview" for request in calls)
    assert all(request.method != "DELETE" for request in calls)
    assert json.loads(calls[1].content) == publisher.index_body()
    rows = json.loads(calls[2].content)["value"]
    assert [row["@search.action"] for row in rows] == ["upload"] * 12
    assert [{key: value for key, value in row.items() if key != "@search.action"} for row in rows] == publisher.documents()


def test_admin_key_is_sent_only_as_the_explicit_api_key_header(mock_search):
    calls = mock_search()
    publisher.publish(apply=True)
    assert all(request.headers["api-key"] == KEY and "authorization" not in request.headers for request in calls)
    assert all(KEY not in str(request.url) and KEY.encode() not in request.content for request in calls)


def test_existing_owned_index_is_checked_for_foreign_documents_before_upload(mock_search):
    calls = mock_search(publisher.index_body())
    assert publisher.publish(apply=True)["mode"] == "applied"
    assert [request.method for request in calls] == ["GET", "POST", "POST"]
    assert calls[1].url.path.endswith("/docs/search") and calls[2].url.path.endswith("/docs/index")
    assert json.loads(calls[1].content) == {"search": "*", "top": 1, "select": "id", "filter": FOREIGN_FILTER}


def test_service_added_field_defaults_do_not_make_the_owned_index_incompatible(mock_search):
    existing = {"@odata.etag": '"0x1"', **publisher.index_body()}
    for field in existing["fields"]:
        field.update({"retrievable": True, "sortable": False, "facetable": False, "analyzer": None})
    calls = mock_search(existing)
    assert publisher.publish(apply=True)["mode"] == "applied"
    assert [request.method for request in calls] == ["GET", "POST", "POST"]


@pytest.mark.parametrize("corruption", [
    "owner", "name", "no-fields", "missing-field", "extra-field", "flag", "type", "key", "malformed", "foreign",
])
def test_existing_index_guard_blocks_every_write(mock_search, corruption):
    existing = copy.deepcopy(publisher.index_body())
    fields = {field["name"]: field for field in existing["fields"]}
    if corruption == "owner":
        existing["description"] = "someone-else"
    elif corruption == "name":
        existing["name"] = "other-index"
    elif corruption == "no-fields":
        existing["fields"] = []
    elif corruption == "missing-field":
        existing["fields"].pop()
    elif corruption == "extra-field":
        existing["fields"].append({"name": "extra", "type": "Edm.String"})
    elif corruption == "flag":
        fields["audience"]["filterable"] = False
    elif corruption == "type":
        fields["topic"]["type"] = "Edm.Int32"
    elif corruption == "key":
        fields["id"]["key"] = False
    elif corruption == "malformed":
        existing = ["not", "an", "index"]
    calls = mock_search(existing, foreign=corruption == "foreign")
    with pytest.raises(RuntimeError, match="no writes performed"):
        publisher.publish(apply=True)
    assert not any(request.method in ("PUT", "DELETE") or request.url.path.endswith("/docs/index") for request in calls)


def _fault(kind):
    def change(rows):
        if kind == "null":
            return None
        if kind == "empty":
            return []
        if kind == "short":
            return rows[:-1]
        if kind == "failed":
            rows[3]["status"] = False
        elif kind == "bad-code":
            rows[3]["statusCode"] = 500
        elif kind == "foreign-key":
            rows[3]["key"] = "HELP-OTHER-001"
        elif kind == "duplicate-key":
            rows[3]["key"] = rows[2]["key"]
        return rows
    return change


@pytest.mark.parametrize("kind", ["null", "empty", "short", "failed", "bad-code", "foreign-key", "duplicate-key"])
def test_partial_upload_is_not_reported_as_success(mock_search, kind):
    mock_search(results=_fault(kind))
    with pytest.raises(RuntimeError, match="incomplete"):
        publisher.publish(apply=True)


def test_explicit_admin_key_is_required_without_fallback(monkeypatch):
    monkeypatch.setenv("HELP_CENTER_PUBLISH_AUTH", "admin_key")
    monkeypatch.delenv("HELP_CENTER_SEARCH_ADMIN_KEY", raising=False)
    monkeypatch.setattr(publisher.httpx, "Client", lambda **_: pytest.fail("No request may be sent without a key"))
    with pytest.raises(ValueError, match="no automatic key lookup"):
        publisher.publish(apply=True)


@pytest.mark.parametrize("mode", ["", "key", "ENTRA_ID", "az_cli"])
def test_unknown_publisher_auth_is_rejected(monkeypatch, mode):
    monkeypatch.setenv("HELP_CENTER_PUBLISH_AUTH", mode)
    monkeypatch.setenv("HELP_CENTER_SEARCH_ADMIN_KEY", KEY)
    monkeypatch.setattr(publisher.httpx, "Client", lambda **_: pytest.fail("No request may be sent"))
    with pytest.raises(ValueError, match="explicitly be entra_id or admin_key"):
        publisher.publish(apply=True)


def test_entra_id_is_the_default_and_never_sends_a_search_key(mock_search, entra, monkeypatch):
    calls = mock_search()
    monkeypatch.delenv("HELP_CENTER_PUBLISH_AUTH")
    monkeypatch.delenv("HELP_CENTER_SEARCH_ADMIN_KEY")
    assert publisher.publish(apply=True)["mode"] == "applied"
    assert entra == [("created", {"tenant_id": publisher.TENANT}),
                     ("scope", "https://search.azure.com/.default"), ("closed",)]
    assert all(request.headers["authorization"] == "Bearer entra-token-in-memory"
               and "api-key" not in request.headers for request in calls)


def test_entra_credential_is_closed_when_the_service_rejects_the_request(entra, monkeypatch):
    monkeypatch.setenv("HELP_CENTER_PUBLISH_AUTH", "entra_id")
    use_transport(monkeypatch, lambda request: httpx.Response(403, json={"error": "private server response"}))
    with pytest.raises(RuntimeError, match=r"preflight GET failed \(HTTP 403\)"):
        publisher.publish(apply=True)
    assert entra[-1] == ("closed",)


def test_redirects_are_never_followed(monkeypatch):
    monkeypatch.setenv("HELP_CENTER_PUBLISH_AUTH", "admin_key")
    monkeypatch.setenv("HELP_CENTER_SEARCH_ADMIN_KEY", KEY)
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(307, headers={"location": "https://redirect.invalid/capture"})

    use_transport(monkeypatch, handle)
    with pytest.raises(RuntimeError, match=r"preflight GET failed \(HTTP 307\)"):
        publisher.publish(apply=True)
    assert [request.url.host for request in calls] == [HOST]


def test_preflight_failure_includes_http_status_without_writes(monkeypatch):
    monkeypatch.setenv("HELP_CENTER_PUBLISH_AUTH", "admin_key")
    monkeypatch.setenv("HELP_CENTER_SEARCH_ADMIN_KEY", KEY)
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(403, json={"error": "private server response"})

    use_transport(monkeypatch, handle)
    with pytest.raises(RuntimeError, match=r"preflight GET failed \(HTTP 403\)") as error:
        publisher.publish(apply=True)
    assert "private server response" not in str(error.value)
    assert [request.method for request in calls] == ["GET"]


# --- CLI: exit codes and bounded, secret-free errors ------------------------------------------------------

@pytest.mark.parametrize(("argv", "expected"), [([], False), (["--apply"], True)])
def test_apply_flag_is_the_only_way_to_write(monkeypatch, capsys, argv, expected):
    seen = []

    def fake(**kwargs):
        seen.append(kwargs)
        return {"mode": "stub"}

    monkeypatch.setattr(publisher, "publish", fake)
    monkeypatch.setattr(sys, "argv", ["publish-customer-help.py", *argv])
    assert publisher.main() == 0
    assert seen == [{"apply": expected}] and json.loads(capsys.readouterr().out) == {"mode": "stub"}


def test_cli_apply_reports_success_without_printing_the_key(mock_search, monkeypatch, capsys):
    mock_search()
    monkeypatch.setattr(sys, "argv", ["publish-customer-help.py", "--apply"])
    assert publisher.main() == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["mode"] == "applied" and output.err == ""
    assert KEY not in output.out + output.err


def test_service_errors_expose_only_the_status_never_bodies_keys_or_urls(monkeypatch, capsys):
    monkeypatch.setenv("HELP_CENTER_PUBLISH_AUTH", "admin_key")
    monkeypatch.setenv("HELP_CENTER_SEARCH_ADMIN_KEY", KEY)

    def handle(request):
        if request.method == "GET":
            return httpx.Response(404)
        return httpx.Response(403, json={"error": {"message": "private-server-body " + KEY}},
                              headers={"x-secret-header": KEY})

    use_transport(monkeypatch, handle)
    with pytest.raises(RuntimeError) as error:
        publisher.publish(apply=True)
    monkeypatch.setattr(sys, "argv", ["publish-customer-help.py", "--apply"])
    assert publisher.main() == 1
    output = capsys.readouterr()
    assert output.out == "" and "HTTP 403" in str(error.value) and "HTTP 403" in output.err
    for text in (str(error.value), output.err):
        assert all(secret not in text for secret in
                   (KEY, "private-server-body", "x-secret-header", "https://", "api-key"))


@pytest.mark.parametrize("error", [
    RuntimeError("Help Center Search POST /indexes/owned/docs/index failed (HTTP 403); no secret details logged."),
    RuntimeError("Existing help index contains foreign documents; no writes performed."),
    ValueError("Explicit publisher admin key is missing; no automatic key lookup or fallback."),
    ValueError("Customer help must contain exactly the 12 expected articles; missing [], unexpected []."),
    FileNotFoundError("Canonical source file is missing."),
])
def test_cli_preserves_actionable_safe_error_classes_and_messages(monkeypatch, capsys, error):
    monkeypatch.setattr(sys, "argv", ["publish-customer-help.py", "--apply"])

    def fail(**kwargs):
        assert kwargs == {"apply": True}
        raise error

    monkeypatch.setattr(publisher, "publish", fail)
    assert publisher.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert type(error).__name__ in output.err and str(error) in output.err
    assert '"mode": "applied"' not in output.err


@pytest.mark.parametrize("kind", ["http_status", "transport", "authentication"])
def test_cli_exposes_safe_status_without_server_bodies_headers_or_secrets(monkeypatch, capsys, kind):
    request = httpx.Request("POST", "https://search.unit.test/private?api-key=secret-key",
                            headers={"Authorization": "Bearer secret-token"})
    if kind == "http_status":
        error = httpx.HTTPStatusError(
            "server body and api-key=secret-key", request=request,
            response=httpx.Response(403, request=request, json={"error": "secret-server-body"}),
        )
    elif kind == "transport":
        error = httpx.ConnectError("Authorization: Bearer secret-token; api-key=secret-key", request=request)
    else:
        class AuthenticationFailure(Exception):
            status_code = 403
        error = AuthenticationFailure("secret-server-body; Bearer secret-token")

    def fail(**kwargs):
        raise error

    monkeypatch.setattr(publisher, "publish", fail)
    monkeypatch.setattr(sys, "argv", ["publish-customer-help.py", "--apply"])
    assert publisher.main() == 1
    output = capsys.readouterr()
    assert output.out == "" and type(error).__name__ in output.err
    if kind != "transport":
        assert "HTTP 403" in output.err
    assert all(secret not in output.err for secret in
               ("secret-key", "secret-token", "secret-server-body", "Authorization", "https://"))


def test_cli_omits_the_details_of_unexpected_exceptions(monkeypatch, capsys):
    def fail(**kwargs):
        raise KeyError("secret-token")

    monkeypatch.setattr(publisher, "publish", fail)
    monkeypatch.setattr(sys, "argv", ["publish-customer-help.py", "--apply"])
    assert publisher.main() == 1
    output = capsys.readouterr()
    assert output.out == "" and "KeyError" in output.err and "Customer help publishing failed" in output.err
    assert "secret-token" not in output.err
