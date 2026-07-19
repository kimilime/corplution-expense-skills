from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import place_config  # noqa: E402
from place_config import PlaceBook  # noqa: E402
import allocate_expenses  # noqa: E402
import apply_allocation_answers  # noqa: E402


class LookupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.book = PlaceBook(
            [
                {"name": "友力国际大厦", "aliases": ["江宁路"], "place_type": "公司"},
                {"name": "中关村产业园", "aliases": [], "place_type": "客户", "client_name": "某某科技"},
            ],
            Path("unused.json"),
        )

    def test_name_substring_hit(self) -> None:
        self.assertEqual(self.book.lookup("上海友力国际大厦B座"), ("公司", "high", False))

    def test_alias_hit(self) -> None:
        self.assertEqual(self.book.lookup("江宁路100号"), ("公司", "high", False))

    def test_client_place_hit(self) -> None:
        self.assertEqual(self.book.lookup("海淀区中关村产业园3号楼"), ("客户", "high", False))

    def test_unknown_place_returns_none(self) -> None:
        self.assertIsNone(self.book.lookup("某某小区"))

    def test_empty_text_returns_none(self) -> None:
        self.assertIsNone(self.book.lookup(""))


class FailOpenTests(unittest.TestCase):
    # No private facts are hard-coded anymore: fail-open means an empty, usable book
    # (allocation continues; unknown places are simply asked), NOT a built-in office.
    def test_missing_file_is_empty_book_no_raise(self) -> None:
        book = PlaceBook.load(Path("this-file-does-not-exist.json"))
        self.assertEqual(book.entries, [])
        self.assertIsNone(book.lookup("友力国际大厦"))

    def test_malformed_file_is_empty_book_no_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "place-definitions.json"
            bad.write_text("{ not valid json ", encoding="utf-8")
            book = PlaceBook.load(bad)
            self.assertEqual(book.entries, [])

    def test_wrong_schema_version_is_empty_book(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "place-definitions.json"
            bad.write_text(json.dumps({"schema_version": "nope.v9", "places": [
                {"name": "友力国际大厦", "place_type": "公司"}]}), encoding="utf-8")
            book = PlaceBook.load(bad)
            self.assertEqual(book.entries, [])

    def test_valid_file_resolves_office(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "place-definitions.json"
            good.write_text(json.dumps({
                "schema_version": place_config.SCHEMA_VERSION,
                "places": [{"name": "友力国际大厦", "aliases": ["江宁路"], "place_type": "公司"}],
            }, ensure_ascii=False), encoding="utf-8")
            book = PlaceBook.load(good)
            self.assertEqual(book.lookup("江宁路100号"), ("公司", "high", False))


class PublicPlaceTests(unittest.TestCase):
    def test_public_places_recognized_without_memory(self) -> None:
        self.assertEqual(place_config.public_place_type("虹桥T2航站楼"), "机场")
        self.assertEqual(place_config.public_place_type("上海虹桥火车站"), "火车站")
        self.assertEqual(place_config.public_place_type("全季酒店"), "酒店")

    def test_private_place_is_not_public(self) -> None:
        self.assertIsNone(place_config.public_place_type("友力国际大厦"))
        self.assertIsNone(place_config.public_place_type("绿地云庭小区"))


class RememberTests(unittest.TestCase):
    def test_append_and_dedup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "place-definitions.json"
            book = PlaceBook.load(path)  # missing -> built-ins only, empty file
            added = book.remember([{"name": "环球金融中心", "place_type": "客户"}], path=path)
            self.assertEqual(added, 1)
            # Same name+type is not re-added.
            again = book.remember([{"name": "环球金融中心", "place_type": "客户"}], path=path)
            self.assertEqual(again, 0)
            data = json.loads(path.read_text(encoding="utf-8"))
            names = [p["name"] for p in data["places"]]
            self.assertEqual(names.count("环球金融中心"), 1)
            self.assertEqual(data["schema_version"], place_config.SCHEMA_VERSION)

    def test_invalid_entries_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "place-definitions.json"
            book = PlaceBook.load(path)
            added = book.remember(
                [
                    {"name": "X", "place_type": "公司"},  # name too short
                    {"name": "有效大厦", "place_type": "外星"},  # bad type
                    {"name": "有效大厦", "place_type": "公司"},  # good
                ],
                path=path,
            )
            self.assertEqual(added, 1)

    def test_remember_never_raises_on_bad_path(self) -> None:
        # A directory path cannot be written as a file; must fail open to 0.
        with tempfile.TemporaryDirectory() as tmp:
            book = PlaceBook.load(Path(tmp) / "missing.json")
            added = book.remember([{"name": "测试大厦", "place_type": "公司"}], path=Path(tmp))
            self.assertEqual(added, 0)


class ClassifyHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.book = PlaceBook(
            [{"name": "友力国际大厦", "aliases": ["江宁路"], "place_type": "公司"}],
            Path("unused.json"),
        )

    def test_memory_wins_high_confidence(self) -> None:
        self.assertEqual(
            allocate_expenses.classify_place_type("友力国际大厦", [], place_book=self.book),
            ("公司", "high", False),
        )

    def test_public_place_still_inferred(self) -> None:
        ptype, conf, need = allocate_expenses.classify_place_type("虹桥火车站", [], place_book=self.book)
        self.assertEqual(ptype, "火车站")
        self.assertFalse(need)

    def test_unknown_private_place_asks(self) -> None:
        ptype, conf, need = allocate_expenses.classify_place_type("某某小区3号楼", [], place_book=self.book)
        self.assertTrue(need)


class CollectPlaceMemoryTests(unittest.TestCase):
    def test_collects_only_explicit_confirmations(self) -> None:
        unit = {
            "origin": "友力国际大厦",
            "destination": "绿地云庭小区",
            "origin_place_type": "公司",
            "destination_place_type": "家",
        }
        sink: list[dict] = []
        # Explicit user answer set both endpoint types (both private -> both memorized).
        apply_allocation_answers.collect_place_memory(
            unit, {"origin_place_type": "公司", "destination_type": "家"}, sink
        )
        self.assertEqual(
            sorted((e["name"], e["place_type"]) for e in sink),
            [("友力国际大厦", "公司"), ("绿地云庭小区", "家")],
        )

    def test_no_explicit_type_records_nothing(self) -> None:
        unit = {"origin": "友力国际大厦", "origin_place_type": "公司"}
        sink: list[dict] = []
        # This answer only set the date; place type was a model guess, not confirmed.
        apply_allocation_answers.collect_place_memory(unit, {"expense_date": "2026-07-02"}, sink)
        self.assertEqual(sink, [])

    def test_public_endpoint_is_not_memorized(self) -> None:
        unit = {
            "origin": "友力国际大厦",
            "destination": "虹桥T2航站楼",
            "origin_place_type": "公司",
            "destination_place_type": "机场",
        }
        sink: list[dict] = []
        apply_allocation_answers.collect_place_memory(
            unit, {"origin_place_type": "公司", "destination_place_type": "机场"}, sink
        )
        # Only the private office is memorized; the public airport is skipped.
        self.assertEqual(sink, [{"name": "友力国际大厦", "place_type": "公司"}])


if __name__ == "__main__":
    unittest.main()
