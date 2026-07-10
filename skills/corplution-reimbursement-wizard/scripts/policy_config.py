#!/usr/bin/env python3
"""Load Corplution reimbursement policy values from assets/policy.toml.

All policy numbers (meal/hotel caps, first-tier cities) and year-coded
charge codes live in the TOML file so a policy change is a one-file edit.
If the file is missing or a key is absent, the historical defaults below
keep the scripts working unchanged.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    import tomli as tomllib


_DEFAULTS = {
    "caps": {
        "business_trip_meal_daily": "150.00",
        "local_overtime_meal_daily": "60.00",
        "first_tier_hotel_per_night": "800.00",
        "other_city_hotel_per_night": "600.00",
        "first_tier_cities": [
            "北京", "上海", "广州", "深圳",
            "beijing", "shanghai", "guangzhou", "shenzhen",
        ],
    },
    "charge_codes": {
        "admin": "CORP-2026-ADMIN",
        "shared_bd": "CORP-2026-BD",
    },
    "clients": {
        "mobile": "通讯费",
        "admin_fallback": "项目、调研以外的其他费用",
    },
}


def policy_path() -> Path:
    return Path(__file__).resolve().parents[1] / "assets" / "policy.toml"


def _load_raw() -> dict:
    path = policy_path()
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _get(raw: dict, section: str, key: str):
    return raw.get(section, {}).get(key, _DEFAULTS[section][key])


class Policy:
    def __init__(self) -> None:
        raw = _load_raw()
        self.business_trip_meal_daily_cap = Decimal(str(_get(raw, "caps", "business_trip_meal_daily")))
        self.local_overtime_meal_daily_cap = Decimal(str(_get(raw, "caps", "local_overtime_meal_daily")))
        self.first_tier_hotel_cap = Decimal(str(_get(raw, "caps", "first_tier_hotel_per_night")))
        self.other_city_hotel_cap = Decimal(str(_get(raw, "caps", "other_city_hotel_per_night")))
        self.first_tier_cities = {str(city).lower() for city in _get(raw, "caps", "first_tier_cities")}
        self.admin_code = str(_get(raw, "charge_codes", "admin"))
        self.shared_bd_code = str(_get(raw, "charge_codes", "shared_bd"))
        self.mobile_client = str(_get(raw, "clients", "mobile"))
        self.admin_fallback_client = str(_get(raw, "clients", "admin_fallback"))


def load_policy() -> Policy:
    return Policy()
