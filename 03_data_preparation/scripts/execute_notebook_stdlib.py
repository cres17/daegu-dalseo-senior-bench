#!/usr/bin/env python3
"""nbformat/nbclient가 없는 환경에서 프로젝트 검증 노트북을 순차 실행한다."""

from __future__ import annotations

import contextlib
import io
import json
import traceback
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
NOTEBOOK = ROOT / "03_data_preparation" / "notebooks" / "01_data_preparation_validation.ipynb"


def main() -> None:
    notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    namespace: dict[str, object] = {"__name__": "__main__"}
    execution_count = 0
    for cell in notebook["cells"]:
        if cell["cell_type"] != "code":
            continue
        execution_count += 1
        cell["execution_count"] = execution_count
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                exec("".join(cell["source"]), namespace)
            text = buffer.getvalue()
            cell["outputs"] = (
                [{"name": "stdout", "output_type": "stream", "text": text.splitlines(keepends=True)}]
                if text
                else []
            )
        except Exception as exc:
            text = buffer.getvalue()
            trace = traceback.format_exc()
            outputs = []
            if text:
                outputs.append(
                    {"name": "stdout", "output_type": "stream", "text": text.splitlines(keepends=True)}
                )
            outputs.append(
                {
                    "output_type": "error",
                    "ename": type(exc).__name__,
                    "evalue": str(exc),
                    "traceback": trace.splitlines(),
                }
            )
            cell["outputs"] = outputs
            NOTEBOOK.write_text(json.dumps(notebook, ensure_ascii=False, indent=1), encoding="utf-8")
            raise
    NOTEBOOK.write_text(json.dumps(notebook, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Executed {execution_count} code cells: {NOTEBOOK}")


if __name__ == "__main__":
    main()
