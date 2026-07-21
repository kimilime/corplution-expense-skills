from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "corplution-reimbursement-wizard" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import allocate_expenses as allocator  # noqa: E402
import apply_allocation_answers as updater  # noqa: E402
import extract_invoices as extractor  # noqa: E402
import write_reimbursement_template as writer  # noqa: E402


def project(context_id: str, city: str, client: str, code: str) -> dict:
    return {
        "context_id": context_id,
        "date_start": "2026-06-08",
        "date_end": "2026-06-10",
        "travel_buffer_days": 1,
        "city": city,
        "client_name": client,
        "client_charge_code": code,
        "project_description": f"{client} project",
    }


def rail_unit(
    number: int,
    origin: str,
    destination: str,
    departure_time: str,
    *,
    travel_date: str = "2026-06-08",
    refund: bool = False,
) -> dict:
    route = f"{origin}-{destination}"
    note_prefix = "高铁退票费" if refund else "高铁"
    evidence_suffix = ", 退票费" if refund else ""
    return {
        "unit_id": f"UNIT-{number:03d}",
        "user_no": number,
        "source_document_id": f"DOC-{number:03d}",
        "source_filename": f"rail-{number}.pdf",
        "supporting_invoice_document_id": f"DOC-{number:03d}",
        "supporting_invoice_filename": f"rail-{number}.pdf",
        "source_category": "travel",
        "document_subtype": "railway_e_ticket",
        "train_no": f"G{100 + number}",
        "origin_station": origin,
        "destination_station": destination,
        "route": route,
        "rail_departure_time": departure_time,
        "rail_departure_datetime": f"{travel_date} {departure_time}",
        "expense_date": travel_date,
        "date_source": "railway_travel_date",
        "date_required": False,
        "date_is_provisional": False,
        "source_note": f"G{100 + number}, {origin} -> {destination}, {travel_date} {departure_time}, 二等座{evidence_suffix}",
        "expense_note": f"G{100 + number}, {origin} -> {destination}, {travel_date} {departure_time}, 二等座{evidence_suffix}",
        "final_note": f"{note_prefix}（{route}）",
        "final_template_column": "travel",
        "is_refund_fee": refund,
        "refund_fee_amount": "100.00" if refund else "",
        "amount": "100.00",
        "invoice_amount": "100.00",
        "reimbursable_amount": "",
        "project_context_id": "",
        "client_name": "",
        "client_charge_code": "",
        "confidence": "low",
        "status": "draft",
        "issues": [],
    }


class RailwayExtractionTests(unittest.TestCase):
    def test_extracts_structured_railway_leg(self) -> None:
        text = "铁路电子客票 上海虹桥 G1234 周口东 2026年6月8日 08:05 二等座 票价￥318.00"
        leg = extractor.parse_railway_leg(text)
        self.assertEqual("G1234", leg["train_no"])
        self.assertEqual("上海虹桥", leg["origin_station"])
        self.assertEqual("周口东", leg["destination_station"])
        self.assertEqual("2026-06-08 08:05", leg["departure_datetime"])
        self.assertEqual("上海虹桥-周口东", leg["route"])

    def test_refund_fee_is_the_invoice_and_reimbursement_amount(self) -> None:
        examples = [
            ("￥34.00\n退票费:", "34.00"),
            ("退票费: ￥63.50", "63.50"),
        ]
        for amount_text, expected in examples:
            with self.subTest(amount=expected):
                text = (
                    "铁路电子客票 郑州东 G1950 滁州 "
                    "2026年07月10日 18:15开 二等座\n"
                    f"{amount_text}\n电子客票号:123 退票"
                )
                self.assertEqual(expected, extractor.parse_railway_refund_fee_amount(text))
                self.assertEqual(expected, extractor.parse_total_amount(text, [], "railway_e_ticket"))
                leg = extractor.parse_railway_leg(text)
                self.assertTrue(leg["is_refund_fee"])
                self.assertEqual(expected, leg["refund_fee_amount"])
                self.assertIn(f"退票费 ¥{expected}", extractor.railway_note(text))

    def test_blank_refund_fee_is_unresolved_not_zero_or_ticket_price(self) -> None:
        text = (
            "铁路电子客票 郑州东 G1950 滁州 2026年07月10日 18:15开\n"
            "票价: ￥491.00\n退票费:\n电子客票号:123 退票"
        )
        self.assertEqual("", extractor.parse_railway_refund_fee_amount(text))
        self.assertEqual("", extractor.parse_total_amount(text, [], "railway_e_ticket"))

    def test_allocator_prefers_structured_refund_fee_over_other_ticket_amount(self) -> None:
        extraction = {
            "documents": [{
                "document_id": "DOC-001",
                "source_file": "refund.pdf",
                "document_role": "invoice",
                "document_subtype": "railway_e_ticket",
                "invoice": {
                    "invoice_no": "123",
                    "issue_date": "2026-07-12",
                    "total_amount": "491.00",
                    "seller_name": "",
                    "raw_remarks": "",
                    "line_item_name": "",
                },
                "classification": {
                    "expense_category": "travel",
                    "expense_date": "2026-07-10",
                    "expense_date_source": "railway_travel_date",
                    "expense_note": "G1950, 郑州东 -> 滁州, 2026-07-10 18:15, 退票费 ¥63.50",
                    "railway_leg": {
                        "train_no": "G1950",
                        "travel_date": "2026-07-10",
                        "departure_time": "18:15",
                        "departure_datetime": "2026-07-10 18:15",
                        "origin_station": "郑州东",
                        "destination_station": "滁州",
                        "route": "郑州东-滁州",
                        "is_refund_fee": True,
                        "refund_fee_amount": "63.50",
                    },
                },
            }],
            "links": [],
        }
        units, _ = allocator.create_units(extraction, [])
        self.assertEqual(1, len(units))
        self.assertEqual("63.50", units[0]["amount"])
        self.assertEqual("63.50", units[0]["invoice_amount"])
        self.assertEqual("63.50", units[0]["refund_fee_amount"])
        self.assertEqual("高铁退票费（郑州东-滁州）", units[0]["final_note"])


