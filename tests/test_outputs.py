"""Behavior tests for factor-loading recovery and portfolio construction."""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


ENTRY = os.environ.get("FINANCE_ENTRY", "/app/factor_book.py")
REFUSED = "FINANCE-CASE-REFUSED: "
FACTORS = ["curve", "credit", "momentum"]
BOUNDS = {"curve": [3.0, 8.0], "credit": [3.0, 8.0], "momentum": [2.0, 7.0]}
ASSETS = [
    {"id": "AST-Q7", "max_units": 3, "price": 5.0, "return": 0.18},
    {"id": "AST-M2", "max_units": 3, "price": 7.0, "return": 0.14},
    {"id": "AST-R9", "max_units": 4, "price": 4.0, "return": 0.12},
    {"id": "AST-K4", "max_units": 2, "price": 6.0, "return": 0.16},
    {"id": "AST-T5", "max_units": 4, "price": 3.0, "return": 0.10},
]
LOADINGS = {
    "AST-Q7": {"curve": 1.0, "credit": 0.2, "momentum": 0.0},
    "AST-M2": {"curve": 0.5, "credit": 1.2, "momentum": 0.1},
    "AST-R9": {"curve": 0.2, "credit": 0.4, "momentum": 0.8},
    "AST-K4": {"curve": 0.9, "credit": 0.1, "momentum": 0.4},
    "AST-T5": {"curve": 0.1, "credit": 0.7, "momentum": 1.1},
}
UNITS = [
    [4, 0, 0, 0, 0],
    [0, 4, 0, 0, 0],
    [0, 0, 4, 0, 0],
    [0, 0, 0, 4, 0],
    [0, 0, 0, 0, 4],
    [1, 2, 1, 2, 1],
]


def risk_for(vector: list[int]) -> dict[str, float]:
    asset_ids = [item["id"] for item in ASSETS]
    return {
        factor: sum(vector[index] * LOADINGS[asset_id][factor] for index, asset_id in enumerate(asset_ids))
        for factor in FACTORS
    }


def build_payload(case_id: str = "BOOK-2026-OCT") -> dict:
    asset_ids = [item["id"] for item in ASSETS]
    observations = [
        {
            "factor_risk": risk_for(vector),
            "observation": f"mandate-{index + 1:02d}",
            "units": {asset_id: vector[index] for index, asset_id in enumerate(asset_ids)},
        }
        for index, vector in enumerate(UNITS)
    ]
    return {
        "assets": copy.deepcopy(ASSETS),
        "capital": 60,
        "case_id": case_id,
        "factor_bounds": copy.deepcopy(BOUNDS),
        "factors": list(FACTORS),
        "observations": observations,
        "schema_version": 1,
        "unit": "currency",
    }


def write_case(tmp_path: Path, payload: dict, name: str = "scenario") -> Path:
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return path


