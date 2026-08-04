#!/usr/bin/env python3
"""Project-specific Data Understanding checks for the Dalseo bench project."""

from __future__ import annotations

import importlib.util
import json
import math
import re
import struct
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "02_data_understanding" / "profile_outputs"
OUT.mkdir(parents=True, exist_ok=True)

spec = importlib.util.spec_from_file_location("profiler", Path(__file__).with_name("profile_sources.py"))
profiler = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(profiler)


def nfc(value: object) -> str:
    return unicodedata.normalize("NFC", str(value)).strip()


def norm_name(value: object) -> str:
    text = nfc(value).lower()
    return re.sub(r"[\s\-_.·ㆍ,()\[\]/]+", "", text)


def find_one(*tokens: str, suffix: str = ".csv") -> Path:
    matches = []
    for path in ROOT.rglob(f"*{suffix}"):
        if "02_data_understanding" in path.parts:
            continue
        label = nfc(path.name)
        if all(token in label for token in tokens):
            matches.append(path)
    if len(matches) != 1:
        raise RuntimeError(f"Expected one match for {tokens}, found {len(matches)}: {matches}")
    return matches[0]


def read_csv(path: Path) -> pd.DataFrame:
    encoding, delimiter = profiler.detect_encoding_and_delimiter(path)
    options = dict(
        sep=delimiter,
        dtype=str,
        keep_default_na=False,
        na_filter=False,
        low_memory=False,
    )
    try:
        frame = pd.read_csv(path, encoding=encoding, **options)
    except UnicodeDecodeError:
        frame = pd.read_csv(path, encoding="cp949", encoding_errors="replace", **options)
    frame.columns = [nfc(col) for col in frame.columns]
    return frame.apply(lambda col: col.map(lambda x: nfc(x) if isinstance(x, str) else x))


def parse_dbf(path: Path, encoding: str = "utf-8") -> pd.DataFrame:
    with path.open("rb") as handle:
        header = handle.read(32)
        record_count = struct.unpack("<I", header[4:8])[0]
        header_length = struct.unpack("<H", header[8:10])[0]
        record_length = struct.unpack("<H", header[10:12])[0]
        fields = []
        while True:
            descriptor = handle.read(32)
            if not descriptor or descriptor[0] == 0x0D:
                break
            fields.append(
                {
                    "name": descriptor[:11].split(b"\x00", 1)[0].decode("ascii", errors="replace"),
                    "type": chr(descriptor[11]),
                    "length": descriptor[16],
                    "decimals": descriptor[17],
                }
            )
        handle.seek(header_length)
        rows = []
        for _ in range(record_count):
            raw = handle.read(record_length)
            if len(raw) < record_length:
                break
            if raw[:1] == b"*":
                continue
            position = 1
            row = {}
            for field in fields:
                chunk = raw[position : position + field["length"]]
                position += field["length"]
                text = chunk.decode(encoding, errors="replace").strip()
                row[field["name"]] = text
            rows.append(row)
    return pd.DataFrame(rows)


def meridional_arc(phi: np.ndarray | float, a: float, e2: float) -> np.ndarray | float:
    return a * (
        (1 - e2 / 4 - 3 * e2**2 / 64 - 5 * e2**3 / 256) * phi
        - (3 * e2 / 8 + 3 * e2**2 / 32 + 45 * e2**3 / 1024) * np.sin(2 * phi)
        + (15 * e2**2 / 256 + 45 * e2**3 / 1024) * np.sin(4 * phi)
        - (35 * e2**3 / 3072) * np.sin(6 * phi)
    )