class FlightTicketNormalizationTests(unittest.TestCase):
    def test_airline_vat_invoice_uses_line_item_and_unicode_arrow_route(self) -> None:
        unit = {
            "source_category": "travel",
            "document_subtype": "vat_special_invoice",
            "seller_name": "中国东方航空股份有限公司",
            "line_item_name": "*交通运输服务*国内航空",
            "source_note": "6.24 太原→上海虹桥 东航 1300(自订)",
            "expense_note": "6.24 太原→上海虹桥 东航 1300(自订)",
            "final_note": "6.24 太原→上海虹桥 东航 1300(自订)",
            "route": "",
        }

        self.assertTrue(updater.is_flight_ticket(unit))
        self.assertEqual("太原-上海虹桥", updater.route_from_text(unit["source_note"]))
        self.assertEqual("飞机（太原-上海虹桥）", allocator.normal_note(unit))
        self.assertEqual("飞机（太原-上海虹桥）", updater.ticket_note(unit))
        self.assertEqual("飞机（太原-上海虹桥）", writer.ticket_note(unit))
        self.assertEqual("flight", writer.proof_type(unit))

    def test_airport_merchant_name_does_not_turn_a_meal_into_a_flight(self) -> None:
        unit = {
            "source_category": "meal",
            "seller_name": "中图餐饮郑州航空港区有限公司",
            "line_item_name": "*生产生活服务*餐饮服务",
        }
        self.assertFalse(updater.is_flight_ticket(unit))


class RailJourneyChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.zhoukou = project("CTX-ZK", "周口", "周口项目", "CORP-ZK")
        self.zhengzhou = project("CTX-ZZ", "郑州", "千味央厨", "CORP-QW")
        self.taiyuan = project("CTX-TY", "太原", "山西信托", "CORP-SX")

    def test_outbound_transfer_uses_terminal_project_when_intermediate_has_no_project(self) -> None:
        units = [
            rail_unit(1, "上海虹桥", "周口东", "08:00"),
            rail_unit(2, "周口", "郑州东", "10:00"),
        ]
        allocator.apply_matches(units, [self.zhengzhou])

        self.assertEqual({"RAIL-CHAIN-001"}, {unit["journey_chain_id"] for unit in units})
        self.assertEqual({"CTX-ZZ"}, {unit["project_context_id"] for unit in units})
        self.assertEqual({"千味央厨"}, {unit["client_name"] for unit in units})
        self.assertEqual({"rail_transfer_chain_destination"}, {unit["auto_project_match"] for unit in units})
        self.assertEqual("上海虹桥 -> 周口东 -> 郑州东", units[0]["journey_chain_route"])
        self.assertEqual("高铁（上海虹桥-周口东）", units[0]["final_note"])
        self.assertEqual("高铁（周口-郑州东）", units[1]["final_note"])

    def test_intermediate_city_project_requires_transfer_or_stop_review(self) -> None:
        units = [
            rail_unit(1, "上海虹桥", "周口东", "08:00"),
            rail_unit(2, "周口", "郑州东", "10:00"),
        ]
        allocator.apply_matches(units, [self.zhoukou, self.zhengzhou])
        questions = allocator.build_questions(units, [])

        self.assertEqual({"RAIL-CHAIN-001"}, {unit["journey_chain_id"] for unit in units})
        self.assertEqual({""}, {unit["project_context_id"] for unit in units})
        self.assertTrue(all(unit["journey_chain_needs_confirmation"] for unit in units))
        self.assertEqual(
            {"rail_transfer_chain_intermediate_project_review"},
            {unit["journey_chain_assignment_rule"] for unit in units},
        )
        open_questions = [question for question in questions if question.get("status") == "open"]
        self.assertEqual(1, len(open_questions))
        self.assertIn("实际停留/项目活动", open_questions[0]["question"])

    def test_return_transfer_uses_project_just_completed(self) -> None:
        units = [
            rail_unit(1, "郑州东", "周口东", "08:00"),
            rail_unit(2, "周口东", "上海虹桥", "10:00"),
        ]
        allocator.apply_matches(units, [self.zhengzhou])

        self.assertEqual({"CTX-ZZ"}, {unit["project_context_id"] for unit in units})
        self.assertEqual({"rail_transfer_chain_return"}, {unit["auto_project_match"] for unit in units})

    def test_refund_transfer_chain_uses_normal_route_assignment(self) -> None:
        units = [
            rail_unit(1, "郑州东", "滁州", "18:15", travel_date="2026-07-10", refund=True),
            rail_unit(2, "滁州", "上海虹桥", "21:04", travel_date="2026-07-10", refund=True),
        ]
        context = project("CTX-ZZ", "郑州", "千味央厨", "CORP-QW")
        context["date_end"] = "2026-07-10"
        allocator.apply_matches(units, [context])

        self.assertEqual({"RAIL-CHAIN-001"}, {unit["journey_chain_id"] for unit in units})
        self.assertEqual({"CTX-ZZ"}, {unit["project_context_id"] for unit in units})
        self.assertEqual({"rail_transfer_chain_return"}, {unit["auto_project_match"] for unit in units})
        self.assertEqual(
            ["高铁退票费（郑州东-滁州）", "高铁退票费（滁州-上海虹桥）"],
            [unit["final_note"] for unit in units],
        )

    def test_refund_and_travelled_tickets_do_not_form_one_chain(self) -> None:
        units = [
            rail_unit(1, "郑州东", "滁州", "18:15", refund=True),
            rail_unit(2, "滁州", "上海虹桥", "21:04"),
        ]
        self.assertEqual([], allocator.build_rail_journey_chains(units))

    def test_project_to_project_transfer_uses_project_being_traveled_to(self) -> None:
        units = [
            rail_unit(1, "太原南", "周口东", "07:00"),
            rail_unit(2, "周口东", "郑州东", "09:00"),
        ]
        allocator.apply_matches(units, [self.taiyuan, self.zhengzhou])

        self.assertEqual({"CTX-ZZ"}, {unit["project_context_id"] for unit in units})

    def test_three_connected_segments_share_one_chain_and_project(self) -> None:
        units = [
            rail_unit(1, "上海虹桥", "南京南", "06:00"),
            rail_unit(2, "南京南", "周口东", "08:00"),
            rail_unit(3, "周口东", "郑州东", "10:00"),
        ]
        allocator.apply_matches(units, [self.zhengzhou])
        self.assertEqual({"RAIL-CHAIN-001"}, {unit["journey_chain_id"] for unit in units})
        self.assertEqual({3}, {unit["journey_chain_length"] for unit in units})
        self.assertEqual({"CTX-ZZ"}, {unit["project_context_id"] for unit in units})

    def test_disconnected_or_reverse_time_tickets_do_not_form_chain(self) -> None:
        disconnected = [
            rail_unit(1, "上海虹桥", "周口东", "08:00"),
            rail_unit(2, "开封北", "郑州东", "13:00"),
        ]
        reverse_time = [
            rail_unit(3, "上海虹桥", "周口东", "13:00"),
            rail_unit(4, "周口东", "郑州东", "08:00"),
        ]
        self.assertEqual([], allocator.build_rail_journey_chains(disconnected))
        self.assertEqual([], allocator.build_rail_journey_chains(reverse_time))

    def test_branching_transfer_candidates_do_not_create_partial_chain(self) -> None:
        units = [
            rail_unit(1, "上海虹桥", "周口东", "08:00"),
            rail_unit(2, "周口东", "郑州东", "10:00"),
            rail_unit(3, "周口东", "漯河西", "10:30"),
        ]
        self.assertEqual([], allocator.build_rail_journey_chains(units))

    def test_missing_times_do_not_auto_form_a_chain(self) -> None:
        units = [
            rail_unit(1, "上海虹桥", "周口东", ""),
            rail_unit(2, "周口东", "郑州东", ""),
        ]
        chains = allocator.build_rail_journey_chains(units)
        self.assertEqual([], chains)

    def test_departure_gap_over_six_hours_does_not_auto_form_a_chain(self) -> None:
        within_window = [
            rail_unit(1, "上海虹桥", "周口东", "08:00"),
            rail_unit(2, "周口东", "郑州东", "14:00"),
        ]
        over_window = [
            rail_unit(3, "上海虹桥", "周口东", "08:00"),
            rail_unit(4, "周口东", "郑州东", "14:01"),
        ]
        self.assertEqual(1, len(allocator.build_rail_journey_chains(within_window)))
        self.assertEqual([], allocator.build_rail_journey_chains(over_window))

    def test_conflicting_user_hint_assignments_prevent_chain_creation(self) -> None:
        units = [
            rail_unit(1, "上海虹桥", "郑州东", "08:00"),
            rail_unit(2, "郑州东", "北京西", "10:00"),
        ]
        beijing = project("CTX-BJ", "北京", "北京项目", "CORP-BJ")
        for unit, ctx in zip(units, [self.zhengzhou, beijing]):
            unit.update({
                "project_context_id": ctx["context_id"],
                "client_name": ctx["client_name"],
                "client_charge_code": ctx["client_charge_code"],
                "auto_project_match": "user_context_expense_hint",
                "status": "confirmed",
            })

        allocator.apply_matches(units, [self.zhengzhou, beijing])

        self.assertEqual({""}, {unit.get("journey_chain_id", "") for unit in units})
        self.assertEqual(["CTX-ZZ", "CTX-BJ"], [unit["project_context_id"] for unit in units])

    def test_old_extraction_note_without_structured_leg_fields_still_forms_chain(self) -> None:
        units = [
            rail_unit(1, "上海虹桥", "周口东", "08:00"),
            rail_unit(2, "周口东", "郑州东", "10:00"),
        ]
        for unit in units:
            for field in [
                "train_no",
                "origin_station",
                "destination_station",
                "rail_departure_time",
                "rail_departure_datetime",
            ]:
                unit.pop(field, None)
        chains = allocator.build_rail_journey_chains(units)
        self.assertEqual(1, len(chains))
        self.assertEqual("high", chains[0]["confidence"])

    def test_resolved_chain_is_advisory_not_a_blocking_per_leg_question(self) -> None:
        units = [
            rail_unit(1, "上海虹桥", "周口东", "08:00"),
            rail_unit(2, "周口东", "郑州东", "10:00"),
        ]
        allocator.apply_matches(units, [self.zhengzhou])
        questions = allocator.build_questions(units, [])

        self.assertFalse(any(question.get("status") == "open" for question in questions))
        advisory = [
            question for question in questions
            if question.get("question_type") == "auto_matched_rail_journey_chain_review"
        ]
        self.assertEqual(1, len(advisory))
        self.assertIn("中间站按换乘节点处理", advisory[0]["question"])

    def test_unresolved_chain_asks_once_for_whole_journey(self) -> None:
        units = [
            rail_unit(1, "上海虹桥", "周口东", "08:00"),
            rail_unit(2, "周口东", "漯河西", "10:00"),
        ]
        allocator.apply_matches(units, [self.zhengzhou, self.taiyuan])
        questions = allocator.build_questions(units, [])
        open_questions = [question for question in questions if question.get("status") == "open"]

        self.assertEqual(1, len(open_questions))
        self.assertEqual("rail_journey_chain_assignment", open_questions[0]["question_type"])
        self.assertEqual(["UNIT-001", "UNIT-002"], open_questions[0]["unit_ids"])

    def test_stage3_accepts_one_chain_assignment_and_rejects_split_assignment(self) -> None:
        units = [
            rail_unit(1, "上海虹桥", "周口东", "08:00"),
            rail_unit(2, "周口东", "郑州东", "10:00"),
        ]
        contexts = [self.zhengzhou]
        allocator.apply_matches(units, contexts)
        allocation = {"project_contexts": contexts, "questions": [], "allocation_units": units}

        self.assertEqual([], writer.require_ready(allocation, allow_unconfirmed=False))
        rows = writer.make_rows(units, "Test Requester")
        self.assertEqual(["高铁（上海虹桥-周口东）", "高铁（周口东-郑州东）"], [row["note"] for row in rows])
        self.assertEqual({"RAIL-CHAIN-001"}, {row["journey_chain_id"] for row in rows})

        units[0]["project_context_id"] = "CTX-ZK"
        units[0]["client_name"] = "周口项目"
        units[0]["client_charge_code"] = "CORP-ZK"
        errors = writer.require_ready(allocation, allow_unconfirmed=False)
        self.assertTrue(any("different project assignments" in error for error in errors))

    def test_updater_splits_chain_when_user_declares_different_projects(self) -> None:
        units = [
            rail_unit(1, "上海虹桥", "郑州东", "08:00"),
            rail_unit(2, "郑州东", "北京西", "10:00"),
        ]
        beijing = project("CTX-BJ", "北京", "北京项目", "CORP-BJ")
        allocator.apply_matches(units, [beijing])
        self.assertEqual({"RAIL-CHAIN-001"}, {unit["journey_chain_id"] for unit in units})

        units[0].update({
            "project_context_id": "CTX-ZZ",
            "client_name": "千味央厨",
            "client_charge_code": "CORP-QW",
        })
        updater.refresh_rail_chain_assignments(
            {"allocation_units": units}, {"UNIT-001"}
        )

        self.assertEqual({""}, {unit.get("journey_chain_id", "") for unit in units})
        self.assertEqual(["CTX-ZZ", "CTX-BJ"], [unit["project_context_id"] for unit in units])
        self.assertEqual([], writer.rail_chain_ready_errors(units))

    def test_stage3_rejects_stale_chain_after_route_correction_breaks_connection(self) -> None:
        units = [
            rail_unit(1, "上海虹桥", "周口东", "08:00"),
            rail_unit(2, "周口东", "郑州东", "10:00"),
        ]
        allocator.apply_matches(units, [self.zhengzhou])
        units[1]["route"] = "开封北-郑州东"
        errors = writer.rail_chain_ready_errors(units)
        self.assertTrue(any("no longer continuous" in error for error in errors))

    def test_stage3_rejects_truncated_three_leg_chain_after_drop(self) -> None:
        units = [
            rail_unit(1, "上海虹桥", "南京南", "06:00"),
            rail_unit(2, "南京南", "周口东", "08:00"),
            rail_unit(3, "周口东", "郑州东", "10:00"),
        ]
        allocator.apply_matches(units, [self.zhengzhou])
        units[2]["status"] = "dropped"
        updater.refresh_rail_chain_assignments({"allocation_units": units}, {"UNIT-003"})

        errors = writer.rail_chain_ready_errors(units)
        self.assertTrue(any("stale chain length metadata" in error for error in errors))
        self.assertTrue(any("stale chain member metadata" in error for error in errors))

    def test_updater_closes_chain_gate_only_after_all_legs_share_assignment(self) -> None:
        units = [
            rail_unit(1, "上海虹桥", "周口东", "08:00"),
            rail_unit(2, "周口东", "漯河西", "10:00"),
        ]
        allocator.apply_matches(units, [self.zhengzhou, self.taiyuan])
        payload = {"allocation_units": units}

        units[0].update({
            "project_context_id": "CTX-ZZ",
            "client_name": "千味央厨",
            "client_charge_code": "CORP-QW",
        })
        updater.refresh_rail_chain_assignments(payload, {"UNIT-001"})
        self.assertTrue(all(unit["journey_chain_needs_confirmation"] for unit in units))

        units[1].update({
            "project_context_id": "CTX-ZZ",
            "client_name": "千味央厨",
            "client_charge_code": "CORP-QW",
        })
        updater.refresh_rail_chain_assignments(payload, {"UNIT-001", "UNIT-002"})
        self.assertFalse(any(unit["journey_chain_needs_confirmation"] for unit in units))
        self.assertEqual({"confirmed"}, {unit["status"] for unit in units})
        self.assertEqual(
            {"rail_transfer_chain_user_confirmed"},
            {unit["journey_chain_assignment_rule"] for unit in units},
        )

    def test_dropping_one_of_two_legs_clears_obsolete_chain_metadata(self) -> None:
        units = [
            rail_unit(1, "上海虹桥", "周口东", "08:00"),
            rail_unit(2, "周口东", "郑州东", "10:00"),
        ]
        allocator.apply_matches(units, [self.zhengzhou])
        units[1]["status"] = "dropped"
        updater.refresh_rail_chain_assignments({"allocation_units": units}, {"UNIT-002"})
        self.assertEqual({""}, {unit.get("journey_chain_id", "") for unit in units})


if __name__ == "__main__":
    unittest.main()
