"""Static demo data — the fictional Northwind Order Concierge fixture."""

# Sample customers used by the Order Concierge demo. In production these come
# from Cosmos; here they're static so the demo works offline too.
CUSTOMERS = [
    {"id": "C-8801", "name": "Priya Patel",   "loyalty_tier": "gold",   "email": "priya@example.com",  "phone": "+1-206-555-0141"},
    {"id": "C-9102", "name": "Marcus Chen",   "loyalty_tier": "silver", "email": "marcus@example.com", "phone": "+1-303-555-0178"},
    {"id": "C-7455", "name": "Sofia Alvarez", "loyalty_tier": "bronze", "email": "sofia@example.com",  "phone": "+1-312-555-0192"},
    {"id": "C-6320", "name": "Ethan Wallace", "loyalty_tier": "gold",   "email": "ethan@example.com",  "phone": "+1-404-555-0110"},
    {"id": "C-5188", "name": "Nora Bell",     "loyalty_tier": "silver", "email": "nora@example.com",   "phone": "+1-617-555-0134"},
]

# Product catalog (SKUs) — small on purpose. The full catalog is generated as
# PDFs into data/seed/generated at azd up.
SKUS = [
    {"sku": "SKU-2201", "name": "Northwind Cascade Espresso Machine", "category": "kitchen",   "price": 189.00},
    {"sku": "SKU-1108", "name": "Northwind Cast-Iron Skillet",         "category": "kitchen",   "price":  74.50},
    {"sku": "SKU-3040", "name": "Northwind Merino Throw Blanket",      "category": "home",      "price":  79.99},
    {"sku": "SKU-4501", "name": "Northwind Ceramic Mug (set of 4)",    "category": "kitchen",   "price":  58.00},
    {"sku": "SKU-5115", "name": "Northwind Trailhead Backpack",        "category": "outdoors",  "price": 129.00},
    {"sku": "SKU-6020", "name": "Northwind Sunrise Alarm Clock",       "category": "bedroom",   "price":  49.00},
]

# Representative order fixtures matching the mcp-order-lookup service. The
# order-lookup MCP and the orchestrator's local shortcut both key off these
# same shapes so a live demo behaves identically whether Foundry hits the
# MCP or the in-process fallback fires.
ORDERS = [
    {
        "order_id": "NW-40021",
        "customer_id": "C-8801",
        "status": "in_transit",
        "placed_on": "2026-09-02",
        "eta": "2026-09-10",
        "total": 189.00,
        "items": [{"sku": "SKU-2201", "qty": 1, "price": 189.00}],
    },
    {
        "order_id": "NW-40022",
        "customer_id": "C-9102",
        "status": "delivered",
        "placed_on": "2026-08-15",
        "eta": "2026-08-20",
        "total": 74.50,
        "items": [{"sku": "SKU-1108", "qty": 1, "price": 74.50}],
    },
    {
        "order_id": "NW-40023",
        "customer_id": "C-7455",
        "status": "processing",
        "placed_on": "2026-09-08",
        "eta": "2026-09-14",
        "total": 239.97,
        "items": [{"sku": "SKU-3040", "qty": 3, "price": 79.99}],
    },
]

# Refund-threshold policy used by the demo and eval harness.
REFUND_POLICY = {
    "auto_approve_max_usd": 50,
    "supervisor_review_max_usd": 250,
    # Above supervisor_review_max_usd goes to manager_review. Any goodwill
    # credit always goes to supervisor_review regardless of amount.
    "goodwill_always_supervisor": True,
}

# Standard return windows (days). The seed generator embeds these into the
# generated return-policy PDF so the policy-lookup KB has ground truth.
RETURN_WINDOWS = {
    "kitchen":  30,
    "home":     30,
    "outdoors": 60,
    "bedroom":  30,
    # electronics has a shorter window; kept here in case future SKUs land
    # in that category.
    "electronics": 15,
}
