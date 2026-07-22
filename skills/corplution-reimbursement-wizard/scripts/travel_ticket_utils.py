"""Shared route parsing and rail/flight ticket-note normalization."""

from __future__ import annotations

import re
from typing import Any

from text_utils import normalize_text as clean


_ROUTE_CONNECTOR = r"(?:->|→|➡|—|–|~|至|到|-)"
_ROUTE_ENDPOINT = r"[A-Za-z\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff·]{0,29}"
_FLIGHT_TEXT_MARKERS = (
    "飞机",
    "机票",
    "航班",
    "航空客票",
    "电子客票行程单",
    "flight",
    "air ticket",
    "airfare",
)
_FLIGHT_LINE_ITEM_MARKERS = (
    "国内航空",
    "国际航空",
    "航空运输",
    "航空旅客运输",
    "航空客运",
)


def strip_route_place(value: Any) -> str:
    text = clean(value)
    text = re.sub(r"^[A-Z]{0,3}\d{1,5}\s*[,，]?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^(?:高铁|动车|火车|铁路|飞机|航班|机票)\s*", "", text)
    text = re.sub(r"^(?:从|由)\s*", "", text)
    text = re.sub(
        r"\s*(?:二等座|一等座|商务座|硬座|软座|硬卧|软卧|经济舱|公务舱|头等舱).*$",
        "",
        text,
    )
    return text.strip(" ,，;；。()（）")


def route_from_text(value: Any) -> str:
    """Extract a compact origin-destination route from ticket evidence text.

    Endpoint matching is deliberately narrow so dates such as ``2026-06-24``
    are not mistaken for a route.  Both ASCII and common Chinese arrow glyphs
    are accepted because applicant notes frequently use ``→``.
    """
    text = clean(value)
    if not text:
        return ""
    parenthesized = re.search(r"（([^（）]+)）", text)
    candidates = [parenthesized.group(1)] if parenthesized else []
    candidates.extend(re.split(r"[,，;；]", text))
    candidates.append(text)
    pattern = re.compile(
        rf"({_ROUTE_ENDPOINT})\s*{_ROUTE_CONNECTOR}\s*({_ROUTE_ENDPOINT})",
        flags=re.IGNORECASE,
    )
    for candidate in candidates:
        match = pattern.search(candidate)
        if not match:
            continue
        origin = strip_route_place(match.group(1))
        destination = strip_route_place(match.group(2))
        if origin and destination:
            return f"{origin}-{destination}"
    return ""


def is_refund_fee(unit: dict[str, Any]) -> bool:
    if unit.get("is_refund_fee") is True or clean(unit.get("refund_fee_amount")):
        return True
    text = clean(" ".join([
        unit.get("final_note", ""),
        unit.get("source_note", ""),
        unit.get("expense_note", ""),
        unit.get("raw_remarks", ""),
        unit.get("line_item_name", ""),
        unit.get("seller_name", ""),
    ])).lower()
    return any(keyword in text for keyword in ["退票费", "退票", "退款", "refund", "cancellation"])


def is_rail_ticket(unit: dict[str, Any]) -> bool:
    subtype = clean(unit.get("document_subtype"))
    if subtype == "railway_e_ticket":
        return True
    source_category = clean(unit.get("source_category"))
    if source_category and source_category not in {"travel", "rail"}:
        return False
    text = clean(" ".join([
        unit.get("source_note", ""),
        unit.get("expense_note", ""),
        unit.get("final_note", ""),
    ]))
    return bool(re.match(r"^[GCDKZT]\d{1,5}\b", text, flags=re.IGNORECASE))


def is_flight_ticket(unit: dict[str, Any]) -> bool:
    """Recognize air tickets from the invoice's substantive evidence.

    Chinese airline invoices are commonly ordinary/special VAT invoices rather
    than a dedicated document subtype.  Their line item (for example
    ``*交通运输服务*国内航空``) is stronger evidence than the seller display name,
    so inspect it explicitly and use the airline name only as a conservative
    fallback within an already-travel category.
    """
    source_category = clean(unit.get("source_category"))
    if source_category and source_category not in {"travel", "flight"}:
        return False

    subtype = clean(unit.get("document_subtype")).lower()
    if any(marker in subtype for marker in ["flight", "air_ticket", "air-travel"]):
        return True

    evidence_text = clean(" ".join([
        unit.get("source_note", ""),
        unit.get("expense_note", ""),
        unit.get("final_note", ""),
        unit.get("raw_remarks", ""),
    ])).lower()
    if any(marker in evidence_text for marker in _FLIGHT_TEXT_MARKERS):
        return True

    line_item = clean(unit.get("line_item_name")).lower()
    if any(marker in line_item for marker in _FLIGHT_LINE_ITEM_MARKERS):
        return True

    seller = clean(unit.get("seller_name"))
    return bool(re.search(r"航空(?:股份)?有限公司|航空公司", seller))


def ticket_route(unit: dict[str, Any]) -> str:
    endpoint_pairs = [
        (unit.get("origin_station"), unit.get("destination_station")),
        (unit.get("flight_origin"), unit.get("flight_destination")),
    ]
    if is_flight_ticket(unit):
        endpoint_pairs.append((unit.get("origin"), unit.get("destination")))
    for origin_value, destination_value in endpoint_pairs:
        origin = strip_route_place(origin_value)
        destination = strip_route_place(destination_value)
        if origin and destination:
            return f"{origin}-{destination}"
    for field in ["route", "source_note", "expense_note", "final_note"]:
        route = route_from_text(unit.get(field))
        if route:
            return route
    return ""


def ticket_note(unit: dict[str, Any]) -> str:
    rail = is_rail_ticket(unit)
    flight = is_flight_ticket(unit)
    if not rail and not flight:
        return ""
    route = ticket_route(unit)
    if not route:
        return ""
    if rail:
        prefix = "高铁退票费" if is_refund_fee(unit) else "高铁"
    else:
        prefix = "飞机退票费" if is_refund_fee(unit) else "飞机"
    return f"{prefix}（{route}）"


def contains_raw_ticket_evidence(note: Any) -> bool:
    text = clean(note)
    return bool(
        any(connector in text for connector in ["->", "→", "➡"])
        or re.search(r"\b[GCDKZT]\d{1,5}\b", text, flags=re.IGNORECASE)
        or any(
            keyword in text
            for keyword in ["二等座", "一等座", "商务座", "经济舱", "公务舱", "头等舱"]
        )
    )
