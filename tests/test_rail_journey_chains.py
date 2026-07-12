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
) -> dict:
    route = f"{origin}-{destination}"
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
        "source_note": f"G{100 + number}, {origin} -> {destination}, {travel_date} {departure_time}, 二等座",
        "expense_note": f"G{100 + number}, {origin} -> {destination}, {travel_date} {departure_time}, 二等座",
        "final_note": f"高铁（{route}）",
        "final_template_column": "travel",
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


class RailJourneyChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.zhoukou = project("CTX-ZK", "周口", "周口项目", "CORP-ZK")
        self.zhengzhou = project("CTX-ZZ", "郑州", "千味央厨", "CORP-QW")
        self.taiyuan = project("CTX-TY", "太原", "山西信托", "CORP-SX")

    def test_outbound_transfer_uses_terminal_project_not_intermediate_city(self) -> None:
        units = [
            rail_unit(1, "上海虹桥", "周口东", "08:00"),
            rail_unit(2, "周口", "郑州东", "13:00"),
        ]
        allocator.apply_matches(units, [self.zhoukou, self.zhengzhou])

        self.assertEqual({"RAIL-CHAIN-001"}, {unit["journey_chain_id"] for unit in units})
        self.assertEqual({"CTX-ZZ"}, {unit["project_context_id"] for unit in units})
        self.assertEqual({"千味央厨"}, {unit["client_name"] for unit in units})
        self.assertEqual({"rail_transfer_chain_destination"}, {unit["auto_project_match"] for unit in units})
        self.assertEqual("上海虹桥 -> 周口东 -> 郑州东", units[0]["journey_chain_route"])
        self.assertEqual("高铁（上海虹桥-周口东）", units[0]["final_note"])
        self.assertEqual("高铁（周口-郑州东）", units[1]["final_note"])

    def test_return_transfer_uses_project_just_completed(self) -> None:
        units = [
            rail_unit(1, "郑州东", "周口东", "08:00"),
            rail_unit(2, "周口东", "上海虹桥", "13:00"),
        ]
        allocator.apply_matches(units, [self.zhengzhou])

        self.assertEqual({"CTX-ZZ"}, {unit["project_context_id"] for unit in units})
        self.assertEqual({"rail_transfer_chain_return"}, {unit["auto_project_match"] for unit in units})

    def test_project_to_project_transfer_uses_project_being_traveled_to(self) -> None:
        units = [
            rail_unit(1, "太原南", "周口东", "07:00"),
            rail_unit(2, "周口东", "郑州东", "13:00"),
        ]
        allocator.apply_matches(units, [self.taiyuan, self.zhengzhou])

        self.assertEqual({"CTX-ZZ"}, {unit["project_context_id"] for unit in units})

    def test_three_connected_segments_share_one_chain_and_project(self) -> None:
        units = [
            rail_unit(1, "上海虹桥", "南京南", "06:00"),
            rail_unit(2, "南京南", "周口东", "09:00"),
            rail_unit(3, "周口东", "郑州东", "15:00"),
        ]
        allocator.apply_matches(units, [self.zhoukou, self.zhengzhou])
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
            rail_unit(2, "周口东", "郑州东", "13:00"),
            rail_unit(3, "周口东", "漯河西", "14:00"),
        ]
        self.assertEqual([], allocator.build_rail_journey_chains(units))

    def test_missing_times_can_still_form_same_day_medium_confidence_chain(self) -> None:
        units = [
            rail_unit(1, "上海虹桥", "周口东", ""),
            rail_unit(2, "周口东", "郑州东", ""),
        ]
        chains = allocator.build_rail_journey_chains(units)
        self.assertEqual(1, len(chains))
        self.assertEqual("medium", chains[0]["confidence"])

    def test_old_extraction_note_without_structured_leg_fields_still_forms_chain(self) -> None:
        units = [
            rail_unit(1, "上海虹桥", "周口东", "08:00"),
            rail_unit(2, "周口东", "郑州东", "13:00"),
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
            rail_unit(2, "周口东", "郑州东", "13:00"),
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
            rail_unit(2, "周口东", "漯河西", "13:00"),
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
            rail_unit(2, "周口东", "郑州东", "13:00"),
        ]
        contexts = [self.zhoukou, self.zhengzhou]
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

    def test_stage3_rejects_stale_chain_after_route_correction_breaks_connection(self) -> None:
        units = [
            rail_unit(1, "上海虹桥", "周口东", "08:00"),
            rail_unit(2, "周口东", "郑州东", "13:00"),
        ]
        allocator.apply_matches(units, [self.zhengzhou])
        units[1]["route"] = "开封北-郑州东"
        errors = writer.rail_chain_ready_errors(units)
        self.assertTrue(any("no longer continuous" in error for error in errors))

    def test_stage3_rejects_truncated_three_leg_chain_after_drop(self) -> None:
        units = [
            rail_unit(1, "上海虹桥", "南京南", "06:00"),
            rail_unit(2, "南京南", "周口东", "09:00"),
            rail_unit(3, "周口东", "郑州东", "15:00"),
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
            rail_unit(2, "周口东", "漯河西", "13:00"),
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
            rail_unit(2, "周口东", "郑州东", "13:00"),
        ]
        allocator.apply_matches(units, [self.zhengzhou])
        units[1]["status"] = "dropped"
        updater.refresh_rail_chain_assignments({"allocation_units": units}, {"UNIT-002"})
        self.assertEqual({""}, {unit.get("journey_chain_id", "") for unit in units})


if __name__ == "__main__":
    unittest.main()