def inverse_korea_2000_east_belt(x: pd.Series, y: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    """Inverse of the PRJ parameters in the supplied bus shapefiles."""
    a = 6378137.0
    f = 1 / 298.257222101
    e2 = f * (2 - f)
    ep2 = e2 / (1 - e2)
    lat0 = math.radians(38.0)
    lon0 = math.radians(129.0)
    false_easting = 200000.0
    false_northing = 600000.0
    k0 = 1.0

    xx = pd.to_numeric(x, errors="coerce").to_numpy(dtype=float) - false_easting
    yy = pd.to_numeric(y, errors="coerce").to_numpy(dtype=float) - false_northing
    m0 = float(meridional_arc(lat0, a, e2))
    m = m0 + yy / k0
    mu = m / (a * (1 - e2 / 4 - 3 * e2**2 / 64 - 5 * e2**3 / 256))
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    j1 = 3 * e1 / 2 - 27 * e1**3 / 32
    j2 = 21 * e1**2 / 16 - 55 * e1**4 / 32
    j3 = 151 * e1**3 / 96
    j4 = 1097 * e1**4 / 512
    fp = mu + j1 * np.sin(2 * mu) + j2 * np.sin(4 * mu) + j3 * np.sin(6 * mu) + j4 * np.sin(8 * mu)
    c1 = ep2 * np.cos(fp) ** 2
    t1 = np.tan(fp) ** 2
    n1 = a / np.sqrt(1 - e2 * np.sin(fp) ** 2)
    r1 = a * (1 - e2) / (1 - e2 * np.sin(fp) ** 2) ** 1.5
    d = xx / (n1 * k0)
    lat = fp - (n1 * np.tan(fp) / r1) * (
        d**2 / 2
        - (5 + 3 * t1 + 10 * c1 - 4 * c1**2 - 9 * ep2) * d**4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1**2 - 252 * ep2 - 3 * c1**2) * d**6 / 720
    )
    lon = lon0 + (
        d
        - (1 + 2 * t1 + c1) * d**3 / 6
        + (5 - 2 * c1 + 28 * t1 - 3 * c1**2 + 8 * ep2 + 24 * t1**2) * d**5 / 120
    ) / np.cos(fp)
    return np.degrees(lon), np.degrees(lat)


def haversine_m(lon1: float, lat1: float, lon2: np.ndarray, lat2: np.ndarray) -> np.ndarray:
    radius = 6371008.8
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = p2 - p1
    dlambda = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlambda / 2) ** 2
    return 2 * radius * np.arctan2(np.sqrt(a), np.sqrt(1 - a))


def pct(number: int | float, denominator: int | float) -> float:
    return round(float(number) / float(denominator), 6) if denominator else 0.0


def add_metric(rows: list[dict[str, object]], dataset: str, metric: str, value: object, note: str = "") -> None:
    rows.append({"dataset": dataset, "metric": metric, "value": value, "note": note})


