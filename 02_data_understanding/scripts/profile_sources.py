#!/usr/bin/env python3
"""Read-only profiler for the Dalseo-gu bench project source files.

The script never modifies source files. It writes compact audit outputs under
02_data_understanding/profile_outputs.
"""

from __future__ import annotations

import csv
import codecs
import hashlib
import json
import math
import re
import struct
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = ROOT / "02_data_understanding" / "profile_outputs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

ENCODINGS = ("utf-8-sig", "cp949", "euc-kr", "utf-8")
NULL_TEXT = {"", "nan", "none", "null", "n/a", "na", "-"}


def normalized_text(value: object) -> str:
    return unicodedata.normalize("NFC", str(value)).strip()


def relative(path: Path) -> str:
    return normalized_text(path.relative_to(ROOT))


def classify(path: Path) -> str:
    name = relative(path)
    if "BND_ADM_DONG_PG" in name or "행정동경계" in name:
        return "행정동 경계"
    if "횡단보도" in name:
        return "횡단보도 공간정보"
    if "공공보행통로" in name:
        return "공공보행통로 공간정보"
    if "보행자우선도로" in name:
        return "보행자우선도로 공간정보"
    if "C_UQ164_RO" in name:
        return "보행자전용도로 공간정보"
    if "노인보호구역" in name:
        return "달서구 노인보호구역"
    if "달서구" in name and "보행자전용도로" in name:
        return "달서구 보행자전용도로"
    if "달서구" in name and "육교정보" in name:
        return "달서구 육교"
    if "시간대별" in name:
        return "버스 정류소별 시간대별 승하차"
    if "월별 이용자수" in name:
        return "버스 정류소별 월별 이용자수"
    if "버스 노선 공간정보" in name:
        return "버스 노선 공간정보"
    if "정류소 위치정보" in name:
        return "버스 정류소 위치정보"
    if "공원시설물정보" in name:
        return "공원시설물"
    if "달서구" in name and "경로당" in name:
        return "달서구 경로당"
    if "달서구" in name and "노인복지시설" in name:
        return "달서구 노인복지시설"
    if "달서구" in name and "노인인구" in name:
        return "달서구 노인인구"
    if "달서구" in name and "노인일자리" in name:
        return "달서구 노인일자리"
    if "달서구" in name and "벤치" in name:
        return "달서구 벤치"
    if "달서구" in name and "의료기관" in name:
        return "달서구 의료기관"
    if "서구" in name and "벤치" in name:
        return "서구 벤치(1차 제외)"
    return "기타"


