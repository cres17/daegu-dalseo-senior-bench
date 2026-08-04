#!/usr/bin/env python3
"""현장조사 통합 검증용 재현 노트북을 생성한다."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "05_evaluation" / "notebooks" / "02_field_review_integration.ipynb"
OUTPUT.parent.mkdir(parents=True, exist_ok=True)


def markdown(source: str) -> dict[str, object]:
    return {
        "cell_type": "markdown",
        "metadata": {},
        "source": source.splitlines(keepends=True),
    }


def code(source: str) -> dict[str, object]:
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": source.splitlines(keepends=True),
    }


cells = [
    markdown(
        """# 현장조사 결과 통합 검증

이 노트북은 29개 고유 후보의 현장조사 CSV를 검증하고, 현장판정을 게이트로
적용한 2차 우선 확인 순서를 재현한다. 결과는 최종 설치 위치가 아니다.
"""
    ),
    code(
        """from pathlib import Path
import json
import subprocess
import sys
import pandas as pd

ROOT = Path.cwd()
if ROOT.name == "notebooks":
    ROOT = ROOT.parents[1]
SCRIPT = ROOT / "05_evaluation" / "scripts" / "integrate_field_review_results.py"
result = subprocess.run([sys.executable, str(SCRIPT)], cwd=ROOT, check=True, capture_output=True, text=True)
print(result.stdout)
"""
    ),
    markdown("## 입력 완결성과 현장판정 분포"),
    code(
        """OUT = ROOT / "05_evaluation" / "outputs"
QA = ROOT / "05_evaluation" / "qa"
ranked = pd.read_csv(OUT / "field_adjusted_priority_candidates.csv", encoding="utf-8-sig")
summary = json.loads((QA / "field_review_integration_summary.json").read_text(encoding="utf-8"))
assert summary["row_count"] == 29
assert summary["unique_candidate_ids"] == 29
assert summary["critical_structure_failures"] == 0
pd.DataFrame({
    "review_status": ranked["review_status"].value_counts(),
    "share": ranked["review_status"].value_counts(normalize=True),
})
"""
    ),
    markdown("## 데이터 품질 점검"),
    code(
        """findings = pd.read_csv(QA / "field_review_quality_findings.csv", encoding="utf-8-sig")
findings[["finding_id", "severity", "check", "affected_rows", "denominator", "evidence"]]
"""
    ),
    markdown("## 현장판정 반영 2차 우선 확인 순서"),
    code(
        """columns = [
    "field_adjusted_review_rank", "field_review_unique_id", "name",
    "candidate_thresholds_m", "review_status", "decision_band",
    "model_selection_strength", "route_count_800m", "nearest_seat_max_m",
    "field_evidence_completion_rate", "blocking_reasons",
]
actionable = ranked[ranked["field_adjusted_review_rank"].notna()][columns]
assert len(actionable) == 10
actionable
"""
    ),
    markdown(
        """## 해석 제한

- `PRELIMINARY_PRIORITY`는 세부 현장근거가 미완료된 설치 가능 판정이다.
- `CONDITIONAL_REVIEW`는 위치 조정·안전 개선·소유권 협의를 전제로 한다.
- 기존 좌석이 확인된 후보는 좌석 상태 재확인 전까지 제외한다.
- 실제 고령자 GPS 경로가 아니라 예상 보행 수요를 사용한다.
"""
    ),
]

notebook = {
    "cells": cells,
    "metadata": {
        "kernelspec": {
            "display_name": "Python 3",
            "language": "python",
            "name": "python3",
        },
        "language_info": {"name": "python", "version": "3.12"},
    },
    "nbformat": 4,
    "nbformat_minor": 5,
}
OUTPUT.write_text(
    json.dumps(notebook, ensure_ascii=False, indent=1),
    encoding="utf-8",
)
print(OUTPUT)
