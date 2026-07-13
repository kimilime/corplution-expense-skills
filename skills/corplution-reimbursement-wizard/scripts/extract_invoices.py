#!/usr/bin/env python3
"""Extract first-stage expense invoice evidence into Markdown and JSON."""

from __future__ import annotations

import argparse

import extraction_corrections as xc
import integrity
import hashlib
import json
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".heic"}
PDF_SUFFIXES = {".pdf"}


C = {
    "invoice_no": "\u53d1\u7968\u53f7\u7801",
    "issue_date": "\u5f00\u7968\u65e5\u671f",
    "ordinary_invoice": "\u666e\u901a\u53d1\u7968",
    "special_invoice": "\u4e13\u7528\u53d1\u7968",
    "vat_special_invoice": "\u589e\u503c\u7a0e\u4e13\u7528\u53d1\u7968",
    "buyer_name": "\u8d2d\u4e70\u65b9\u540d\u79f0",
    "seller_name": "\u9500\u552e\u65b9\u540d\u79f0",
    "tax_id": "\u7edf\u4e00\u793e\u4f1a\u4fe1\u7528\u4ee3\u7801",
    "tax_total": "\u4ef7\u7a0e\u5408\u8ba1",
    "lower": "\u5c0f\u5199",
    "remarks": "\u5907\u6ce8",
    "didi": "\u6ef4\u6ef4",
    "gaode": "\u9ad8\u5fb7",
    "didi_trip_title": "\u6ef4\u6ef4\u51fa\u884c-\u884c\u7a0b\u5355",
    "trip_table": "DIDI TRAVEL - TRIP TABLE",
    "trip_count": "\u7b14\u884c\u7a0b",
    "boarding_time": "\u4e0a\u8f66\u65f6\u95f4",
    "origin": "\u8d77\u70b9",
    "destination": "\u7ec8\u70b9",
    "amount_yuan": "\u91d1\u989d[\u5143]",
    "passenger_transport": "\u5ba2\u8fd0\u670d\u52a1\u8d39",
    "travel_service": "\u65c5\u5ba2\u8fd0\u8f93\u670d\u52a1",
    "mobile_service": "\u901a\u4fe1\u670d\u52a1\u8d39",
    "telecom_service": "\u7535\u4fe1\u670d\u52a1",
    "meal_service": "\u9910\u996e\u670d\u52a1",
    "lodging_service": "\u4f4f\u5bbf\u670d\u52a1",
    "hotel": "\u9152\u5e97",
    "railway_ticket": "\u94c1\u8def\u7535\u5b50\u5ba2\u7968",
    "ticket_price": "\u7968\u4ef7",
    "refund_fee": "\u9000\u7968\u8d39",
    "shanghai": "\u4e0a\u6d77",
    "invoice": "\u53d1\u7968",
    "total": "\u5408\u8ba1",
    "phone": "\u624b\u673a\u53f7",
    "billing_period": "\u8d26\u671f",
}

ROLE_LABELS = {
    "invoice": C["invoice"],
    "supporting_schedule": "\u884c\u7a0b\u5355/\u652f\u6301\u6587\u4ef6",
    "supporting_document": "\u652f\u6301\u6587\u4ef6",
    "unknown": "\u672a\u8bc6\u522b",
}

SUBTYPE_LABELS = {
    "didi_trip_report": C["didi"] + "\u884c\u7a0b\u5355",
    "gaode_trip_report": C["gaode"] + "\u884c\u7a0b\u5355",
    "railway_e_ticket": C["railway_ticket"],
    "vat_special_invoice": C["vat_special_invoice"],
    "invoice_unknown_subtype": C["ordinary_invoice"],
    "unknown": "\u5f85\u786e\u8ba4",
}

INVOICE_TYPE_LABELS = {
    "ordinary": C["ordinary_invoice"],
    "special": C["special_invoice"],
    "railway_e_ticket": C["railway_ticket"],
    "unknown": "\u5f85\u786e\u8ba4",
}

CATEGORY_LABELS = {
    "hotel": "\u9152\u5e97",
    "travel": "\u51fa\u5dee/\u4ea4\u901a",
    "taxi": "\u6253\u8f66",
    "meal": "\u9910\u8d39",
    "mobile": "\u901a\u8baf\u8d39",
    "other": "\u5176\u4ed6",
    "unknown": "\u5f85\u786e\u8ba4",
}


@dataclass
class ExtractedText:
    text: str
    tables: list[list[list[str]]]
    page_count: int
    method: str
    ocr_required: bool
    issues: list[dict[str, str]]