def detect_encoding_and_delimiter(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()[:200_000]
    chosen = None
    text = None
    for encoding in ENCODINGS:
        try:
            # final=False prevents a multibyte Korean character cut at the
            # sample boundary from being mistaken for a file encoding error.
            text = codecs.getincrementaldecoder(encoding)().decode(raw, final=False)
            chosen = encoding
            break
        except UnicodeDecodeError:
            continue
    if chosen is None:
        chosen = "utf-8"
        text = raw.decode(chosen, errors="replace")
    try:
        delimiter = csv.Sniffer().sniff(text[:20_000], delimiters=",\t;|").delimiter
    except csv.Error:
        delimiter = ","
    return chosen, delimiter


def is_blank(series: pd.Series) -> pd.Series:
    normalized = series.fillna("").astype(str).str.strip().str.lower()
    return normalized.isin(NULL_TEXT)


def numeric_profile(series: pd.Series) -> dict[str, object]:
    cleaned = series.fillna("").astype(str).str.replace(",", "", regex=False).str.strip()
    nonblank = ~cleaned.str.lower().isin(NULL_TEXT)
    numeric = pd.to_numeric(cleaned.where(nonblank), errors="coerce")
    denominator = int(nonblank.sum())
    ratio = float(numeric.notna().sum() / denominator) if denominator else 0.0
    result: dict[str, object] = {"numeric_parse_rate": round(ratio, 6)}
    if ratio >= 0.8 and numeric.notna().any():
        result.update(
            {
                "numeric_min": float(numeric.min()),
                "numeric_max": float(numeric.max()),
                "numeric_median": float(numeric.median()),
            }
        )
    return result


def candidate_role(column: str) -> str:
    key = normalized_text(column).lower().replace(" ", "")
    roles = []
    if any(token in key for token in ("위도", "latitude", "lat")):
        roles.append("latitude")
    if any(token in key for token in ("경도", "longitude", "lon", "lng")):
        roles.append("longitude")
    if re.fullmatch(r"x|x좌표|좌표x", key):
        roles.append("x")
    if re.fullmatch(r"y|y좌표|좌표y", key):
        roles.append("y")
    if "주소" in key or "소재지" in key:
        roles.append("address")
    if any(token in key for token in ("id", "코드", "번호", "정류소번호", "정류소id")):
        roles.append("identifier")
    if any(token in key for token in ("일자", "날짜", "년월", "기준일", "데이터기준일")):
        roles.append("date")
    return ",".join(sorted(set(roles)))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def profile_csv(path: Path) -> tuple[dict[str, object], list[dict[str, object]]]:
    encoding, delimiter = detect_encoding_and_delimiter(path)
    read_error = ""
    try:
        frame = pd.read_csv(
            path,
            encoding=encoding,
            sep=delimiter,
            dtype=str,
            keep_default_na=False,
            na_filter=False,
            low_memory=False,
        )
    except Exception as exc:
        read_error = f"{type(exc).__name__}: {exc}"
        frame = pd.DataFrame()

    frame.columns = [normalized_text(col) for col in frame.columns]
    row_count = int(len(frame))
    column_count = int(len(frame.columns))
    duplicate_rows = int(frame.duplicated().sum()) if column_count else 0
    schema_signature = "|".join(frame.columns)

    file_record: dict[str, object] = {
        "path": relative(path),
        "family": classify(path),
        "extension": ".csv",
        "bytes": path.stat().st_size,
        "sha256": sha256(path),
        "encoding": encoding,
        "delimiter": "TAB" if delimiter == "\t" else delimiter,
        "rows": row_count,
        "columns": column_count,
        "exact_duplicate_rows": duplicate_rows,
        "exact_duplicate_rate": round(duplicate_rows / row_count, 6) if row_count else 0.0,
        "schema_signature": schema_signature,
        "read_error": read_error,
    }

    columns: list[dict[str, object]] = []
    for column in frame.columns:
        series = frame[column]
        blank = is_blank(series)
        nonblank = series[~blank].astype(str).str.strip()
        unique = int(nonblank.nunique(dropna=True))
        duplicates = int(len(nonblank) - unique)
        role = candidate_role(column)
        samples = [normalized_text(value) for value in nonblank.drop_duplicates().head(5).tolist()]
        record: dict[str, object] = {
            "path": relative(path),
            "family": classify(path),
            "column": column,
            "role": role,
            "rows": row_count,
            "blank_count": int(blank.sum()),
            "blank_rate": round(float(blank.mean()), 6) if row_count else 0.0,
            "nonblank_unique": unique,
            "nonblank_unique_rate": round(unique / len(nonblank), 6) if len(nonblank) else 0.0,
            "nonblank_duplicate_count": duplicates,
            "samples": " | ".join(samples),
        }
        record.update(numeric_profile(series))
        columns.append(record)

    return file_record, columns


def read_dbf_header(path: Path) -> dict[str, object]:
    with path.open("rb") as handle:
        header = handle.read(32)
        if len(header) < 32:
            raise ValueError("DBF header is shorter than 32 bytes")
        version = header[0]
        year, month, day = header[1] + 1900, header[2], header[3]
        record_count = struct.unpack("<I", header[4:8])[0]
        header_length = struct.unpack("<H", header[8:10])[0]
        record_length = struct.unpack("<H", header[10:12])[0]
        fields = []
        while True:
            descriptor = handle.read(32)
            if not descriptor or descriptor[0] == 0x0D:
                break
            name = descriptor[:11].split(b"\x00", 1)[0].decode("ascii", errors="replace")
            field_type = chr(descriptor[11])
            length = descriptor[16]
            decimal_count = descriptor[17]
            fields.append(
                {"name": name, "type": field_type, "length": length, "decimals": decimal_count}
            )
    return {
        "version": version,
        "last_update": f"{year:04d}-{month:02d}-{day:02d}",
        "record_count": record_count,
        "header_length": header_length,
        "record_length": record_length,
        "fields": fields,
    }


def read_shp_header(path: Path) -> dict[str, object]:
    with path.open("rb") as handle:
        header = handle.read(100)
        if len(header) < 100:
            raise ValueError("SHP header is shorter than 100 bytes")
        file_code = struct.unpack(">i", header[0:4])[0]
        file_length_bytes = struct.unpack(">i", header[24:28])[0] * 2
        version = struct.unpack("<i", header[28:32])[0]
        shape_type = struct.unpack("<i", header[32:36])[0]
        xmin, ymin, xmax, ymax = struct.unpack("<4d", header[36:68])
        records = 0
        while True:
            rec_header = handle.read(8)
            if not rec_header:
                break
            if len(rec_header) < 8:
                raise ValueError("Truncated SHP record header")
            content_words = struct.unpack(">i", rec_header[4:8])[0]
            handle.seek(content_words * 2, 1)
            records += 1
    return {
        "file_code": file_code,
        "declared_length_bytes": file_length_bytes,
        "actual_length_bytes": path.stat().st_size,
        "version": version,
        "shape_type": shape_type,
        "record_count": records,
        "xmin": xmin,
        "ymin": ymin,
        "xmax": xmax,
        "ymax": ymax,
    }


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    frame = pd.DataFrame(rows)
    frame.to_csv(path, index=False, encoding="utf-8-sig")


def main() -> None:
    csv_paths = sorted(ROOT.rglob("*.csv"))
    source_paths = [p for p in csv_paths if "02_data_understanding" not in p.parts]
    file_records: list[dict[str, object]] = []
    column_records: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []

    for path in source_paths:
        try:
            file_record, column_profile = profile_csv(path)
            file_records.append(file_record)
            column_records.extend(column_profile)
        except Exception as exc:
            errors.append({"path": relative(path), "error": f"{type(exc).__name__}: {exc}"})

    spatial_records: list[dict[str, object]] = []
    for path in sorted(ROOT.rglob("*.shp")):
        if "02_data_understanding" in path.parts:
            continue
        try:
            result = read_shp_header(path)
            result.update({"path": relative(path), "family": classify(path), "extension": ".shp"})
            spatial_records.append(result)
        except Exception as exc:
            errors.append({"path": relative(path), "error": f"{type(exc).__name__}: {exc}"})
    for path in sorted(ROOT.rglob("*.dbf")):
        if "02_data_understanding" in path.parts:
            continue
        try:
            result = read_dbf_header(path)
            result.update({"path": relative(path), "family": classify(path), "extension": ".dbf"})
            spatial_records.append(result)
        except Exception as exc:
            errors.append({"path": relative(path), "error": f"{type(exc).__name__}: {exc}"})

    family_rows: list[dict[str, object]] = []
    by_family: dict[str, list[dict[str, object]]] = defaultdict(list)
    for record in file_records:
        by_family[str(record["family"])].append(record)
    for family, records in sorted(by_family.items()):
        family_rows.append(
            {
                "family": family,
                "file_count": len(records),
                "total_rows": sum(int(r["rows"]) for r in records),
                "total_bytes": sum(int(r["bytes"]) for r in records),
                "schema_count": len({str(r["schema_signature"]) for r in records}),
                "encoding_count": len({str(r["encoding"]) for r in records}),
                "read_error_count": sum(bool(r["read_error"]) for r in records),
                "exact_duplicate_rows": sum(int(r["exact_duplicate_rows"]) for r in records),
            }
        )

    payload = {
        "root": str(ROOT),
        "csv_file_count": len(file_records),
        "spatial_component_count": len(spatial_records),
        "files": file_records,
        "columns": column_records,
        "spatial": spatial_records,
        "families": family_rows,
        "errors": errors,
    }
    (OUTPUT_DIR / "source_profile.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    write_csv(OUTPUT_DIR / "file_inventory.csv", file_records)
    write_csv(OUTPUT_DIR / "column_profile.csv", column_records)
    write_csv(OUTPUT_DIR / "family_summary.csv", family_rows)
    write_csv(OUTPUT_DIR / "spatial_profile.csv", spatial_records)
    write_csv(OUTPUT_DIR / "profile_errors.csv", errors)

    print(json.dumps({"files": len(file_records), "columns": len(column_records), "spatial": len(spatial_records), "errors": len(errors)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