def main() -> None:
    metrics: list[dict[str, object]] = []
    joins: list[dict[str, object]] = []
    findings: list[dict[str, object]] = []

    paths = {
        "population": find_one("노인인구"),
        "senior_centers": find_one("경로당"),
        "welfare": find_one("노인복지시설"),
        "jobs": find_one("노인일자리"),
        "benches": find_one("달서구", "벤치"),
        "medical": find_one("의료기관"),
        "parks": find_one("공원시설물정보"),
        "stops": find_one("정류소 위치정보"),
    }
    data = {name: read_csv(path) for name, path in paths.items()}

    population = data["population"]
    population_numbers = population[["계", "남", "여"]].apply(pd.to_numeric, errors="coerce")
    add_metric(metrics, "달서구 노인인구", "rows", len(population))
    add_metric(metrics, "달서구 노인인구", "administrative_dongs", population["동명"].nunique())
    add_metric(metrics, "달서구 노인인구", "total_elderly_population", int(population_numbers["계"].sum()))
    add_metric(metrics, "달서구 노인인구", "sex_sum_mismatch_rows", int((population_numbers["계"] != population_numbers["남"] + population_numbers["여"]).sum()))

    population_dongs = set(population["동명"].map(norm_name))
    for key, label in [("senior_centers", "달서구 경로당"), ("welfare", "달서구 노인복지시설")]:
        frame = data[key]
        unmatched = sorted({x for x in frame["행정동"].map(norm_name) if x and x not in population_dongs})
        add_metric(metrics, label, "rows", len(frame))
        add_metric(metrics, label, "coordinate_missing_rows", int(((frame["위도"] == "") | (frame["경도"] == "")).sum()))
        add_metric(metrics, label, "duplicate_coordinate_rows", int(frame.duplicated(["위도", "경도"], keep=False).sum()))
        add_metric(metrics, label, "unmatched_admin_dong_values", len(unmatched), " | ".join(unmatched))

    jobs = data["jobs"]
    add_metric(metrics, "달서구 노인일자리", "rows", len(jobs))
    add_metric(metrics, "달서구 노인일자리", "address_columns", int(any("주소" in col for col in jobs.columns)))
    add_metric(metrics, "달서구 노인일자리", "coordinate_columns", int(any(col in jobs.columns for col in ("위도", "경도"))))
    add_metric(metrics, "달서구 노인일자리", "performing_institutions", jobs["수행기관"].nunique())

    benches = data["benches"].copy()
    benches["개수_num"] = pd.to_numeric(benches["개수"], errors="coerce")
    add_metric(metrics, "달서구 벤치", "rows", len(benches))
    add_metric(metrics, "달서구 벤치", "exact_duplicate_rows", int(benches.drop(columns=["개수_num"]).duplicated().sum()))
    add_metric(metrics, "달서구 벤치", "coordinate_location_count", benches[["위도", "경도"]].drop_duplicates().shape[0])
    add_metric(metrics, "달서구 벤치", "zero_seat_rows", int((benches["개수_num"] == 0).sum()))
    add_metric(metrics, "달서구 벤치", "total_reported_seats", int(benches["개수_num"].sum()))
    for kind, group in benches.groupby("구분", dropna=False):
        add_metric(metrics, "달서구 벤치", f"rows_type_{kind}", len(group))
        add_metric(metrics, "달서구 벤치", f"seat_sum_type_{kind}", int(group["개수_num"].sum()))
    bus_seat_rows = benches[benches["구분"] == "버스정류장"].copy()
    bus_seat_rows["name_norm"] = bus_seat_rows["명칭"].map(norm_name)
    add_metric(metrics, "달서구 벤치", "bus_stop_rows_with_positive_seats", int((bus_seat_rows["개수_num"] > 0).sum()))
    add_metric(metrics, "달서구 벤치", "bus_stop_rows_with_zero_seats", int((bus_seat_rows["개수_num"] == 0).sum()))

    medical = data["medical"]
    add_metric(metrics, "달서구 의료기관", "rows", len(medical))
    add_metric(metrics, "달서구 의료기관", "unique_names", medical["의료기관명"].nunique())
    add_metric(metrics, "달서구 의료기관", "unique_coordinates", medical[["위도", "경도"]].drop_duplicates().shape[0])
    add_metric(metrics, "달서구 의료기관", "coordinate_groups_with_multiple_records", int((medical.groupby(["위도", "경도"]).size() > 1).sum()))

    stops = data["stops"].copy()
    dalseo_stops = stops[stops["구군"] == "달서구"].copy()
    dalseo_stops["name_norm"] = dalseo_stops["정류소명"].map(norm_name)
    add_metric(metrics, "버스 정류소 위치정보", "all_daegu_rows", len(stops))
    add_metric(metrics, "버스 정류소 위치정보", "dalseo_rows", len(dalseo_stops))
    add_metric(metrics, "버스 정류소 위치정보", "dalseo_unique_names", dalseo_stops["name_norm"].nunique())
    add_metric(metrics, "버스 정류소 위치정보", "dalseo_rows_with_duplicate_name", int(dalseo_stops.duplicated("name_norm", keep=False).sum()))
    add_metric(metrics, "버스 정류소 위치정보", "dalseo_duplicate_coordinate_rows", int(dalseo_stops.duplicated(["경도", "위도"], keep=False).sum()))
    dalseo_names = set(dalseo_stops["name_norm"])
    bench_bus_names = set(bus_seat_rows["name_norm"])
    bus_seat_name_coverage = len(dalseo_names & bench_bus_names)
    joins.append({"join": "달서구 정류소 위치 → 벤치 현황 버스정류장", "rule": "normalized_stop_name_presence", "matched": bus_seat_name_coverage, "denominator": len(dalseo_names), "rate": pct(bus_seat_name_coverage, len(dalseo_names))})
    positive_bus_seat_names = set(bus_seat_rows.loc[bus_seat_rows["개수_num"] > 0, "name_norm"])
    joins.append({"join": "달서구 정류소 위치 → 양수 의자 보유 정류장", "rule": "normalized_stop_name_and_count_gt_0", "matched": len(dalseo_names & positive_bus_seat_names), "denominator": len(dalseo_names), "rate": pct(len(dalseo_names & positive_bus_seat_names), len(dalseo_names))})
    seat_lon = pd.to_numeric(bus_seat_rows["경도"], errors="coerce").to_numpy()
    seat_lat = pd.to_numeric(bus_seat_rows["위도"], errors="coerce").to_numpy()
    seat_nearest_rows = []
    for _, row in dalseo_stops.iterrows():
        distances = haversine_m(float(row["경도"]), float(row["위도"]), seat_lon, seat_lat)
        idx = int(np.nanargmin(distances))
        match = bus_seat_rows.iloc[idx]
        seat_nearest_rows.append({
            "location_stop_name": row["정류소명"],
            "seat_record_name": match["명칭"],
            "distance_m": round(float(distances[idx]), 3),
            "seat_count": int(match["개수_num"]),
            "normalized_name_match": norm_name(row["정류소명"]) == norm_name(match["명칭"]),
        })
    seat_nearest = pd.DataFrame(seat_nearest_rows)
    seat_nearest.to_csv(OUT / "dalseo_stop_to_bus_seat_diagnostic.csv", index=False, encoding="utf-8-sig")
    for threshold in (20, 50):
        matched = int((seat_nearest["distance_m"] <= threshold).sum())
        joins.append({"join": "달서구 정류소 위치 → 벤치 현황 버스정류장", "rule": f"nearest_coordinate_within_{threshold}m", "matched": matched, "denominator": len(seat_nearest), "rate": pct(matched, len(seat_nearest))})
    positive_near = int(((seat_nearest["distance_m"] <= 20) & (seat_nearest["seat_count"] > 0)).sum())
    joins.append({"join": "달서구 정류소 위치 → 양수 의자 보유 정류장", "rule": "nearest_coordinate_within_20m_and_count_gt_0", "matched": positive_near, "denominator": len(seat_nearest), "rate": pct(positive_near, len(seat_nearest))})

    dalseo_lon = pd.to_numeric(dalseo_stops["경도"], errors="coerce")
    dalseo_lat = pd.to_numeric(dalseo_stops["위도"], errors="coerce")
    bounds = {"lon_min": float(dalseo_lon.min()), "lon_max": float(dalseo_lon.max()), "lat_min": float(dalseo_lat.min()), "lat_max": float(dalseo_lat.max())}
    for key, label in [("senior_centers", "달서구 경로당"), ("welfare", "달서구 노인복지시설"), ("benches", "달서구 벤치"), ("medical", "달서구 의료기관")]:
        frame = data[key]
        lon = pd.to_numeric(frame["경도"], errors="coerce")
        lat = pd.to_numeric(frame["위도"], errors="coerce")
        outside = (lon < bounds["lon_min"] - 0.02) | (lon > bounds["lon_max"] + 0.02) | (lat < bounds["lat_min"] - 0.02) | (lat > bounds["lat_max"] + 0.02)
        add_metric(metrics, label, "coordinate_outlier_rows_vs_dalseo_stop_extent", int(outside.sum()), json.dumps(bounds, ensure_ascii=False))
        if outside.any():
            export = frame.loc[outside].copy()
            export.to_csv(OUT / f"coordinate_outliers_{key}.csv", index=False, encoding="utf-8-sig")

    parks = data["parks"].copy()
    dalseo_parks = parks[parks["시군구"] == "달서구"].copy()
    park_benches = dalseo_parks[dalseo_parks["시설명"].str.contains("벤치", na=False)].copy()
    add_metric(metrics, "공원시설물", "all_daegu_rows", len(parks))
    add_metric(metrics, "공원시설물", "dalseo_rows", len(dalseo_parks))
    add_metric(metrics, "공원시설물", "dalseo_bench_rows", len(park_benches))
    add_metric(metrics, "공원시설물", "dalseo_bench_unique_coordinates", park_benches[["위도", "경도"]].drop_duplicates().shape[0])
    add_metric(metrics, "공원시설물", "dalseo_bench_repair_needed", int((park_benches["공원시설물수리필요"] == "Y").sum()))
    add_metric(metrics, "공원시설물", "data_age_date", parks["데이터기준일"].iloc[0])

    bench_coords = set(zip(benches["위도"], benches["경도"]))
    exact_park_coord_overlap = sum((lat, lon) in bench_coords for lat, lon in zip(park_benches["위도"], park_benches["경도"]))
    add_metric(metrics, "공원시설물", "exact_coordinate_overlap_with_dalseo_bench_rows", int(exact_park_coord_overlap))
    park_aggregate = benches[benches["구분"] == "공원"].copy()
    park_aggregate["park_norm"] = park_aggregate["명칭"].map(norm_name)
    park_benches["park_norm"] = park_benches["공원명"].map(norm_name)
    aggregate_names = set(park_aggregate["park_norm"])
    facility_park_names = set(park_benches["park_norm"])
    joins.append({"join": "달서구 벤치 공원행 → 공원시설물 개별 벤치", "rule": "normalized_park_name_presence", "matched": len(aggregate_names & facility_park_names), "denominator": len(aggregate_names), "rate": pct(len(aggregate_names & facility_park_names), len(aggregate_names))})
    park_comparison = (
        park_aggregate.groupby("park_norm", as_index=False)["개수_num"].sum()
        .rename(columns={"개수_num": "reported_2026_bench_count"})
        .merge(
            park_benches.groupby("park_norm").size().rename("individual_2022_bench_rows").reset_index(),
            on="park_norm",
            how="outer",
        )
    )
    park_comparison["count_difference_2026_minus_2022"] = park_comparison["reported_2026_bench_count"].fillna(0) - park_comparison["individual_2022_bench_rows"].fillna(0)
    park_comparison.to_csv(OUT / "park_bench_source_comparison.csv", index=False, encoding="utf-8-sig")

    bus_dbf_path = next(ROOT.rglob("bs_20250903.dbf"))
    bus_dbf = parse_dbf(bus_dbf_path)
    bus_dbf["name_norm"] = bus_dbf["bs_nm"].map(norm_name)
    bus_dbf["lon_wgs84"], bus_dbf["lat_wgs84"] = inverse_korea_2000_east_belt(bus_dbf["ngis_x_pos"], bus_dbf["ngis_y_pos"])
    add_metric(metrics, "버스 정류소 공간정보(bs)", "rows", len(bus_dbf))
    add_metric(metrics, "버스 정류소 공간정보(bs)", "unique_bs_id", bus_dbf["bs_id"].nunique())
    add_metric(metrics, "버스 정류소 공간정보(bs)", "duplicate_bs_id_rows", int(bus_dbf.duplicated("bs_id", keep=False).sum()))

    bus_lon = bus_dbf["lon_wgs84"].to_numpy()
    bus_lat = bus_dbf["lat_wgs84"].to_numpy()
    nearest_rows = []
    for _, row in dalseo_stops.iterrows():
        distances = haversine_m(float(row["경도"]), float(row["위도"]), bus_lon, bus_lat)
        idx = int(np.nanargmin(distances))
        match = bus_dbf.iloc[idx]
        nearest_rows.append(
            {
                "location_stop_name": row["정류소명"],
                "location_lon": row["경도"],
                "location_lat": row["위도"],
                "bs_id": match["bs_id"],
                "bs_name": match["bs_nm"],
                "distance_m": round(float(distances[idx]), 3),
                "normalized_name_match": norm_name(row["정류소명"]) == norm_name(match["bs_nm"]),
            }
        )
    nearest = pd.DataFrame(nearest_rows)
    nearest.to_csv(OUT / "dalseo_stop_to_bs_crosswalk_diagnostic.csv", index=False, encoding="utf-8-sig")
    for threshold in (5, 20, 50, 100):
        count = int((nearest["distance_m"] <= threshold).sum())
        joins.append({"join": "달서구 위치 CSV → bs 공간정보", "rule": f"nearest_coordinate_within_{threshold}m", "matched": count, "denominator": len(nearest), "rate": pct(count, len(nearest))})
    both = int(((nearest["distance_m"] <= 20) & nearest["normalized_name_match"]).sum())
    joins.append({"join": "달서구 위치 CSV → bs 공간정보", "rule": "within_20m_and_normalized_name_match", "matched": both, "denominator": len(nearest), "rate": pct(both, len(nearest))})

    monthly_paths = sorted(path for path in ROOT.rglob("*.csv") if "월별 이용자수" in nfc(str(path.parent)))
    monthly_frames = []
    monthly_schema = []
    for path in monthly_paths:
        frame = read_csv(path)
        frame["source_file"] = nfc(path.name)
        monthly_frames.append(frame)
        monthly_schema.append({"file": nfc(path.name), "rows": len(frame), "columns": "|".join(col for col in frame.columns if col != "source_file")})
    monthly = pd.concat(monthly_frames, ignore_index=True, sort=False)
    monthly["name_norm"] = monthly["정류소명"].map(norm_name)
    monthly["grain_key"] = monthly["년월"] + "|" + monthly["name_norm"]
    monthly_dup = int(monthly.duplicated("grain_key", keep=False).sum())
    add_metric(metrics, "버스 정류소별 월별 이용자수", "files", len(monthly_paths))
    add_metric(metrics, "버스 정류소별 월별 이용자수", "rows", len(monthly))
    add_metric(metrics, "버스 정류소별 월별 이용자수", "rows_in_duplicate_month_name_grain", monthly_dup)
    add_metric(metrics, "버스 정류소별 월별 이용자수", "schema_count", len(set(x["columns"] for x in monthly_schema)))
    latest_monthly = monthly[monthly["년월"].astype(str).str.startswith("2025") | monthly["년월"].astype(str).str.startswith("2026")]
    matched_monthly = int(latest_monthly["name_norm"].isin(dalseo_names).sum())
    joins.append({"join": "최근 월별 이용량 → 달서구 위치 CSV", "rule": "normalized_stop_name", "matched": matched_monthly, "denominator": len(latest_monthly), "rate": pct(matched_monthly, len(latest_monthly))})
    latest_monthly_names = set(latest_monthly["name_norm"])
    dalseo_name_coverage = len(dalseo_names & latest_monthly_names)
    joins.append({"join": "달서구 위치 CSV → 최근 월별 이용량", "rule": "normalized_stop_name_presence", "matched": dalseo_name_coverage, "denominator": len(dalseo_names), "rate": pct(dalseo_name_coverage, len(dalseo_names))})

    hourly_paths = sorted(path for path in ROOT.rglob("*.csv") if "시간대별 승하차" in nfc(str(path.parent)))
    hourly_frames = []
    strict_decode_error_files = []
    hourly_schema = []
    for path in hourly_paths:
        decodable = False
        for candidate_encoding in ("utf-8-sig", "cp949", "euc-kr", "utf-8"):
            try:
                path.read_text(encoding=candidate_encoding)
                decodable = True
                break
            except UnicodeDecodeError:
                continue
        if not decodable:
            strict_decode_error_files.append(nfc(path.name))
        frame = read_csv(path)
        frame["source_file"] = nfc(path.name)
        hourly_frames.append(frame)
        hourly_schema.append({"file": nfc(path.name), "rows": len(frame), "columns": "|".join(col for col in frame.columns if col != "source_file")})
    hourly = pd.concat(hourly_frames, ignore_index=True, sort=False)
    hourly["id_norm"] = hourly["정류소ID"].astype(str).str.strip()
    hourly["name_norm"] = hourly["정류소명"].map(norm_name)
    hourly["grain_key"] = hourly["년월"] + "|" + hourly["id_norm"] + "|" + hourly["구분"]
    hourly_dup = int(hourly.duplicated("grain_key", keep=False).sum())
    add_metric(metrics, "버스 정류소별 시간대별 승하차", "files_found", len(hourly_paths))
    add_metric(metrics, "버스 정류소별 시간대별 승하차", "strict_decode_error_files", len(strict_decode_error_files), " | ".join(strict_decode_error_files))
    add_metric(metrics, "버스 정류소별 시간대별 승하차", "nonempty_rows", len(hourly))
    add_metric(metrics, "버스 정류소별 시간대별 승하차", "schema_count_nonempty", len(set(x["columns"] for x in hourly_schema)))
    add_metric(metrics, "버스 정류소별 시간대별 승하차", "rows_in_duplicate_month_id_direction_grain", hourly_dup)
    add_metric(metrics, "버스 정류소별 시간대별 승하차", "blank_stop_id_rows", int((hourly["id_norm"] == "").sum()))
    add_metric(metrics, "버스 정류소별 시간대별 승하차", "blank_direction_rows", int((hourly["구분"] == "").sum()))
    add_metric(metrics, "버스 정류소별 시간대별 승하차", "unique_stop_ids", hourly["id_norm"].nunique())
    add_metric(metrics, "버스 정류소별 시간대별 승하차", "direction_values", hourly["구분"].nunique(), " | ".join(sorted(hourly["구분"].unique())))
    bs_ids = set(bus_dbf["bs_id"].astype(str).str.strip())
    matched_hourly_ids = int(hourly["id_norm"].isin(bs_ids).sum())
    joins.append({"join": "시간대별 승하차 → bs 공간정보", "rule": "정류소ID=bs_id", "matched": matched_hourly_ids, "denominator": len(hourly), "rate": pct(matched_hourly_ids, len(hourly))})
    unique_hourly_ids = set(hourly["id_norm"])
    joins.append({"join": "시간대별 승하차 고유 ID → bs 공간정보", "rule": "정류소ID=bs_id", "matched": len(unique_hourly_ids & bs_ids), "denominator": len(unique_hourly_ids), "rate": pct(len(unique_hourly_ids & bs_ids), len(unique_hourly_ids))})

    crosswalk_ids = set(nearest.loc[nearest["distance_m"] <= 20, "bs_id"].astype(str))
    dalseo_hourly = hourly[hourly["id_norm"].isin(crosswalk_ids)]
    joins.append({"join": "시간대별 승하차 → 달서구 정류장", "rule": "20m_crosswalk_bs_id", "matched": len(dalseo_hourly), "denominator": len(hourly), "rate": pct(len(dalseo_hourly), len(hourly))})

    blank_key_by_file = (
        hourly[(hourly["id_norm"] == "") | (hourly["구분"] == "")]
        .groupby("source_file")
        .size()
        .sort_values(ascending=False)
    )
    findings.extend(
        [
            {"finding_id": "DQ-01", "severity": "Low", "dataset": "버스 시간대별", "finding": "모든 값이 비어 있는 행이 존재", "impact": "집계 전 완전 공백 행을 제거해야 하나 유효 관측치에는 직접 영향이 적음", "evidence": " | ".join(f"{name}: {count}행" for name, count in blank_key_by_file.items())},
            {"finding_id": "DQ-02", "severity": "High", "dataset": "버스 정류소 위치/이용량", "finding": "위치 CSV에는 정류소 ID가 없고 월별 이용량도 정류소명만 제공", "impact": "동명이거나 표기가 바뀐 정류소에서 이름 조인 오매칭 위험", "evidence": "위치 컬럼: 정류소명·경위도, 월별 컬럼: 년월·정류소명·승하차"},
            {"finding_id": "DQ-03", "severity": "High", "dataset": "노인일자리", "finding": "주소와 좌표 컬럼이 없음", "impact": "개별 사업을 정기 이동 목적지로 직접 공간화할 수 없음", "evidence": f"{len(jobs)}행, 수행기관 {jobs['수행기관'].nunique()}개"},
            {"finding_id": "DQ-04", "severity": "Medium", "dataset": "달서구 벤치", "finding": "정확히 중복된 행과 0개로 기록된 위치가 존재", "impact": "중복은 제거해야 하며 0개 위치는 결측치가 아니라 휴식시설 공급에서 제외할 상태값으로 관리해야 함", "evidence": f"정확 중복 {int(benches.drop(columns=['개수_num']).duplicated().sum())}행, 0개 {int((benches['개수_num']==0).sum())}행(버스정류장 {int(((benches['구분']=='버스정류장') & (benches['개수_num']==0)).sum())}행)"},
            {"finding_id": "DQ-05", "severity": "Medium", "dataset": "공원시설물", "finding": "기준일이 2022-12-09로 다른 핵심 자료보다 오래됨", "impact": "벤치의 철거·신설 및 상태가 현재와 다를 수 있음", "evidence": parks["데이터기준일"].iloc[0]},
            {"finding_id": "DQ-06", "severity": "Medium", "dataset": "버스 이용량", "finding": "연도별 인코딩과 컬럼 구조가 다름", "impact": "통합 전 합계 컬럼 생성 및 인코딩 표준화 필요", "evidence": f"월별 스키마 {len(set(x['columns'] for x in monthly_schema))}개, 시간대별 스키마 {len(set(x['columns'] for x in hourly_schema))}개"},
            {"finding_id": "DQ-07", "severity": "High", "dataset": "달서구 경로당", "finding": "달서구 범위에서 크게 벗어난 좌표가 3건 존재", "impact": "보정 없이 경로 출발지로 사용하면 비현실적인 장거리 경로가 생성됨", "evidence": "두류파크kcc스위첸경로당 | 죽전역코오롱하늘채경로당 | 월성월드메르디앙경로당"},
            {"finding_id": "DQ-08", "severity": "High", "dataset": "달서구 벤치·공원시설물", "finding": "2026년 공원 벤치 자료는 공원별 합계 위치이고 2022년 자료는 개별 벤치 좌표임", "impact": "공원별 대표점에 합계 개수를 배치하면 실제 벤치 접근거리를 왜곡하므로 두 자료의 역할을 분리해야 함", "evidence": f"2026 공원행 {len(park_aggregate)}개·벤치 {int(park_aggregate['개수_num'].sum())}개, 2022 개별 벤치행 {len(park_benches)}개"},
            {"finding_id": "DQ-09", "severity": "Medium", "dataset": "노인복지시설", "finding": "노인인구 행정동과 일치하지 않는 행정동 표기가 존재", "impact": "행정동 단순 문자열 조인 시 시설이 누락되거나 잘못 배정될 수 있어 공간조인 또는 표준화가 필요", "evidence": "용산동(행정동 세분 필요) | 진동동(진천동 오기 추정)"},
        ]
    )

    pd.DataFrame(metrics).to_csv(OUT / "core_dataset_metrics.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(joins).to_csv(OUT / "join_coverage.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(findings).to_csv(OUT / "quality_findings.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(monthly_schema).to_csv(OUT / "monthly_bus_schema_by_file.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(hourly_schema).to_csv(OUT / "hourly_bus_schema_by_file.csv", index=False, encoding="utf-8-sig")
    summary = {"metrics": metrics, "joins": joins, "findings": findings, "dalseo_stop_extent": bounds}
    (OUT / "key_quality_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"metrics": len(metrics), "joins": len(joins), "findings": len(findings)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
