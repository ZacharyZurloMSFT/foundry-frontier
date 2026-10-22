import hashlib
import logging
from types import SimpleNamespace

import pytest
from app.diagnostics import log_failure

PRIVATE = "private customer question about claim CLM-2001"
SESSION = "asst-concierge-private-session-id"


class Failure(Exception):
    status_code = 403
    code = -32603


def logged(caplog, error, correlation=SESSION):
    log = logging.getLogger("tests.diagnostics")
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger=log.name):
        log_failure(log, "assistant", "model_call", error, correlation)
    assert len(caplog.records) == 1
    return caplog.records[0]


def test_records_allow_listed_metadata_and_never_the_exception_text_or_a_traceback(caplog):
    try:
        raise Failure(PRIVATE)
    except Failure as error:
        record = logged(caplog, error)
    assert (record.component, record.stage, record.error_class) == ("assistant", "model_call", "Failure")
    assert record.http_status == 403 and record.rpc_code == -32603
    assert record.correlation_hash == hashlib.sha256(SESSION.encode()).hexdigest()[:16]
    assert record.exc_info is None and record.stack_info is None
    assert PRIVATE not in caplog.text and PRIVATE not in str(record.__dict__)
    assert SESSION not in caplog.text and SESSION not in str(record.__dict__)


@pytest.mark.parametrize(("error", "status"), [
    (type("FromResponse", (Exception,), {"response": SimpleNamespace(status_code=429)})(PRIVATE), 429),
    (type("FromData", (Exception,), {"data": {"statusCode": 404}})(PRIVATE), 404),
    (type("FromSnakeData", (Exception,), {"data": {"status_code": 502}})(PRIVATE), 502),
    (RuntimeError(PRIVATE), None),
    (object(), None),
])
def test_the_status_comes_from_the_error_not_its_text(caplog, error, status):
    record = logged(caplog, error)
    assert record.http_status == status and record.rpc_code is None
    assert record.error_class == type(error).__name__ and PRIVATE not in caplog.text


@pytest.mark.parametrize("value", [99, 600, True, "403", 403.0, None])
def test_an_implausible_http_status_is_dropped(caplog, value):
    assert logged(caplog, type("Odd", (Exception,), {"status_code": value})()).http_status is None


@pytest.mark.parametrize("value", [32768, -32769, True, "x", 1.5, None])
def test_an_implausible_rpc_code_is_dropped(caplog, value):
    assert logged(caplog, type("Odd", (Exception,), {"code": value})()).rpc_code is None


def test_the_correlation_is_hashed_or_absent(caplog):
    assert logged(caplog, RuntimeError(), correlation="").correlation_hash == "none"
    hashed = logged(caplog, RuntimeError()).correlation_hash
    assert len(hashed) == 16 and hashed != SESSION
