import re

import pytest

LEGACY_VOCABULARY = re.compile(
    r"retail|order[_-]?lookup|orderlookup|order concierge|NW-4\d{4}|refund|reship|\bRMA\b|loyalty|\bSKU\b|shipment"
    r"|propose_resolution|lookup_order|resolution_draft|resolutiondraft|ORDERS_|ORDER_AGENT|ORDER_PROJECT"
    r"|northwind-orders|orders-learning|work.order|hvac|technician", re.IGNORECASE)


@pytest.fixture(scope="session")
def legacy_vocabulary():
    """The retired retail / field-service vocabulary; eval inputs must not contain it."""
    return LEGACY_VOCABULARY
