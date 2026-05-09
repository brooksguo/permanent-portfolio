TOTAL_ASSETS = 1_000_000
START_DATE = "2026-01-01"
REBALANCE_THRESHOLD = 0.05
DB_PATH = "portfolio.db"
REBALANCE_START_DATE = "2026-01-01"

ASSETS = {
    "009051": {"name": "易方达中证红利ETF联接A",  "target": 0.10},
    "024566": {"name": "易方达自由现金流",         "target": 0.10},
    "110017": {"name": "易方达增强回报债券A",      "target": 0.10},
    "013304": {"name": "易方达科创创业50",         "target": 0.05},
    "014519": {"name": "博时恒生港股通高股息率ETF", "target": 0.10},
    "000307": {"name": "易方达黄金ETF联接A",       "target": 0.10},
    "006484": {"name": "广发中债1-3年",            "target": 0.25},
    "110027": {"name": "易方达安心回报债券A",      "target": 0.10},
    "022459": {"name": "易方达中证A500",           "target": 0.10},
}

PYRAMID_RULES = {
    "110017": [
        {"drop": 0.02, "amount": 1000},
    ],
    "110027": [
        {"drop": 0.03, "amount": 1000},
        {"drop": 0.05, "amount": 5000},
    ],
    "009051": [
        {"drop": 0.05, "amount": 5000},
        {"drop": 0.10, "amount": 10000},
        {"drop": 0.15, "amount": 20000},
    ],
    "024566": [
        {"drop": 0.05, "amount": 5000},
        {"drop": 0.10, "amount": 10000},
        {"drop": 0.15, "amount": 20000},
    ],
    "000307": [
        {"drop": 0.10, "amount": 1000},
    ],
    "006484": [
        {"drop": 0.01, "amount": 1000},
    ],
    "022459": [
        {"drop": 0.10, "amount": 5000},
        {"drop": 0.20, "amount": 7000},
        {"drop": 0.30, "amount": 10000},
    ],
}
