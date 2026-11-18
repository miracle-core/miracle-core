# Base prices in INR (India)
INDIA_BASE_INR = {
    "starter_299": {"label": "Starter Trial (7 Days, then ₹3,500/wk)", "inr": 299, "days": 7},
    "camp_1500":   {"label": "Free Health Camp Ad (7 Days)", "inr": 1500, "days": 7},
    "weekly_3500": {"label": "Weekly 10km Spotlight (7 Days)", "inr": 3500, "days": 7},
    "monthly_7500":{"label": "Monthly 10km Growth Plan (30 Days)", "inr": 7500, "days": 30},
}

# Approximate FX rates per 1 INR & Gateway Fee %
# Outside India automatically uses multiplier = 2.0 (2x India rate) + international gateway fee (3.5%)
COUNTRY_CONFIG = {
    "IN": {"name": "India", "currency": "INR", "symbol": "₹", "fx_from_inr": 1.0, "multiplier": 1.0, "gw_pct": 0.02},
    "TR": {"name": "Turkey", "currency": "TRY", "symbol": "₺", "fx_from_inr": 0.42, "multiplier": 2.0, "gw_pct": 0.035},
    "US": {"name": "USA / Global", "currency": "USD", "symbol": "$", "fx_from_inr": 0.0118, "multiplier": 2.0, "gw_pct": 0.035},
    "AE": {"name": "UAE", "currency": "AED", "fx_from_inr": 0.0435, "multiplier": 2.0, "gw_pct": 0.035},
    "EU": {"name": "Europe", "currency": "EUR", "symbol": "€", "fx_from_inr": 0.011, "multiplier": 2.0, "gw_pct": 0.035},
    "GB": {"name": "UK", "currency": "GBP", "symbol": "£", "fx_from_inr": 0.0093, "multiplier": 2.0, "gw_pct": 0.035},
}


def calculate_plan_price(plan_type: str, country_code: str = "IN", include_gateway_fee: bool = True) -> dict:
    if plan_type not in INDIA_BASE_INR:
        raise ValueError("Invalid plan_type")
    cc = country_code.upper() if country_code.upper() in COUNTRY_CONFIG else "US"
    cfg = COUNTRY_CONFIG[cc]
    plan = INDIA_BASE_INR[plan_type]

    base_amount = round(plan["inr"] * cfg["multiplier"] * cfg["fx_from_inr"])
    if base_amount < 1:
        base_amount = 1
    gw_fee = round(base_amount * cfg["gw_pct"]) if include_gateway_fee else 0
    if include_gateway_fee and gw_fee == 0:
        gw_fee = 1
    total = base_amount + gw_fee

    return {
        "plan_type": plan_type,
        "label": plan["label"],
        "days": plan["days"],
        "country": cfg["name"],
        "country_code": cc,
        "currency": cfg["currency"],
        "symbol": cfg["symbol"],
        "multiplier": cfg["multiplier"],
        "base_amount": base_amount,
        "gateway_fee": gw_fee,
        "gateway_pct": round(cfg["gw_pct"] * 100, 1) if include_gateway_fee else 0,
        "total_amount": total,
    }
