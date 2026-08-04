#!/usr/bin/env python3
"""nbformat/nbclient 없이 Modeling 노트북을 위에서 아래로 실행한다."""

from __future__ import annotations

import contextlib
import io
import json
import traceback
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
NOTEBOOK = ROOT / "04_modeling" / "notebooks" / "01_expected_walk_bench_gap_modeling.ipynb"


def main() -> None:
    notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    namespace: dict[str, object] = {"__name__": "__main__"}
    execution_count = 0
    for cell in notebook["cells"]:
        if cell["cell_type"] != "code":
            continue
        execution_count += 1
        cell["execution_count"] = execution_count
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                exec("".join(cell["source"]), namespace)
            text = output.getvalue()
            cell["outputs"] = (
                [{"name": "stdout", "output_type": "stream", "text": text.splitlines(keepends=True)}]
                if text
                else []
            )
        except Exception as exc:
            text = output.getvalue()
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