def invoke(
    tmp_path: Path,
    payload: dict | None = None,
    *,
    raw: str | None = None,
    journal_name: str = "journal.jsonl",
    report_name: str = "portfolio.json",
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
    scenario = tmp_path / "input" / "scenario.json"
    scenario.parent.mkdir(parents=True, exist_ok=True)
    if raw is None:
        scenario.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    else:
        scenario.write_text(raw, encoding="utf-8")
    journal = tmp_path / "state" / journal_name
    report = tmp_path / "out" / report_name
    process = subprocess.run(
        [
            sys.executable,
            ENTRY,
            "--scenario",
            str(scenario),
            "--journal",
            str(journal),
            "--report",
            str(report),
        ],
        capture_output=True,
        text=True,
        timeout=4,
    )
    return process, scenario, journal, report


def read_report(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def compact(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def expected_portfolio() -> tuple[dict[str, int], float, float, dict[str, float]]:
    ranges = [range(item["max_units"] + 1) for item in ASSETS]
    best_vector: tuple[int, ...] | None = None
    best_objective = -math.inf
    best_spend = 0.0
    best_exposure: dict[str, float] = {}
    for vector in itertools.product(*ranges):
        spend = sum(vector[index] * ASSETS[index]["price"] for index in range(len(ASSETS)))
        if spend > 60:
            continue
        objective = sum(vector[index] * ASSETS[index]["return"] for index in range(len(ASSETS)))
        exposure = risk_for(list(vector))
        if any(exposure[factor] < BOUNDS[factor][0] - 1e-9 or exposure[factor] > BOUNDS[factor][1] + 1e-9 for factor in FACTORS):
            continue
        if objective > best_objective + 1e-9 or (
            abs(objective - best_objective) <= 1e-9
            and (best_vector is None or vector < best_vector)
        ):
            best_vector = vector
            best_objective = objective
            best_spend = spend
            best_exposure = exposure
    assert best_vector is not None
    allocations = {ASSETS[index]["id"]: best_vector[index] for index in range(len(ASSETS))}
    return allocations, best_objective, best_spend, best_exposure


def test_existing_inspect_mode_returns_zero() -> None:
    """题面：「`--inspect` 作为只读检查入口，结束时返回 0」"""
    process = subprocess.run(
        [sys.executable, ENTRY, "--inspect"],
        capture_output=True,
        text=True,
        timeout=4,
    )
    assert process.returncode == 0

def test_success_is_silent_and_creates_report(tmp_path: Path) -> None:
    """题面：「成功路径不打印日志，标准输出和标准错误均为空，退出码为 0」"""
    process, _, _, report = invoke(tmp_path, build_payload())
    assert process.returncode == 0
    assert process.stdout == ""
    assert process.stderr == ""
    assert report.is_file()

def test_missing_argument_returns_three(tmp_path: Path) -> None:
    """题面：「失败收口使用退出码 3」"""
    report_path = tmp_path / "out" / "case_02.json"
    process = subprocess.run(
        [sys.executable, ENTRY, "--scenario", str(tmp_path / "scenario.json")],
        capture_output=True,
        text=True,
        timeout=4,
    )
    assert process.returncode == 3
    assert report_path.exists() is False
    assert process.stderr.startswith(REFUSED)

def test_malformed_json_returns_three(tmp_path: Path) -> None:
    """题面：「根对象读取八个字段」"""
    process, _, _, report_path = invoke(tmp_path, raw="{")
    assert process.returncode == 3
    assert report_path.exists() is False
    assert process.stdout == ""

def test_duplicate_top_level_key_returns_three(tmp_path: Path) -> None:
    """题面：「根对象读取八个字段：`assets`、`capital`、`case_id`、`factor_bounds`、`factors`、`observations`、`schema_version`、`unit`」"""
    raw = (
        '{"assets":[],"capital":1,"case_id":"A-B-C","case_id":"D-E-F",'
        '"factor_bounds":{},"factors":[],"observations":[],"schema_version":1,"unit":"currency"}'
    )
    process, _, _, report_path = invoke(tmp_path, raw=raw)
    assert process.returncode == 3
    assert report_path.exists() is False
    assert process.stderr.startswith(REFUSED)

def test_top_level_array_returns_three(tmp_path: Path) -> None:
    """题面：「根对象读取八个字段」"""
    process, _, _, report_path = invoke(tmp_path, raw="[]")
    assert process.returncode == 3
    assert report_path.exists() is False
    assert process.stderr.startswith(REFUSED)

def test_missing_top_level_key_returns_three(tmp_path: Path) -> None:
    """题面：「根对象读取八个字段：`assets`、`capital`、`case_id`、`factor_bounds`、`factors`、`observations`、`schema_version`、`unit`」"""
    payload = build_payload()
    del payload["unit"]
    process, _, _, report_path = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report_path.exists() is False
    assert process.stderr.startswith(REFUSED)

def test_extra_top_level_key_returns_three(tmp_path: Path) -> None:
    """题面：「根对象读取八个字段：`assets`、`capital`、`case_id`、`factor_bounds`、`factors`、`observations`、`schema_version`、`unit`」"""
    payload = build_payload()
    payload["analyst"] = "unused"
    process, _, _, report_path = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report_path.exists() is False
    assert process.stderr.startswith(REFUSED)

def test_schema_version_bool_returns_three(tmp_path: Path) -> None:
    """题面：「| `schema_version` | 整数 1 |」"""
    payload = build_payload()
    payload["schema_version"] = True
    process, _, _, report_path = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report_path.exists() is False
    assert process.stderr.startswith(REFUSED)

def test_unit_value_returns_three(tmp_path: Path) -> None:
    """题面：「| `unit` | 字符串 `"currency"` |」"""
    payload = build_payload()
    payload["unit"] = "shares"
    process, _, _, report_path = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report_path.exists() is False
    assert process.stderr.startswith(REFUSED)

def test_case_id_shape_returns_three(tmp_path: Path) -> None:
    """题面：「| `case_id` | 包含账簿段和月份段的非空编号 |」"""
    process, _, _, report_path = invoke(tmp_path, build_payload(case_id="OCT"))
    assert process.returncode == 3
    assert report_path.exists() is False
    assert process.stderr.startswith(REFUSED)

def test_capital_zero_returns_three(tmp_path: Path) -> None:
    """题面：「| `capital` | 1 到 100 的整数 |」"""
    payload = build_payload()
    payload["capital"] = 0
    process, _, _, report_path = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report_path.exists() is False
    assert process.stderr.startswith(REFUSED)

def test_factor_count_returns_three(tmp_path: Path) -> None:
    """题面：「| `factors` | 三个互不相同的非空名称 |」"""
    payload = build_payload()
    payload["factors"] = payload["factors"][:2]
    process, _, _, report_path = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report_path.exists() is False
    assert process.stderr.startswith(REFUSED)

def test_factor_bounds_missing_key_returns_three(tmp_path: Path) -> None:
    """题面：「| `factor_bounds` | 每个因子对应一对有限数，左端不大于右端 |」"""
    payload = build_payload()
    del payload["factor_bounds"]["momentum"]
    process, _, _, report_path = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report_path.exists() is False
    assert process.stderr.startswith(REFUSED)

def test_factor_bounds_reversed_returns_three(tmp_path: Path) -> None:
    """题面：「每个因子对应一对有限数，左端不大于右端」"""
    payload = build_payload()
    payload["factor_bounds"]["curve"] = [8.0, 3.0]
    process, _, _, report_path = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report_path.exists() is False
    assert process.stderr.startswith(REFUSED)

def test_too_few_assets_returns_three(tmp_path: Path) -> None:
    """题面：「资产条目只含 `id`、`max_units`、`price`、`return`；清单长度 4 到 8」"""
    payload = build_payload()
    payload["assets"] = payload["assets"][:3]
    process, _, _, report_path = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report_path.exists() is False
    assert process.stderr.startswith(REFUSED)

def test_duplicate_asset_returns_three(tmp_path: Path) -> None:
    """题面：「编号不可重复」"""
    payload = build_payload()
    payload["assets"][1]["id"] = payload["assets"][0]["id"]
    process, _, _, report_path = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report_path.exists() is False
    assert process.stderr.startswith(REFUSED)

def test_price_bool_returns_three(tmp_path: Path) -> None:
    """题面：「价格为正有限数」"""
    payload = build_payload()
    payload["assets"][0]["price"] = True
    process, _, _, report_path = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report_path.exists() is False
    assert process.stderr.startswith(REFUSED)

def test_too_few_observations_returns_three(tmp_path: Path) -> None:
    """题面：「复核输入条目只含 `factor_risk`、`observation`、`units`；清单长度 6 到 12」"""
    payload = build_payload()
    payload["observations"] = payload["observations"][:5]
    process, _, _, report_path = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report_path.exists() is False
    assert process.stderr.startswith(REFUSED)

def test_duplicate_observation_returns_three(tmp_path: Path) -> None:
    """题面：「观察编号不可重复」"""
    payload = build_payload()
    payload["observations"][1]["observation"] = payload["observations"][0]["observation"]
    process, _, _, report_path = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report_path.exists() is False
    assert process.stderr.startswith(REFUSED)

def test_units_missing_asset_returns_three(tmp_path: Path) -> None:
    """题面：「单位向量的键与资产对齐」"""
    payload = build_payload()
    del payload["observations"][2]["units"]["AST-R9"]
    process, _, _, report_path = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report_path.exists() is False
    assert process.stderr.startswith(REFUSED)

def test_units_bool_returns_three(tmp_path: Path) -> None:
    """题面：「单位向量的键与资产对齐」"""
    payload = build_payload()
    payload["observations"][2]["units"]["AST-R9"] = False
    process, _, _, report_path = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report_path.exists() is False
    assert process.stderr.startswith(REFUSED)

def test_factor_risk_extra_key_returns_three(tmp_path: Path) -> None:
    """题面：「因子风险的键与因子对齐」"""
    payload = build_payload()
    payload["observations"][0]["factor_risk"]["vol"] = 1.0
    process, _, _, report_path = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report_path.exists() is False
    assert process.stderr.startswith(REFUSED)

def test_factor_loadings_recovered(tmp_path: Path) -> None:
    """题面：「每个因子的载荷都必须由全部聚合组合的资产单位向量联合最小二乘反演」"""
    process, _, _, report_path = invoke(tmp_path, build_payload())
    recovered = read_report(report_path)["factor_loadings"]
    assert process.returncode == 0
    assert recovered["AST-Q7"]["curve"] == pytest.approx(1.0, abs=1e-8)
    assert recovered["AST-M2"]["credit"] == pytest.approx(1.2, abs=1e-8)
    assert recovered["AST-T5"]["momentum"] == pytest.approx(1.1, abs=1e-8)

def test_allocation_matches_global_optimum(tmp_path: Path) -> None:
    """题面：「目标是在这些条件成立时最大化 `sum(units × return)`」"""
    expected, _, _, _ = expected_portfolio()
    process, _, _, report_path = invoke(tmp_path, build_payload())
    report = read_report(report_path)
    assert process.returncode == 0
    assert report["allocations"] == expected

def test_spend_matches_allocations(tmp_path: Path) -> None:
    """题面：「总成本不得超过 `capital`」"""
    process, _, _, report_path = invoke(tmp_path, build_payload())
    report = read_report(report_path)
    lookup = {item["id"]: item for item in ASSETS}
    expected = sum(report["allocations"][name] * lookup[name]["price"] for name in lookup)
    assert process.returncode == 0
    assert report["spend"] == pytest.approx(expected, abs=1e-9)
    assert report["spend"] <= 60

def test_objective_matches_allocations(tmp_path: Path) -> None:
    """题面：「组合产物为 UTF-8 JSON」"""
    process, _, _, report_path = invoke(tmp_path, build_payload())
    report = read_report(report_path)
    lookup = {item["id"]: item for item in ASSETS}
    expected = sum(report["allocations"][name] * lookup[name]["return"] for name in lookup)
    assert process.returncode == 0
    assert report["objective"] == pytest.approx(expected, abs=1e-9)

def test_exposure_matches_loadings(tmp_path: Path) -> None:
    """题面：「每个因子的组合敞口必须落在该因子的闭区间内」"""
    process, _, _, report_path = invoke(tmp_path, build_payload())
    report = read_report(report_path)
    recomputed = {
        factor: sum(
            report["allocations"][asset] * report["factor_loadings"][asset][factor]
            for asset in report["allocations"]
        )
        for factor in FACTORS
    }
    assert process.returncode == 0
    assert report["exposure"] == pytest.approx(recomputed, abs=1e-8)
    assert all(BOUNDS[factor][0] <= report["exposure"][factor] <= BOUNDS[factor][1] for factor in FACTORS)

def test_report_exact_keys(tmp_path: Path) -> None:
    """题面：「组合产物为 UTF-8 JSON，字段分布如下」"""
    process, _, _, report_path = invoke(tmp_path, build_payload())
    report = read_report(report_path)
    assert process.returncode == 0
    assert set(report) == {
        "allocations",
        "case_id",
        "checksum",
        "exposure",
        "factor_loadings",
        "journal_entry",
        "objective",
        "schema_version",
        "solver_nodes",
        "spend",
    }

def test_compact_json_and_trailing_newline(tmp_path: Path) -> None:
    """题面：「报告按键字典序、无空格，并以单个换行结束」"""
    process, _, _, report_path = invoke(tmp_path, build_payload())
    raw = report_path.read_bytes()
    assert process.returncode == 0
    assert raw == compact(json.loads(raw))

def test_checksum_matches_result(tmp_path: Path) -> None:
    """题面：「剔除 `journal_entry` 后对报告紧凑字节计算的校验摘要」"""
    process, _, _, report_path = invoke(tmp_path, build_payload())
    report = read_report(report_path)
    without_entry = {key: value for key, value in report.items() if key != "journal_entry"}
    assert process.returncode == 0
    assert report["journal_entry"]["report_sha256"] == hashlib.sha256(compact(without_entry)).hexdigest()

def test_journal_repeat_is_idempotent(tmp_path: Path) -> None:
    """题面：「同一情景和同一报告字节的复核不会新增流水」"""
    first, scenario, journal, report = invoke(tmp_path, build_payload())
    before = report.read_bytes()
    second = subprocess.run(
        [
            sys.executable,
            ENTRY,
            "--scenario",
            str(scenario),
            "--journal",
            str(journal),
            "--report",
            str(report),
        ],
        capture_output=True,
        text=True,
        timeout=4,
    )
    assert first.returncode == 0
    assert second.returncode == 0
    assert len(journal.read_text(encoding="utf-8").splitlines()) == 1
    assert report.read_bytes() == before

def test_failure_preserves_existing_outputs(tmp_path: Path) -> None:
    """题面：「不改变旧报告和流水，也不留下临时文件」"""
    success, scenario, journal, report = invoke(tmp_path, build_payload())
    journal_before = journal.read_bytes()
    report_before = report.read_bytes()
    scenario.write_text("{broken", encoding="utf-8")
    failed = subprocess.run(
        [
            sys.executable,
            ENTRY,
            "--scenario",
            str(scenario),
            "--journal",
            str(journal),
            "--report",
            str(report),
        ],
        capture_output=True,
        text=True,
        timeout=4,
    )
    assert success.returncode == 0
    assert failed.returncode == 3
    assert journal.read_bytes() == journal_before
    assert report.read_bytes() == report_before

def test_normal_case_finishes_within_three_seconds(tmp_path: Path) -> None:
    """题面：「常规规模的一次组合构建，3 秒内应完成」"""
    scenario = write_case(tmp_path, build_payload())
    journal_path = tmp_path / "state" / "timed.jsonl"
    report_path = tmp_path / "out" / "timed.json"
    start = time.monotonic()
    process = subprocess.run(
        [
            sys.executable,
            ENTRY,
            "--scenario",
            str(scenario),
            "--journal",
            str(journal_path),
            "--report",
            str(report_path),
        ],
        capture_output=True,
        text=True,
        timeout=4,
    )
    elapsed = time.monotonic() - start
    assert process.returncode == 0
    assert report_path.is_file()
    assert elapsed < 3.0
