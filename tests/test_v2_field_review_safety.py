from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "05_evaluation"
    / "scripts"
    / "integrate_v2_field_review_results.py"
)
SPEC = importlib.util.spec_from_file_location("integrate_v2", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def complete_installable(**overrides: str) -> dict[str, str]:
    row = {
        "review_status": "INSTALLABLE",
        "existing_seat_verified": "NO",
        "continuous_sidewalk": "YES",
        "effective_width_m": "2.0",
        "width_measurement_method": "줄자 실측",
        "width_evidence_ref": "PHOTO-001",
        "slope_condition": "FLAT",
        "crossing_risk": "LOW",
        "ownership_installability": "PUBLIC_LIKELY",
        "reviewer": "reviewer-1",
        "review_date": "2026-08-24",
    }
    row.update(overrides)
    return row


class InstallableSafetyGateTests(unittest.TestCase):
    def test_complete_low_risk_candidate_passes(self) -> None:
        result = MODULE.evaluate_installable_safety(complete_installable())

        self.assertEqual(result["safety_gate_status"], "PASS")
        self.assertFalse(result["safety_gate_overrode_status"])
        self.assertEqual(result["effective_width_m_numeric"], 2.0)
        self.assertEqual(result["width_compliance_status"], "STANDARD_PASS")

    def test_substandard_width_requires_documented_exception(self) -> None:
        result = MODULE.evaluate_installable_safety(
            complete_installable(effective_width_m="1.8")
        )

        self.assertEqual(result["safety_gate_status"], "BLOCKED_INCOMPLETE")
        self.assertIn(
            "width_standard_exception", result["safety_gate_missing_fields"]
        )

    def test_documented_width_exception_can_pass(self) -> None:
        result = MODULE.evaluate_installable_safety(
            complete_installable(
                effective_width_m="1.8",
                width_standard_exception="APPROVED",
                width_exception_authority="달서구 도로관리청",
                width_exception_basis="EXISTING_ROAD_UNAVOIDABLE",
                width_exception_reason="기존 도로 개축 시 불가피",
            )
        )

        self.assertEqual(result["safety_gate_status"], "PASS")
        self.assertEqual(result["width_compliance_status"], "EXCEPTION_PASS")
        self.assertTrue(result["width_exception_path_used"])

    def test_unrecognized_exception_basis_is_blocked(self) -> None:
        result = MODULE.evaluate_installable_safety(
            complete_installable(
                effective_width_m="1.8",
                width_standard_exception="APPROVED",
                width_exception_authority="달서구 도로관리청",
                width_exception_basis="OTHER",
                width_exception_reason="임의 사유",
            )
        )

        self.assertEqual(result["safety_gate_status"], "BLOCKED_CONFLICT")
        self.assertIn(
            "WIDTH_EXCEPTION_BASIS_NOT_ALLOWED:OTHER",
            result["safety_gate_conflicts"],
        )

    def test_width_below_1_5_requires_passing_zone(self) -> None:
        result = MODULE.evaluate_installable_safety(
            complete_installable(
                effective_width_m="1.3",
                width_standard_exception="APPROVED",
                width_exception_authority="달서구 도로관리청",
                width_exception_basis="TOPOGRAPHY_IMPOSSIBLE",
                width_exception_reason="지형상 불가능",
            )
        )

        self.assertEqual(result["safety_gate_status"], "BLOCKED_INCOMPLETE")
        self.assertIn("passing_zone_compliance", result["safety_gate_missing_fields"])

    def test_width_below_1_5_with_passing_zone_can_pass(self) -> None:
        result = MODULE.evaluate_installable_safety(
            complete_installable(
                effective_width_m="1.3",
                width_standard_exception="APPROVED",
                width_exception_authority="달서구 도로관리청",
                width_exception_basis="TOPOGRAPHY_IMPOSSIBLE",
                width_exception_reason="지형상 불가능",
                passing_zone_compliance="YES",
            )
        )

        self.assertEqual(result["safety_gate_status"], "PASS")
        self.assertEqual(result["width_compliance_status"], "EXCEPTION_PASS")

    def test_sloped_narrow_exception_requires_rest_landing(self) -> None:
        result = MODULE.evaluate_installable_safety(
            complete_installable(
                effective_width_m="1.3",
                slope_condition="MODERATE",
                width_standard_exception="APPROVED",
                width_exception_authority="달서구 도로관리청",
                width_exception_basis="TOPOGRAPHY_IMPOSSIBLE",
                width_exception_reason="지형상 불가능",
                passing_zone_compliance="YES",
            )
        )

        self.assertEqual(result["safety_gate_status"], "BLOCKED_INCOMPLETE")
        self.assertIn(
            "sloped_rest_landing_compliance",
            result["safety_gate_missing_fields"],
        )

    def test_width_below_exception_minimum_is_conflict(self) -> None:
        result = MODULE.evaluate_installable_safety(
            complete_installable(effective_width_m="1.19")
        )

        self.assertEqual(result["safety_gate_status"], "BLOCKED_CONFLICT")
        self.assertIn(
            "EFFECTIVE_WIDTH_BELOW_ABSOLUTE_MIN:1.19",
            result["safety_gate_conflicts"],
        )

    def test_high_crossing_risk_is_blocked_as_conflict(self) -> None:
        result = MODULE.evaluate_installable_safety(
            complete_installable(crossing_risk="HIGH")
        )

        self.assertEqual(result["safety_gate_status"], "BLOCKED_CONFLICT")
        self.assertIn("CROSSING_RISK_NOT_LOW:HIGH", result["safety_gate_conflicts"])

    def test_medium_crossing_risk_does_not_satisfy_low_risk_rule(self) -> None:
        result = MODULE.evaluate_installable_safety(
            complete_installable(crossing_risk="MEDIUM")
        )

        self.assertEqual(result["safety_gate_status"], "BLOCKED_CONFLICT")
        self.assertIn(
            "CROSSING_RISK_NOT_LOW:MEDIUM", result["safety_gate_conflicts"]
        )

    def test_partial_sidewalk_and_steep_slope_are_blocked(self) -> None:
        result = MODULE.evaluate_installable_safety(
            complete_installable(
                continuous_sidewalk="PARTIAL",
                slope_condition="STEEP",
            )
        )

        self.assertEqual(result["safety_gate_status"], "BLOCKED_CONFLICT")
        self.assertIn("SIDEWALK_NOT_CONTINUOUS:PARTIAL", result["safety_gate_conflicts"])
        self.assertIn("SLOPE_NOT_ACCEPTABLE:STEEP", result["safety_gate_conflicts"])

    def test_missing_width_and_provenance_are_blocked_as_incomplete(self) -> None:
        result = MODULE.evaluate_installable_safety(
            complete_installable(effective_width_m="", reviewer="", review_date="")
        )

        self.assertEqual(result["safety_gate_status"], "BLOCKED_INCOMPLETE")
        self.assertIn("effective_width_m", result["safety_gate_missing_fields"])
        self.assertIn("reviewer", result["safety_gate_missing_fields"])
        self.assertIn("review_date", result["safety_gate_missing_fields"])

    def test_non_installable_status_is_not_reinterpreted(self) -> None:
        result = MODULE.evaluate_installable_safety(
            {"review_status": "CONDITIONAL"}
        )

        self.assertEqual(result["safety_gate_status"], "NOT_APPLICABLE")
        self.assertFalse(result["safety_gate_overrode_status"])

    def test_public_payload_never_presents_blocked_rows_as_direct(self) -> None:
        payload_path = Path(__file__).resolve().parents[1] / "docs" / "candidates.json"
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        summary = payload["summary"]

        self.assertEqual(
            summary["total"],
            summary["direct"]
            + summary["safetyBlocked"]
            + summary["conditional"]
            + summary["excluded"],
        )
        if payload["mode"] == "DIRECT_REVIEW":
            self.assertTrue(
                all(row["gateStatus"] == "PASS" for row in payload["candidates"])
            )
        else:
            self.assertEqual(summary["direct"], 0)
            self.assertTrue(
                all(row["gateStatus"] != "PASS" for row in payload["candidates"])
            )


if __name__ == "__main__":
    unittest.main()