def clean(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\u3000", " ").replace("\r", "\n")
    return re.sub(r"\s+", " ", text).strip()


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def compact(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def money(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace(",", "").replace("\uffe5", "").replace("\u00a5", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return ""
    try:
        return f"{Decimal(match.group(0)):.2f}"
    except InvalidOperation:
        return ""


def date_from_chinese(value: str) -> str:
    match = re.search(r"(\d{4})\s*\u5e74\s*(\d{1,2})\s*\u6708\s*(\d{1,2})\s*\u65e5", value or "")
    if not match:
        return ""
    return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"


def date_from_dash(value: str) -> str:
    match = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", value or "")
    if not match:
        return ""
    return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def iter_input_files(paths: list[Path]) -> tuple[list[Path], list[Path]]:
    """Collect supported files and, separately, files that were skipped.

    Skipped files must be surfaced to the user, never silently dropped:
    unsupported suffixes (e.g. .ofd e-invoices, .eml, .zip) may still be
    reimbursement evidence that needs manual handling.
    """
    files: list[Path] = []
    skipped: list[Path] = []
    for path in paths:
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if not child.is_file():
                    continue
                if child.suffix.lower() in PDF_SUFFIXES | IMAGE_SUFFIXES:
                    files.append(child)
                else:
                    skipped.append(child)
        elif path.is_file():
            if path.suffix.lower() in PDF_SUFFIXES | IMAGE_SUFFIXES:
                files.append(path)
            else:
                skipped.append(path)
    return files, skipped


def has_tesseract() -> bool:
    return shutil.which("tesseract") is not None


def extract_pdf(path: Path) -> ExtractedText:
    issues: list[dict[str, str]] = []
    try:
        import pdfplumber  # type: ignore
    except Exception as exc:  # pragma: no cover - environment dependent
        return ExtractedText("", [], 0, "manual_review", False, [{
            "field": "pdf_text",
            "problem": f"pdfplumber unavailable: {exc}",
            "suggested_action": "Install pdfplumber or inspect the PDF manually.",
        }])

    text_parts: list[str] = []
    all_tables: list[list[list[str]]] = []
    page_count = 0
    try:
        with pdfplumber.open(str(path)) as pdf:
            page_count = len(pdf.pages)
            for page in pdf.pages:
                text_parts.append(page.extract_text(x_tolerance=1, y_tolerance=3) or "")
                try:
                    for table in page.extract_tables() or []:
                        all_tables.append([[clean(cell) for cell in row] for row in table])
                except Exception as exc:
                    issues.append({
                        "field": "tables",
                        "problem": f"Table extraction failed: {exc}",
                        "suggested_action": "Inspect rendered page if table fields are missing.",
                    })
    except Exception as exc:
        return ExtractedText("", [], page_count, "manual_review", False, [{
            "field": "pdf",
            "problem": f"Cannot read PDF: {exc}",
            "suggested_action": "Open the file manually and verify it is not corrupt or password protected.",
        }])

    text = "\n".join(text_parts).strip()
    if len(clean(text)) >= 30:
        return ExtractedText(text, all_tables, page_count, "text_layer", False, issues)

    if has_tesseract():
        ocr_text = ocr_pdf(path, issues)
        method = "ocr" if ocr_text else "manual_review"
        return ExtractedText(ocr_text, all_tables, page_count, method, True, issues)

    issues.append({
        "field": "ocr",
        "problem": "PDF has no reliable text layer and no local Tesseract OCR executable was found.",
        "suggested_action": "Install Tesseract with Chinese language data or inspect the rendered page manually.",
    })
    return ExtractedText(text, all_tables, page_count, "manual_review", True, issues)


def ocr_pdf(path: Path, issues: list[dict[str, str]]) -> str:
    try:
        import pytesseract  # type: ignore
        from pdf2image import convert_from_path  # type: ignore
    except Exception as exc:  # pragma: no cover - optional dependency
        issues.append({
            "field": "ocr",
            "problem": f"OCR dependencies unavailable: {exc}",
            "suggested_action": "Install pytesseract and pdf2image, or use manual review.",
        })
        return ""

    try:
        pages = convert_from_path(str(path), dpi=220)
    except Exception as exc:
        issues.append({
            "field": "ocr",
            "problem": f"PDF rendering for OCR failed: {exc}",
            "suggested_action": "Render the page manually and inspect it.",
        })
        return ""

    text_parts = []
    for page in pages:
        try:
            text_parts.append(pytesseract.image_to_string(page, lang="chi_sim+eng"))
        except Exception as exc:
            issues.append({
                "field": "ocr",
                "problem": f"Tesseract OCR failed: {exc}",
                "suggested_action": "Verify Tesseract language data and inspect the page manually.",
            })
    return "\n".join(text_parts).strip()


def extract_image(path: Path) -> ExtractedText:
    issues: list[dict[str, str]] = []
    if not has_tesseract():
        issues.append({
            "field": "ocr",
            "problem": "Image input requires OCR, but no local Tesseract executable was found.",
            "suggested_action": "Install Tesseract with Chinese language data or inspect the image manually.",
        })
        return ExtractedText("", [], 1, "manual_review", True, issues)

    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore
    except Exception as exc:  # pragma: no cover - optional dependency
        issues.append({
            "field": "ocr",
            "problem": f"OCR dependencies unavailable: {exc}",
            "suggested_action": "Install pytesseract and Pillow or inspect the image manually.",
        })
        return ExtractedText("", [], 1, "manual_review", True, issues)

    try:
        with Image.open(path) as image:
            text = pytesseract.image_to_string(image, lang="chi_sim+eng")
    except Exception as exc:
        issues.append({
            "field": "ocr",
            "problem": f"Image OCR failed: {exc}",
            "suggested_action": "Inspect the image manually or try a clearer scan.",
        })
        return ExtractedText("", [], 1, "manual_review", True, issues)

    return ExtractedText(text, [], 1, "ocr", True, issues)


def extract_source(path: Path) -> ExtractedText:
    suffix = path.suffix.lower()
    if suffix in PDF_SUFFIXES:
        return extract_pdf(path)
    if suffix in IMAGE_SUFFIXES:
        return extract_image(path)
    return ExtractedText("", [], 0, "manual_review", False, [{
        "field": "file_type",
        "problem": f"Unsupported file type: {suffix}",
        "suggested_action": "Provide a PDF or image file.",
    }])


def regex_first(pattern: str, text: str, flags: int = re.S) -> str:
    match = re.search(pattern, text or "", flags)
    return clean(match.group(1)) if match else ""


def table_buyer_seller(tables: list[list[list[str]]]) -> dict[str, str]:
    out = {"buyer_name": "", "buyer_tax_id": "", "seller_name": "", "seller_tax_id": ""}
    for table in tables:
        for row in table:
            cells = [clean(cell) for cell in row]
            joined = " ".join(cells)
            if "\u8d2d" in joined and "\u9500" in joined and len(cells) >= 5:
                buyer_cell = cells[1] if len(cells) > 1 else ""
                seller_cell = cells[4] if len(cells) > 4 else ""
                out["buyer_name"] = parse_name_from_cell(buyer_cell)
                out["seller_name"] = parse_name_from_cell(seller_cell)
                out["buyer_tax_id"] = parse_tax_id_from_cell(buyer_cell)
                out["seller_tax_id"] = parse_tax_id_from_cell(seller_cell)
                return out
    return out


def parse_name_from_cell(cell: str) -> str:
    value = regex_first(r"\u540d\u79f0\s*[:\uff1a]\s*(.*?)(?:\s*\u7edf\u4e00|\s*$)", cell)
    return value


def parse_tax_id_from_cell(cell: str) -> str:
    value = regex_first(r"(?:\u7eb3\u7a0e\u4eba\u8bc6\u522b\u53f7|\u7edf\u4e00\u793e\u4f1a\u4fe1\u7528\u4ee3\u7801)\s*[:\uff1a]\s*([A-Z0-9]+)", cell)
    return value


def parse_invoice_type(text: str) -> str:
    if C["railway_ticket"] in text or "12306" in text:
        return "railway_e_ticket"
    if C["special_invoice"] in text or C["vat_special_invoice"] in text:
        return "special"
    if C["ordinary_invoice"] in text:
        return "ordinary"
    return "unknown"


def classify_role_and_subtype(text: str, tables: list[list[list[str]]]) -> tuple[str, str]:
    normalized = clean(text)
    if (
        C["gaode"] in normalized
        and ("\u884c\u7a0b\u5355" in normalized or "\u884c\u7a0b\u62a5\u9500" in normalized or "\u884c\u7a0b\u660e\u7ec6" in normalized)
    ):
        return "supporting_schedule", "gaode_trip_report"
    if (
        C["didi_trip_title"] in normalized
        or C["trip_table"] in normalized
        or (C["trip_count"] in normalized and C["boarding_time"] in normalized and C["origin"] in normalized)
    ):
        return "supporting_schedule", "didi_trip_report"

    if C["railway_ticket"] in normalized or "12306" in normalized:
        return "invoice", "railway_e_ticket"

    markers = [C["invoice_no"], C["issue_date"], C["tax_total"]]
    if sum(1 for marker in markers if marker in normalized) >= 2:
        invoice_type = parse_invoice_type(normalized)
        subtype = {
            "ordinary": "vat_ordinary_invoice",
            "special": "vat_special_invoice",
            "railway_e_ticket": "railway_e_ticket",
        }.get(invoice_type, "invoice_unknown_subtype")
        return "invoice", subtype

    if tables:
        return "supporting_document", "table_document"
    return "unknown", "unknown"


def is_railway_refund_text(text: str) -> bool:
    normalized = compact(text).lower()
    return "\u9000\u7968" in normalized or "refund" in normalized or "cancellation" in normalized


def parse_railway_refund_fee_amount(text: str) -> str:
    """Parse the reimbursable amount printed beside the railway refund-fee label.

    China Railway e-ticket PDFs can place the larger amount glyph a few points
    above the smaller label. pdfplumber can then emit the amount one line
    before its refund-fee label, so support both semantic orders.
    """
    label = r"\u9000\s*\u7968\s*\u8d39"
    amount = r"[0-9][0-9,]*(?:\.[0-9]{1,2})?"
    currency = r"(?:[\u00a5\uffe5]|RMB|CNY)"
    patterns = [
        label + r"[^\S\r\n]*[:\uff1a]?\s*" + currency + r"\s*(" + amount + r")",
        label + r"[^\S\r\n]*[:\uff1a][^\S\r\n]*(" + amount + r")",
        (
            r"(?m)^[^\S\r\n]*" + currency + r"[^\S\r\n]*(" + amount + r")"
            r"[^\S\r\n]*(?:\r?\n[^\S\r\n]*)?" + label + r"[^\S\r\n]*[:\uff1a]?"
        ),
    ]
    for pattern in patterns:
        value = money(regex_first(pattern, text, re.S | re.I))
        if value:
            return value
    return ""


def parse_total_amount(text: str, tables: list[list[list[str]]], invoice_type: str) -> str:
    if invoice_type == "railway_e_ticket" and is_railway_refund_text(text):
        # A railway refund invoice reimburses the refund fee itself. Do not
        # fall back to another visible amount or treat a blank label as zero.
        return parse_railway_refund_fee_amount(text)

    patterns = [
        r"\u5c0f\s*\u5199\s*[\)\uff09]?\s*[\u00a5\uffe5]?\s*([0-9]+(?:\.[0-9]{1,2})?)",
        r"[\(\uff08]\s*\u5c0f\s*\u5199\s*[\)\uff09]\s*[\u00a5\uffe5]?\s*([0-9]+(?:\.[0-9]{1,2})?)",
        C["tax_total"] + r".{0,30}?[\u00a5\uffe5]\s*([0-9]+(?:\.[0-9]{1,2})?)",
    ]
    for pattern in patterns:
        value = money(regex_first(pattern, text))
        if value:
            return value

    if invoice_type == "railway_e_ticket":
        value = money(regex_first(r"[\u00a5\uffe5]\s*([0-9]+(?:\.[0-9]{1,2})?)\s*" + C["ticket_price"], text))
        if value:
            return value
        value = money(regex_first(C["ticket_price"] + r"\s*[:\uff1a]?\s*[\u00a5\uffe5]?\s*([0-9]+(?:\.[0-9]{1,2})?)", text))
        if value:
            return value

    for table in tables:
        for row in table:
            joined = " ".join(clean(cell) for cell in row)
            if C["tax_total"] in joined or C["lower"] in joined:
                amounts = re.findall(r"[\u00a5\uffe5]\s*([0-9]+(?:\.[0-9]{1,2})?)", joined)
                if amounts:
                    return money(amounts[-1])
    return ""


def parse_amount_parts(text: str, tables: list[list[list[str]]]) -> tuple[str, str]:
    for table in tables:
        for row in table:
            joined = " ".join(clean(cell) for cell in row)
            if C["total"] in joined and "[\u5143]" not in joined:
                amounts = re.findall(r"[\u00a5\uffe5]\s*([0-9]+(?:\.[0-9]{1,2})?)", joined)
                if len(amounts) >= 2:
                    return money(amounts[-2]), money(amounts[-1])
                if len(amounts) == 1:
                    return money(amounts[0]), ""
    return "", ""


def parse_line_item(text: str) -> str:
    match = re.search(r"\*([^*\n]{2,20})\*([^ \n]+)", text or "")
    if match:
        return f"*{clean(match.group(1))}*{clean(match.group(2))}"
    for keyword in [C["passenger_transport"], C["mobile_service"], C["meal_service"], C["lodging_service"]]:
        if keyword in text:
            return keyword
    return ""


def parse_raw_remarks(tables: list[list[list[str]]], text: str) -> str:
    for table in tables:
        for row in table:
            cells = [clean(cell) for cell in row]
            joined = " ".join(cells)
            if C["remarks"] in joined:
                values = [cell for cell in cells if cell and C["remarks"] not in cell]
                if values:
                    return clean(" ".join(values))
    return regex_first(C["remarks"] + r"\s*(.*?)(?:\u5f00\s*\u7968\s*\u4eba|\n|$)", text)


def classify_expense(text: str, seller_name: str, line_item: str, subtype: str) -> tuple[str, str]:
    haystack = clean(" ".join([text, seller_name, line_item, subtype]))
    if subtype == "railway_e_ticket":
        return "travel", "Railway e-ticket invoice."
    if C["didi"] in haystack and (C["passenger_transport"] in haystack or C["travel_service"] in haystack):
        return "taxi", "Didi passenger transport summary invoice."
    if C["gaode"] in haystack and (C["passenger_transport"] in haystack or C["travel_service"] in haystack):
        return "taxi", "Gaode passenger transport summary invoice."
    if C["lodging_service"] in haystack or C["hotel"] in seller_name:
        return "hotel", "Lodging or hotel service."
    if C["meal_service"] in haystack:
        return "meal", "Meal service invoice."
    if C["mobile_service"] in haystack or C["telecom_service"] in haystack:
        return "mobile", "Telecom or mobile service invoice."
    if C["passenger_transport"] in haystack or C["travel_service"] in haystack:
        return "travel", "Passenger transport service."
    if C["invoice"] in haystack:
        return "other", "Valid invoice without a more specific first-pass category."
    return "unknown", "Insufficient evidence for category."


def note_for_invoice(text: str, invoice: dict[str, str], category: str, subtype: str) -> str:
    if subtype == "railway_e_ticket":
        return railway_note(text)
    seller = invoice.get("seller_name", "")
    line_item = invoice.get("line_item_name", "")
    raw = invoice.get("raw_remarks", "")
    if category == "hotel":
        qty = regex_first(r"\s(\d+(?:\.\d+)?)\s*" + "\u5929", text)
        suffix = f", {qty}\u5929" if qty else ""
        return clean(f"{seller}\u4f4f\u5bbf{suffix}") or line_item
    if category == "meal":
        return clean(f"{seller}\u9910\u996e") or line_item
    if category == "mobile":
        phone = regex_first(C["phone"] + r"\s*[:\uff1a]\s*([0-9]{6,20})", raw or text)
        period = regex_first(C["billing_period"] + r"\s*[:\uff1a]\s*([0-9]{4,8})", raw or text)
        parts = [seller or "\u901a\u4fe1\u670d\u52a1\u8d39"]
        if period:
            parts.append(f"\u8d26\u671f{period}")
        if phone:
            parts.append(phone)
        return clean(", ".join(parts))
    if C["didi"] in (seller + text):
        return "Didi passenger transport summary invoice"
    if C["gaode"] in (seller + text):
        return "Gaode passenger transport summary invoice"
    return clean(seller or line_item)


def parse_railway_leg(text: str) -> dict[str, Any]:
    compact_text = clean(text)
    train = regex_first(r"\b([GDCZTK]\d{1,5})\b", compact_text)
    travel_date = date_from_chinese(regex_first(r"(\d{4}\s*\u5e74\s*\d{1,2}\s*\u6708\s*\d{1,2}\s*\u65e5\s*\d{1,2}:\d{2})", compact_text))
    time_part = regex_first(r"\d{4}\s*\u5e74\s*\d{1,2}\s*\u6708\s*\d{1,2}\s*\u65e5\s*(\d{1,2}:\d{2})", compact_text)
    origin = ""
    destination = ""
    if train:
        route_match = re.search(r"([\u4e00-\u9fffA-Za-z]{2,30})\s+" + re.escape(train) + r"\s+([\u4e00-\u9fffA-Za-z]{2,30})", compact_text)
        if route_match:
            origin = clean(route_match.group(1))
            destination = clean(route_match.group(2))
    refund = is_railway_refund_text(text)
    refund_fee_amount = parse_railway_refund_fee_amount(text) if refund else ""
    return {
        "train_no": train,
        "travel_date": travel_date,
        "departure_time": time_part,
        "departure_datetime": f"{travel_date} {time_part}".strip() if travel_date else "",
        "origin_station": origin,
        "destination_station": destination,
        "route": f"{origin}-{destination}" if origin and destination else "",
        "is_refund_fee": refund,
        "refund_fee_amount": refund_fee_amount,
    }


def railway_note(text: str) -> str:
    leg = parse_railway_leg(text)
    seat = regex_first(r"(\u4e00\u7b49\u5ea7|\u4e8c\u7b49\u5ea7|\u5546\u52a1\u5ea7|\u786c\u5ea7|\u786c\u5367|\u8f6f\u5367)", clean(text))
    route = leg["route"].replace("-", " -> ", 1) if leg["route"] else ""
    refund_note = ""
    if leg["is_refund_fee"]:
        refund_note = C["refund_fee"]
        if leg["refund_fee_amount"]:
            refund_note += f" \u00a5{leg['refund_fee_amount']}"
    parts = [
        part
        for part in [
            leg["train_no"],
            route,
            leg["departure_datetime"],
            seat,
            refund_note,
        ]
        if part
    ]
    return clean(", ".join(parts))


def railway_travel_date(text: str) -> str:
    return clean(parse_railway_leg(text).get("travel_date"))


def parse_invoice(text: str, tables: list[list[list[str]]], subtype: str) -> dict[str, str]:
    invoice_type = parse_invoice_type(text)
    party = table_buyer_seller(tables)
    invoice_no = regex_first(C["invoice_no"] + r"\s*[:\uff1a]\s*([0-9]{8,30})", text)
    issue_date = date_from_chinese(regex_first(C["issue_date"] + r"\s*[:\uff1a]\s*(\d{4}\s*\u5e74\s*\d{1,2}\s*\u6708\s*\d{1,2}\s*\u65e5)", text))
    if not issue_date:
        issue_date = date_from_dash(regex_first(C["issue_date"] + r"\s*[:\uff1a]\s*([0-9]{4}[-/.][0-9]{1,2}[-/.][0-9]{1,2})", text))

    buyer_name = party["buyer_name"] or regex_first(C["buyer_name"] + r"\s*[:\uff1a]\s*(.*?)(?:\s+" + C["tax_id"] + r"|\n|$)", text)
    seller_name = party["seller_name"] or regex_first(C["seller_name"] + r"\s*[:\uff1a]\s*(.*?)(?:\s+" + C["tax_id"] + r"|\n|$)", text)
    if not buyer_name:
        buyer_name = regex_first(r"\u8d2d\s+\u540d\u79f0\s*[:\uff1a]\s*(.*?)\s+\u9500\s+\u540d\u79f0", clean(text))
    if not seller_name:
        seller_name = regex_first(r"\u9500\s+\u540d\u79f0\s*[:\uff1a]\s*(.*?)\s+\u4e70", clean(text))

    amount_without_tax, tax_amount = parse_amount_parts(text, tables)
    total_amount = parse_total_amount(text, tables, invoice_type)
    line_item = parse_line_item(text)
    raw_remarks = parse_raw_remarks(tables, text)

    invoice = {
        "invoice_no": invoice_no,
        "invoice_type": invoice_type,
        "issue_date": issue_date,
        "buyer_name": clean(buyer_name),
        "buyer_tax_id": party["buyer_tax_id"],
        "seller_name": clean(seller_name),
        "seller_tax_id": party["seller_tax_id"],
        "line_item_name": line_item,
        "amount_without_tax": amount_without_tax,
        "tax_amount": tax_amount,
        "total_amount": total_amount,
        "currency": "CNY",
        "raw_remarks": raw_remarks,
    }
    return invoice


def column_index(header: list[str], candidates: list[str]) -> int | None:
    for idx, cell in enumerate(header):
        if any(candidate in cell for candidate in candidates):
            return idx
    return None


def parse_ride_report(
    text: str,
    tables: list[list[list[str]]],
    document_id: str,
    provider: str,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    report_date = date_from_dash(regex_first(r"\u7533\u8bf7\u65e5\u671f\s*[:\uff1a]\s*([0-9]{4}-[0-9]{1,2}-[0-9]{1,2})", text))
    period_match = re.search(r"\u884c\u7a0b\u8d77\u6b62\u65e5\u671f\s*[:\uff1a]\s*([0-9]{4}-[0-9]{1,2}-[0-9]{1,2})\s*\u81f3\s*([0-9]{4}-[0-9]{1,2}-[0-9]{1,2})", text or "")
    period_start = date_from_dash(period_match.group(1)) if period_match else ""
    period_end = date_from_dash(period_match.group(2)) if period_match else ""
    phone = regex_first(r"\u884c\u7a0b\u4eba" + C["phone"] + r"\s*[:\uff1a]\s*([0-9]{6,20})", text)
    count_text = regex_first(r"\u5171\s*(\d+)\s*\u7b14\u884c\u7a0b", text)
    total = money(regex_first(r"\u5408\u8ba1\s*([0-9]+(?:\.[0-9]{1,2})?)\s*\u5143", text))

    items: list[dict[str, Any]] = []
    for table in tables:
        if not table:
            continue
        header = [compact(cell) for cell in table[0]]
        time_idx = column_index(header, [C["boarding_time"], "\u7528\u8f66\u65f6\u95f4", "\u884c\u7a0b\u65f6\u95f4", "\u5f00\u59cb\u65f6\u95f4"])
        city_idx = column_index(header, ["\u57ce\u5e02", "\u7528\u8f66\u57ce\u5e02"])
        origin_idx = column_index(header, [C["origin"], "\u4e0a\u8f66\u5730\u70b9", "\u51fa\u53d1\u5730"])
        dest_idx = column_index(header, [C["destination"], "\u4e0b\u8f66\u5730\u70b9", "\u76ee\u7684\u5730"])
        distance_idx = column_index(header, ["\u91cc\u7a0b", "\u8ddd\u79bb"])
        amount_idx = column_index(header, [C["amount_yuan"], "\u91d1\u989d", "\u8d39\u7528", "\u5b9e\u4ed8"])
        vehicle_idx = column_index(header, ["\u8f66\u578b", "\u7528\u8f66\u7c7b\u578b", "\u670d\u52a1\u7c7b\u578b"])
        if time_idx is None or origin_idx is None or dest_idx is None or amount_idx is None:
            continue
        for row_number, row in enumerate(table[1:], start=1):
            if len(row) <= max(idx for idx in [time_idx, origin_idx, dest_idx, amount_idx] if idx is not None):
                continue
            seq = clean(row[0])
            if not seq or not seq.isdigit():
                seq = str(row_number)
            vehicle_type = clean(row[vehicle_idx]) if vehicle_idx is not None and len(row) > vehicle_idx else ""
            ride_time_raw = clean(row[time_idx])
            city = compact(clean(row[city_idx])) if city_idx is not None and len(row) > city_idx else ""
            origin = clean(row[origin_idx])
            destination = clean(row[dest_idx])
            distance = money(row[distance_idx]) if distance_idx is not None and len(row) > distance_idx else ""
            amount = money(row[amount_idx])
            if not amount:
                continue
            ride_datetime = didi_datetime(ride_time_raw, period_start, period_end)
            category = "taxi" if C["shanghai"] in city else "travel"
            note = clean(f"{provider} {city}: {origin} -> {destination}")
            items.append({
                "item_id": f"{document_id}-ITEM-{int(seq):03d}",
                "ride_datetime": ride_datetime,
                "city": city,
                "vehicle_type": vehicle_type,
                "origin": origin,
                "destination": destination,
                "distance_km": distance,
                "amount": amount,
                "expense_category": category,
                "expense_note": note,
            })

    schedule = {
        "report_date": report_date,
        "traveler_phone": phone,
        "period_start": period_start,
        "period_end": period_end,
        "reported_total_amount": total,
        "item_count": int(count_text) if count_text.isdigit() else len(items),
    }
    return schedule, items


def parse_didi_report(text: str, tables: list[list[list[str]]], document_id: str) -> tuple[dict[str, str], list[dict[str, Any]]]:
    return parse_ride_report(text, tables, document_id, "Didi")


def parse_gaode_report(text: str, tables: list[list[list[str]]], document_id: str) -> tuple[dict[str, str], list[dict[str, Any]]]:
    return parse_ride_report(text, tables, document_id, C["gaode"])


def didi_datetime(raw: str, period_start: str, period_end: str) -> str:
    match = re.search(r"(\d{1,2})-(\d{1,2})\s+(\d{1,2})\s*:\s*(\d{1,2})", raw or "")
    if not match:
        return ""
    month, day, hour, minute = [int(x) for x in match.groups()]
    year = ""
    for candidate in [period_start, period_end]:
        if candidate and f"-{month:02d}-{day:02d}" in candidate:
            year = candidate[:4]
            break
    if not year:
        year = (period_start or period_end or "")[:4]
    if not year:
        return f"{month:02d}-{day:02d} {hour:02d}:{minute:02d}"
    return f"{year}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}"


def base_document(document_id: str, path: Path, source: ExtractedText) -> dict[str, Any]:
    return {
        "document_id": document_id,
        "source_file": str(path.resolve()),
        "sha256": sha256_file(path),
        "page_count": source.page_count,
        "document_role": "unknown",
        "document_subtype": "unknown",
        "extraction_method": source.method,
        "ocr_required": source.ocr_required,
        "confidence": 0.0,
        "needs_review": False,
        "invoice": None,
        "classification": {
            "expense_category": "unknown",
            "expense_date": "",
            "expense_date_source": "",
            "expense_note": "",
            "reason": "",
        },
        "supporting_schedule": None,
        "supporting_items": [],
        "evidence": [],
        "issues": list(source.issues),
    }


def extract_document(document_id: str, path: Path) -> dict[str, Any]:
    source = extract_source(path)
    doc = base_document(document_id, path, source)
    text = source.text
    role, subtype = classify_role_and_subtype(text, source.tables)
    doc["document_role"] = role
    doc["document_subtype"] = subtype

    if source.method == "manual_review":
        doc["needs_review"] = True

    if role == "supporting_schedule" and subtype in {"didi_trip_report", "gaode_trip_report"}:
        provider = C["gaode"] if subtype == "gaode_trip_report" else C["didi"]
        schedule, items = (
            parse_gaode_report(text, source.tables, document_id)
            if subtype == "gaode_trip_report"
            else parse_didi_report(text, source.tables, document_id)
        )
        doc["supporting_schedule"] = schedule
        doc["supporting_items"] = items
        doc["classification"] = {
            "expense_category": "travel" if any(item.get("expense_category") == "travel" for item in items) else "taxi",
            "expense_date": schedule.get("period_start", ""),
            "expense_date_source": "trip_report_period_start" if schedule.get("period_start") else "",
            "expense_note": f"{provider} trip report",
            "reason": "Trip report parsed into ride-level support items.",
        }
        if schedule.get("item_count") and schedule.get("item_count") != len(items):
            doc["issues"].append({
                "field": "supporting_items",
                "problem": f"Reported item count is {schedule.get('item_count')} but parsed {len(items)} rows.",
                "suggested_action": f"Inspect the {provider} trip table manually.",
            })
        if schedule.get("reported_total_amount"):
            parsed_total = sum(Decimal(item["amount"]) for item in items if item.get("amount"))
            if items and f"{parsed_total:.2f}" != schedule["reported_total_amount"]:
                doc["issues"].append({
                    "field": "reported_total_amount",
                    "problem": f"Reported total {schedule['reported_total_amount']} does not equal parsed total {parsed_total:.2f}.",
                    "suggested_action": f"Verify all {provider} rows were parsed.",
                })
        doc["confidence"] = 0.95 if items else 0.65

    elif role == "invoice":
        invoice = parse_invoice(text, source.tables, subtype)
        category, reason = classify_expense(text, invoice.get("seller_name", ""), invoice.get("line_item_name", ""), subtype)
        invoice_note = note_for_invoice(text, invoice, category, subtype)
        doc["invoice"] = invoice
        expense_date = ""
        expense_date_source = ""
        if subtype == "railway_e_ticket":
            railway_leg = parse_railway_leg(text)
            expense_date = clean(railway_leg.get("travel_date"))
            expense_date_source = "railway_travel_date" if expense_date else ""
        doc["classification"] = {
            "expense_category": category,
            "expense_date": expense_date,
            "expense_date_source": expense_date_source,
            "expense_note": invoice_note,
            "reason": reason,
        }
        if subtype == "railway_e_ticket":
            doc["classification"]["railway_leg"] = railway_leg
        required = ["invoice_no", "issue_date", "total_amount"]
        if subtype != "railway_e_ticket":
            required.append("seller_name")
        for field in required:
            if not invoice.get(field):
                refund_amount_missing = (
                    field == "total_amount"
                    and subtype == "railway_e_ticket"
                    and bool((doc["classification"].get("railway_leg") or {}).get("is_refund_fee"))
                )
                doc["issues"].append({
                    "field": field,
                    "problem": (
                        "Railway refund-fee amount was not extracted; a blank label is not zero."
                        if refund_amount_missing
                        else "Required invoice field was not extracted."
                    ),
                    "suggested_action": (
                        "Inspect the amount printed beside the refund-fee label and correct it through Stage 1."
                        if refund_amount_missing
                        else "Inspect source document or OCR output manually."
                    ),
                })
        doc["confidence"] = 0.95 if not doc["issues"] else 0.75

    else:
        doc["issues"].append({
            "field": "document_role",
            "problem": "Document role could not be confidently identified.",
            "suggested_action": "Inspect the file manually and classify it before downstream use.",
        })
        doc["confidence"] = 0.2

    if doc["issues"]:
        doc["needs_review"] = True
    return doc


def build_links_and_reviews(documents: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    links: list[dict[str, Any]] = []
    review: list[dict[str, str]] = []

    seen_invoice_numbers: dict[str, str] = {}
    seen_hashes: dict[str, str] = {}
    for doc in documents:
        source_hash = doc.get("sha256") or ""
        if source_hash and source_hash in seen_hashes:
            links.append({
                "source_document_id": seen_hashes[source_hash],
                "target_document_id": doc["document_id"],
                "relation": "duplicate_source_file",
                "check": {"sha256": source_hash, "matched": True},
            })
            doc["issues"].append({
                "field": "source_file",
                "problem": f"File content exactly duplicates {seen_hashes[source_hash]} (same SHA-256).",
                "suggested_action": (
                    "Confirm which copy to keep, then exclude the duplicate at Stage 1 through "
                    "apply_extraction_corrections.py; dropping an allocation unit is not equivalent."
                ),
            })
            doc["needs_review"] = True
        else:
            if source_hash:
                seen_hashes[source_hash] = doc["document_id"]

        invoice = doc.get("invoice") or {}
        invoice_no = invoice.get("invoice_no") or ""
        if invoice_no:
            if invoice_no in seen_invoice_numbers:
                links.append({
                    "source_document_id": seen_invoice_numbers[invoice_no],
                    "target_document_id": doc["document_id"],
                    "relation": "possible_duplicate_invoice_no",
                    "check": {"invoice_no": invoice_no, "matched": True},
                })
                doc["issues"].append({
                    "field": "invoice_no",
                    "problem": f"Invoice number duplicates {seen_invoice_numbers[invoice_no]}.",
                    "suggested_action": "Confirm whether the duplicate file should be excluded.",
                })
                doc["needs_review"] = True
            else:
                seen_invoice_numbers[invoice_no] = doc["document_id"]

    for provider_key, provider_name in [("didi", C["didi"]), ("gaode", C["gaode"])]:
        provider_invoices = [
            doc for doc in documents
            if doc.get("document_role") == "invoice"
            and doc.get("invoice")
            and (
                provider_name in ((doc["invoice"].get("seller_name") or "") + (doc["classification"].get("expense_note") or ""))
                or provider_key.title() in (doc["classification"].get("expense_note") or "")
            )
        ]
        provider_schedules = [
            doc for doc in documents
            if doc.get("document_subtype") == f"{provider_key}_trip_report" and doc.get("supporting_schedule")
        ]
        for invoice_doc in provider_invoices:
            amount = (invoice_doc.get("invoice") or {}).get("total_amount")
            for schedule_doc in provider_schedules:
                schedule_amount = (schedule_doc.get("supporting_schedule") or {}).get("reported_total_amount")
                if amount and schedule_amount and amount == schedule_amount:
                    links.append({
                        "source_document_id": invoice_doc["document_id"],
                        "target_document_id": schedule_doc["document_id"],
                        "relation": f"invoice_total_matches_{provider_key}_trip_report",
                        "check": {
                            "source_amount": amount,
                            "target_amount": schedule_amount,
                            "matched": True,
                        },
                    })

    for doc in documents:
        for issue in doc.get("issues", []):
            review.append({
                "document_id": doc["document_id"],
                "field": issue.get("field", ""),
                "problem": issue.get("problem", ""),
                "suggested_action": issue.get("suggested_action", ""),
            })
    return links, review


def batch_summary(documents: list[dict[str, Any]], review_queue: list[dict[str, str]]) -> dict[str, Any]:
    total = Decimal("0.00")
    for doc in documents:
        invoice = doc.get("invoice") or {}
        value = invoice.get("total_amount")
        if value:
            try:
                total += Decimal(value)
            except InvalidOperation:
                pass
    return {
        "input_count": len(documents),
        "invoice_count": sum(1 for doc in documents if doc.get("document_role") == "invoice"),
        "supporting_schedule_count": sum(1 for doc in documents if doc.get("document_role") == "supporting_schedule"),
        "unknown_count": sum(1 for doc in documents if doc.get("document_role") == "unknown"),
        "review_count": len({item["document_id"] for item in review_queue}),
        "total_invoice_amount": f"{total:.2f}",
    }


def write_json(output_dir: Path, payload: dict[str, Any]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "invoice-extraction.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def md_escape(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def document_user_no(doc: dict[str, Any]) -> int | str:
    match = re.search(r"(\d+)$", str(doc.get("document_id", "")))
    return int(match.group(1)) if match else doc.get("document_id", "")


def label(value: str, labels: dict[str, str]) -> str:
    return labels.get(value or "", value or "\u5f85\u786e\u8ba4")


def document_amount(doc: dict[str, Any]) -> str:
    invoice = doc.get("invoice") or {}
    schedule = doc.get("supporting_schedule") or {}
    return invoice.get("total_amount") or schedule.get("reported_total_amount") or ""


def document_date(doc: dict[str, Any]) -> str:
    invoice = doc.get("invoice") or {}
    classification = doc.get("classification") or {}
    return classification.get("expense_date") or invoice.get("issue_date") or ""


def document_type(doc: dict[str, Any]) -> str:
    invoice = doc.get("invoice") or {}
    invoice_type = invoice.get("invoice_type")
    if invoice_type:
        return label(invoice_type, INVOICE_TYPE_LABELS)
    subtype = doc.get("document_subtype", "")
    return label(subtype, SUBTYPE_LABELS)


def document_seller(doc: dict[str, Any]) -> str:
    invoice = doc.get("invoice") or {}
    if invoice.get("seller_name"):
        return invoice["seller_name"]
    if doc.get("document_subtype") == "didi_trip_report":
        return C["didi"]
    if doc.get("document_subtype") == "gaode_trip_report":
        return C["gaode"]
    return ""


def document_status(doc: dict[str, Any]) -> str:
    if doc.get("needs_review"):
        issues = doc.get("issues") or []
        if issues:
            first = issues[0]
            return "\u9700\u590d\u6838: " + clean(first.get("problem", ""))
        return "\u9700\u590d\u6838"
    if doc.get("ocr_required"):
        return "\u9700OCR\u590d\u6838"
    return "\u5df2\u8bc6\u522b"


def review_row(doc: dict[str, Any]) -> dict[str, str]:
    classification = doc.get("classification") or {}
    invoice = doc.get("invoice") or {}
    category = classification.get("expense_category", "")
    return {
        "user_no": str(document_user_no(doc)),
        "file": Path(doc.get("source_file", "")).name,
        "role": label(doc.get("document_role", ""), ROLE_LABELS),
        "type": document_type(doc),
        "invoice_no": invoice.get("invoice_no", ""),
        "seller": document_seller(doc),
        "date": document_date(doc),
        "amount": document_amount(doc),
        "category": label(category, CATEGORY_LABELS),
        "status": document_status(doc),
    }


def applicant_review_line(doc: dict[str, Any]) -> str:
    row = review_row(doc)
    parts = [
        f"\u7b2c{row['user_no']}\u5f20",
        f"\u6587\u4ef6 {row['file']}",
        f"\u89d2\u8272 {row['role']}",
        f"\u7c7b\u578b {row['type']}",
    ]
    if row["invoice_no"]:
        parts.append(f"\u53d1\u7968\u53f7 {row['invoice_no']}")
    if row["seller"]:
        parts.append(f"\u5f00\u5177\u65b9/\u670d\u52a1\u65b9 {row['seller']}")
    if row["date"]:
        parts.append(f"\u65e5\u671f {row['date']}")
    if row["amount"]:
        parts.append(f"\u91d1\u989d {row['amount']}")
    if row["category"]:
        parts.append(f"\u521d\u5206 {row['category']}")
    parts.append(f"\u72b6\u6001 {row['status']}")
    return " | ".join(parts)


def print_input_reconciliation(payload: dict[str, Any], files: list[Path], skipped: list[Path]) -> None:
    """Print a mechanical accounting of every input file.

    The agent must copy or summarize this block in chat. Every file the user
    provided has to end up in exactly one bucket: indexed, needing manual
    transcription, or skipped-unsupported. Nothing is allowed to disappear.
    """
    documents = payload.get("documents", [])
    manual_docs = [doc for doc in documents if doc.get("extraction_method") == "manual_review"]
    print("")
    print("INPUT RECONCILIATION TO SHOW IN CHAT:")
    print(f"- Files received: {len(files) + len(skipped)}")
    print(f"- Indexed for extraction: {len(documents)}")
    print(f"- Needing manual review/transcription (no usable text layer or OCR): {len(manual_docs)}")
    for doc in manual_docs:
        print(f"  * {doc.get('document_id', '?')}: {Path(str(doc.get('source_file', '?'))).name}")
    unresolved = [item for item in payload.get("unresolved_input_files", []) if item.get("status") == "open"]
    resolved = [item for item in payload.get("unresolved_input_files", []) if item.get("status") != "open"]
    print(f"- Unsupported input files awaiting an explicit decision: {len(unresolved)}")
    for item in unresolved:
        print(f"  * {item.get('filename')} (unsupported suffix {item.get('suffix') or '<none>'}; sha256 {item.get('sha256')})")
    if resolved:
        print(f"- Previously resolved unsupported input files: {len(resolved)}")
    if manual_docs:
        print("ACTION: read each file visually if you can, then record what you saw via")
        print("scripts/apply_extraction_corrections.py. If you cannot read it and OCR is")
        print("unavailable, ask the user in chat with this template (batch all items):")
        print("---")
        print(f"有 {len(manual_docs)} 个文件我这边无法自动识别（图片/扫描件）。")
        print("麻烦你对照原件告诉我每一项是什么：发票（请给发票号码、开票日期、销售方、金额）、")
        print("合伙人审批截图、付款凭证（小票/支付宝/微信截图），还是其他？不报销的也请说明。")
        for doc in manual_docs:
            print(f"- {Path(str(doc.get('source_file', '?'))).name}")
        print("---")
        print("Then write the answers back with apply_extraction_corrections.py; they persist")
        print("across extractor re-runs. Do NOT hand-edit invoice-extraction.json.")
    if unresolved:
        print("ACTION: tell the user these files were not processed and ask whether they are")
        print("reimbursement evidence that needs converting (e.g. OFD/eml to PDF) or can be excluded.")
        print("Record the user's decision through apply_extraction_corrections.py under input_resolutions;")
        print("unsupported inputs remain a hard blocker until that decision is persisted.")
    if manual_docs or unresolved:
        print("NEXT: resolve the items above (vision/OCR/ask user + apply_extraction_corrections.py) "
              "BEFORE running allocate_expenses.py.")
    else:
        print("NEXT: relay the extraction review list below to the user, then run allocate_expenses.py.")


def print_extraction_review_list(payload: dict[str, Any]) -> None:
    docs = payload.get("documents", [])
    if not docs:
        return
    print("")
    print("EXTRACTION REVIEW LIST TO SHOW IN CHAT:")
    print("Copy or summarize this list so the user can confirm recognized invoices by number and source filename.")
    for doc in docs:
        print(applicant_review_line(doc))


def write_markdown(output_dir: Path, payload: dict[str, Any]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "invoice-extraction.md"
    docs = payload["documents"]
    batch = payload["batch"]
    lines: list[str] = []
    lines.append("# Invoice Extraction Process")
    lines.append("")
    lines.append(f"Generated at: {payload['generated_at']}")
    lines.append(f"Input files: {batch['input_count']}")
    lines.append(f"Indexed for extraction: {batch['indexed_input_count']}")
    lines.append(f"Unsupported inputs awaiting decision: {batch['unresolved_input_count']}")
    lines.append(f"Documents needing review: {batch['review_count']}")
    lines.append("")
    lines.append("## Batch Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | ---: |")
    lines.append(f"| Invoice documents | {batch['invoice_count']} |")
    lines.append(f"| Supporting schedules | {batch['supporting_schedule_count']} |")
    lines.append(f"| Unknown documents | {batch['unknown_count']} |")
    lines.append(f"| Total invoice amount | {batch['total_invoice_amount']} |")
    lines.append("")
    lines.append("## Unsupported Input Files")
    lines.append("")
    lines.append("Every supplied file remains evidence until it is explicitly excluded or replaced with a readable conversion.")
    lines.append("")
    lines.append("| File | Suffix | SHA-256 | Status | Resolution | Replacement |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for item in payload.get("unresolved_input_files", []):
        lines.append(
            f"| {md_escape(item.get('filename'))} | {md_escape(item.get('suffix'))} | "
            f"{md_escape(item.get('sha256'))} | {md_escape(item.get('status'))} | "
            f"{md_escape(item.get('resolution'))} | {md_escape(item.get('replacement_file'))} |"
        )
    if not payload.get("unresolved_input_files"):
        lines.append("| None |  |  |  |  |  |")
    lines.append("")
    lines.append("## Applicant Review List")
    lines.append("")
    lines.append("| No. | File | Role | Type | Invoice No. | Seller/Provider | Date | Amount | Category | Status |")
    lines.append("| ---: | --- | --- | --- | --- | --- | --- | ---: | --- | --- |")
    for doc in docs:
        row = review_row(doc)
        lines.append(
            f"| {md_escape(row['user_no'])} | {md_escape(row['file'])} | {md_escape(row['role'])} | "
            f"{md_escape(row['type'])} | {md_escape(row['invoice_no'])} | {md_escape(row['seller'])} | "
            f"{md_escape(row['date'])} | {md_escape(row['amount'])} | {md_escape(row['category'])} | "
            f"{md_escape(row['status'])} |"
        )
    lines.append("")
    lines.append("## Document Index")
    lines.append("")
    lines.append("| ID | File | Role | Subtype | Category | Amount | Date | Needs Review |")
    lines.append("| --- | --- | --- | --- | --- | ---: | --- | --- |")
    for doc in docs:
        invoice = doc.get("invoice") or {}
        classification = doc.get("classification") or {}
        amount = invoice.get("total_amount") or (doc.get("supporting_schedule") or {}).get("reported_total_amount", "")
        date = classification.get("expense_date") or invoice.get("issue_date") or ""
        lines.append(
            f"| {md_escape(doc['document_id'])} | {md_escape(Path(doc['source_file']).name)} | "
            f"{md_escape(doc['document_role'])} | {md_escape(doc['document_subtype'])} | "
            f"{md_escape(classification.get('expense_category', ''))} | {md_escape(amount)} | "
            f"{md_escape(date)} | {'Yes' if doc.get('needs_review') else 'No'} |"
        )
    lines.append("")
    lines.append("## Extracted Documents")
    for doc in docs:
        invoice = doc.get("invoice") or {}
        classification = doc.get("classification") or {}
        lines.append("")
        lines.append(f"### {doc['document_id']} - {Path(doc['source_file']).name}")
        lines.append("")
        lines.append(f"- Role: {doc.get('document_role', '')}")
        lines.append(f"- Subtype: {doc.get('document_subtype', '')}")
        lines.append(f"- Extraction method: {doc.get('extraction_method', '')}")
        lines.append(f"- OCR required: {doc.get('ocr_required', False)}")
        lines.append(f"- Invoice no: {invoice.get('invoice_no', '')}")
        lines.append(f"- Invoice type: {invoice.get('invoice_type', '')}")
        lines.append(f"- Issue date: {invoice.get('issue_date', '')}")
        lines.append(f"- Buyer: {invoice.get('buyer_name', '')}")
        lines.append(f"- Seller: {invoice.get('seller_name', '')}")
        lines.append(f"- Amount: {invoice.get('total_amount', '')}")
        lines.append(f"- Category: {classification.get('expense_category', '')}")
        lines.append(f"- Expense note: {classification.get('expense_note', '')}")
        railway_leg = classification.get("railway_leg") or {}
        if railway_leg:
            lines.append(
                "- Railway leg: "
                f"{railway_leg.get('train_no', '') or '-'} | "
                f"{railway_leg.get('origin_station', '') or '-'} -> {railway_leg.get('destination_station', '') or '-'} | "
                f"{railway_leg.get('departure_datetime', '') or railway_leg.get('travel_date', '') or '-'}"
            )
            if railway_leg.get("is_refund_fee"):
                lines.append(f"- Railway refund fee: {railway_leg.get('refund_fee_amount', '') or 'NEEDS REVIEW'}")
        lines.append(f"- Raw remarks: {invoice.get('raw_remarks', '')}")
        lines.append(f"- Confidence: {doc.get('confidence', '')}")
        lines.append(f"- Issues: {len(doc.get('issues', []))}")
        lines.append("- Evidence: ")
        if doc.get("supporting_items"):
            lines.append("")
            lines.append("#### Supporting Items")
            lines.append("")
            lines.append("| Item ID | Date/Time | City | Origin | Destination | Amount | Note |")
            lines.append("| --- | --- | --- | --- | --- | ---: | --- |")
            for item in doc["supporting_items"]:
                lines.append(
                    f"| {md_escape(item.get('item_id'))} | {md_escape(item.get('ride_datetime'))} | "
                    f"{md_escape(item.get('city'))} | {md_escape(item.get('origin'))} | "
                    f"{md_escape(item.get('destination'))} | {md_escape(item.get('amount'))} | "
                    f"{md_escape(item.get('expense_note'))} |"
                )
    lines.append("")
    lines.append("## Document Links")
    lines.append("")
    lines.append("| Source | Target | Relation | Check |")
    lines.append("| --- | --- | --- | --- |")
    for link in payload.get("document_links", []):
        lines.append(
            f"| {md_escape(link.get('source_document_id'))} | {md_escape(link.get('target_document_id'))} | "
            f"{md_escape(link.get('relation'))} | {md_escape(json.dumps(link.get('check', {}), ensure_ascii=False))} |"
        )
    lines.append("")
    lines.append("## Review Queue")
    lines.append("")
    lines.append("| ID | Field | Problem | Suggested Action |")
    lines.append("| --- | --- | --- | --- |")
    for item in payload.get("review_queue", []):
        lines.append(
            f"| {md_escape(item.get('document_id'))} | {md_escape(item.get('field'))} | "
            f"{md_escape(item.get('problem'))} | {md_escape(item.get('suggested_action'))} |"
        )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def unsupported_input_record(path: Path) -> dict[str, str]:
    return {
        "source_file": str(path),
        "filename": path.name,
        "suffix": path.suffix.lower(),
        "sha256": sha256_file(path),
        "status": "open",
        "resolution": "",
        "replacement_file": "",
    }


def build_payload(input_files: list[Path], skipped_files: list[Path]) -> dict[str, Any]:
    documents = [extract_document(f"DOC-{idx:03d}", path) for idx, path in enumerate(input_files, start=1)]
    links, review_queue = build_links_and_reviews(documents)
    batch = batch_summary(documents, review_queue)
    batch["input_count"] = len(input_files) + len(skipped_files)
    batch["indexed_input_count"] = len(input_files)
    batch["unresolved_input_count"] = len(skipped_files)
    return {
        "schema_version": "invoice_extraction.v1",
        "generated_at": datetime.now().replace(microsecond=0).isoformat(),
        "batch": batch,
        "documents": documents,
        "unresolved_input_files": [unsupported_input_record(path) for path in skipped_files],
        "document_links": links,
        "review_queue": review_queue,
    }


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="Extract reimbursement invoice evidence into process files.")
    parser.add_argument("inputs", nargs="+", help="Input PDF/image files or folders.")
    parser.add_argument("--output", "-o", default="process", help="Output process folder.")
    args = parser.parse_args(argv)

    input_paths = [Path(item).expanduser() for item in args.inputs]
    files, skipped = iter_input_files(input_paths)
    if not files and not skipped:
        print("No supported PDF or image files found.", file=sys.stderr)
        return 2

    payload = build_payload(files, skipped)
    output_dir = Path(args.output)

    # Re-run safety: back up the previous result, then replay saved corrections
    # so vision/user fixes survive re-extraction (see extraction_corrections.py).
    existing = output_dir / "invoice-extraction.json"
    if existing.exists():
        backup = existing.with_suffix(existing.suffix + ".bak")
        backup.write_bytes(existing.read_bytes())
        print(f"Previous extraction backed up to {backup}")
    overlay = xc.load_overlay(output_dir)
    if overlay.get("corrections") or overlay.get("input_resolutions"):
        stamp_ok, stamp_reason = integrity.check(overlay)
        if not stamp_ok:
            print(f"WARNING: {xc.overlay_path(output_dir).name} failed its integrity check "
                  f"({stamp_reason}) — it was edited outside apply_extraction_corrections.py. "
                  "Schema-valid entries will still replay, but prefer the script so entries are "
                  "validated and stamped.", file=sys.stderr)
        valid_entries = []
        for idx, entry in enumerate(overlay["corrections"], start=1):
            entry_errors = xc.validate_correction(entry)
            if entry_errors:
                print(f"WARNING: skipping invalid overlay correction #{idx} "
                      f"(overlay was edited outside apply_extraction_corrections.py?): "
                      f"{'; '.join(entry_errors)}", file=sys.stderr)
            else:
                valid_entries.append(entry)
        valid_input_resolutions = []
        for idx, entry in enumerate(overlay["input_resolutions"], start=1):
            entry_errors = xc.validate_input_resolution(entry)
            if entry_errors:
                print(f"WARNING: skipping invalid overlay input resolution #{idx} "
                      f"(overlay was edited outside apply_extraction_corrections.py?): "
                      f"{'; '.join(entry_errors)}", file=sys.stderr)
            else:
                valid_input_resolutions.append(entry)
        if valid_entries:
            replay_log = xc.apply_overlay(payload, {"corrections": valid_entries})
            print(f"Replayed {len(valid_entries)} saved correction(s) from {xc.overlay_path(output_dir).name}:")
            for line in replay_log:
                print(f"  {line}")
        if valid_input_resolutions:
            replay_log = xc.apply_input_resolutions(payload, {"input_resolutions": valid_input_resolutions})
            print(f"Replayed {len(valid_input_resolutions)} saved input resolution(s) from {xc.overlay_path(output_dir).name}:")
            for line in replay_log:
                print(f"  {line}")

    integrity.stamp(payload, "extract_invoices.py")
    json_path = write_json(output_dir, payload)
    md_path = write_markdown(output_dir, payload)
    print(f"Wrote {md_path}")
    print(f"Wrote {json_path}")
    print_input_reconciliation(payload, files, skipped)
    print_extraction_review_list(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
