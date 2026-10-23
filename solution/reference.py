#!/usr/bin/env python3
"""Recover anonymous factor loadings and build a constrained portfolio."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


REFUSED = "FINANCE-CASE-REFUSED: "
TOP_KEYS = {
    "assets",
    "capital",
    "case_id",
    "factor_bounds",
    "factors",
    "observations",
    "schema_version",
    "unit",
}
ASSET_KEYS = {"id", "max_units", "price", "return"}
OBSERVATION_KEYS = {"factor_risk", "observation", "units"}
EPSILON = 1e-9


class Refusal(ValueError):
    """The portfolio case is not admissible."""


def finite(value: Any) -> bool:
    return type(value) in {int, float} and math.isfinite(float(value))


def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Refusal(f"duplicate key: {key}")
        result[key] = value
    return result


def read_json(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise Refusal(f"cannot read scenario: {exc}") from exc
    try:
        value = json.loads(text, object_pairs_hook=reject_duplicates)
    except json.JSONDecodeError as exc:
        raise Refusal(f"invalid scenario JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise Refusal("scenario root must be an object")
    return value


def exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        raise Refusal(f"{label} keys differ; missing={missing} extra={extra}")


def validate(raw: dict[str, Any]) -> dict[str, Any]:
    exact_keys(raw, TOP_KEYS, "scenario")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise Refusal("schema_version must be integer 1")
    if raw["unit"] != "currency":
        raise Refusal("unit must be currency")
    case_id = raw["case_id"]
    if not isinstance(case_id, str) or len(case_id.split("-")) < 3:
        raise Refusal("case_id must contain book and month segments")
    capital = raw["capital"]
    if type(capital) is not int or not 1 <= capital <= 100:
        raise Refusal("capital must be an integer from 1 through 100")

    factors = raw["factors"]
    if (
        not isinstance(factors, list)
        or len(factors) != 3
        or any(not isinstance(item, str) or not item.strip() for item in factors)
        or len(set(factors)) != len(factors)
    ):
        raise Refusal("factors must contain three unique non-empty names")
    bounds = raw["factor_bounds"]
    if not isinstance(bounds, dict) or set(bounds) != set(factors):
        raise Refusal("factor_bounds keys must exactly cover factors")
    clean_bounds: dict[str, list[float]] = {}
    for factor in factors:
        interval = bounds[factor]
        if (
            not isinstance(interval, list)
            or len(interval) != 2
            or any(not finite(value) for value in interval)
            or float(interval[0]) > float(interval[1])
        ):
            raise Refusal("each factor bound must be a finite ordered pair")
        clean_bounds[factor] = [float(interval[0]), float(interval[1])]

    assets = raw["assets"]
    if not isinstance(assets, list) or not 4 <= len(assets) <= 8:
        raise Refusal("assets must contain between four and eight entries")
    asset_ids: list[str] = []
    clean_assets: list[dict[str, Any]] = []
    for index, item in enumerate(assets):
        if not isinstance(item, dict):
            raise Refusal(f"assets[{index}] must be an object")
        exact_keys(item, ASSET_KEYS, f"assets[{index}]")
        asset_id = item["id"]
        if not isinstance(asset_id, str) or not asset_id.strip():
            raise Refusal("asset id must be non-empty")
        if asset_id in asset_ids:
            raise Refusal("asset ids must be unique")
        maximum = item["max_units"]
        if type(maximum) is not int or not 1 <= maximum <= 4:
            raise Refusal("max_units must be an integer from 1 through 4")
        if not finite(item["price"]) or float(item["price"]) <= 0.0:
            raise Refusal("price must be positive and finite")
        if not finite(item["return"]):
            raise Refusal("return must be finite")
        asset_ids.append(asset_id)
        clean_assets.append(
            {
                "id": asset_id,
                "max_units": maximum,
                "price": float(item["price"]),
                "return": float(item["return"]),
            }
        )

    observations = raw["observations"]
    if not isinstance(observations, list) or not 6 <= len(observations) <= 12:
        raise Refusal("observations must contain between six and twelve entries")
    observation_ids: set[str] = set()
    clean_observations: list[dict[str, Any]] = []
    for index, item in enumerate(observations):
        if not isinstance(item, dict):
            raise Refusal(f"observations[{index}] must be an object")
        exact_keys(item, OBSERVATION_KEYS, f"observations[{index}]")
        observation = item["observation"]
        if not isinstance(observation, str) or not observation.strip():
            raise Refusal("observation id must be non-empty")
        if observation in observation_ids:
            raise Refusal("observation ids must be unique")
        observation_ids.add(observation)
        units = item["units"]
        if not isinstance(units, dict) or set(units) != set(asset_ids):
            raise Refusal("units keys must exactly cover assets")
        clean_units: dict[str, int] = {}
        for asset_id in asset_ids:
            value = units[asset_id]
            if type(value) is not int or not 0 <= value <= 4:
                raise Refusal("unit values must be integers from 0 through 4")
            clean_units[asset_id] = value
        risks = item["factor_risk"]
        if not isinstance(risks, dict) or set(risks) != set(factors):
            raise Refusal("factor_risk keys must exactly cover factors")
        clean_risks: dict[str, float] = {}
        for factor in factors:
            if not finite(risks[factor]):
                raise Refusal("factor risk must be finite")
            clean_risks[factor] = float(risks[factor])
        clean_observations.append(
            {
                "factor_risk": clean_risks,
                "observation": observation,
                "units": clean_units,
            }
        )

    return {
        "assets": clean_assets,
        "capital": capital,
        "case_id": case_id,
        "factor_bounds": clean_bounds,
        "factors": list(factors),
        "observations": clean_observations,
        "schema_version": 1,
        "unit": "currency",
    }


def solve_linear(matrix: list[list[float]], target: list[float]) -> list[float]:
    size = len(target)
    work = [list(map(float, matrix[row])) + [float(target[row])] for row in range(size)]
    scale = max(1.0, max(abs(value) for row in matrix for value in row))
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(work[row][column]))
        if abs(work[pivot][column]) <= EPSILON * scale:
            raise Refusal("observation matrix is rank deficient")
        if pivot != column:
            work[column], work[pivot] = work[pivot], work[column]
        divisor = work[column][column]
        for cell in range(column, size + 1):
            work[column][cell] /= divisor
        for row in range(size):
            if row == column:
                continue
            factor = work[row][column]
            if abs(factor) <= EPSILON:
                continue
            for cell in range(column, size + 1):
                work[row][cell] -= factor * work[column][cell]
    return [work[row][size] for row in range(size)]


def recover_loadings(case: dict[str, Any]) -> dict[str, dict[str, float]]:
    asset_ids = [item["id"] for item in case["assets"]]
    width = len(asset_ids)
    rows = [[float(item["units"][asset]) for asset in asset_ids] for item in case["observations"]]
    normal = [[0.0 for _ in range(width)] for _ in range(width)]
    right: dict[str, list[float]] = {
        factor: [0.0 for _ in range(width)] for factor in case["factors"]
    }
    for row, observation in zip(rows, case["observations"]):
        for left in range(width):
            for other in range(width):
                normal[left][other] += row[left] * row[other]
            for factor in case["factors"]:
                right[factor][left] += row[left] * observation["factor_risk"][factor]
    factor_values: dict[str, list[float]] = {}
    for factor in case["factors"]:
        values = solve_linear(normal, right[factor])
        if any(not math.isfinite(value) for value in values):
            raise Refusal("recovered loading is not finite")
        factor_values[factor] = values
    loadings = {
        asset_id: {
            factor: factor_values[factor][index] for factor in case["factors"]
        }
        for index, asset_id in enumerate(asset_ids)
    }
    if any(
        max(abs(value) for value in loadings[asset_id].values()) <= 1e-7
        for asset_id in asset_ids
    ):
        raise Refusal("an asset has no observable factor loading")
    return loadings


def exposure_of(
    vector: list[int],
    case: dict[str, Any],
    loadings: dict[str, dict[str, float]],
) -> dict[str, float]:
    asset_ids = [item["id"] for item in case["assets"]]
    return {
        factor: math.fsum(
            vector[index] * loadings[asset_id][factor]
            for index, asset_id in enumerate(asset_ids)
        )
        for factor in case["factors"]
    }


def optimize(
    case: dict[str, Any],
    loadings: dict[str, dict[str, float]],
) -> tuple[list[int], float, float, dict[str, float], int]:
    assets = case["assets"]
    maximums = [item["max_units"] for item in assets]
    prices = [item["price"] for item in assets]
    returns = [item["return"] for item in assets]
    asset_ids = [item["id"] for item in assets]
    suffix_return = [0.0 for _ in range(len(assets) + 1)]
    for index in range(len(assets) - 1, -1, -1):
        suffix_return[index] = suffix_return[index + 1] + maximums[index] * returns[index]

    best_vector: list[int] | None = None
    best_objective = -math.inf
    best_spend = 0.0
    best_exposure: dict[str, float] = {}
    nodes = 0

    def visit(position: int, vector: list[int], spend: float, objective: float) -> None:
        nonlocal best_vector, best_objective, best_spend, best_exposure, nodes
        nodes += 1
        if objective + suffix_return[position] < best_objective - EPSILON:
            return
        if spend > case["capital"] + EPSILON:
            return
        if position == len(assets):
            exposure = exposure_of(vector, case, loadings)
            if any(
                exposure[factor] < case["factor_bounds"][factor][0] - EPSILON
                or exposure[factor] > case["factor_bounds"][factor][1] + EPSILON
                for factor in case["factors"]
            ):
                return
            candidate = list(vector)
            if objective > best_objective + EPSILON or (
                abs(objective - best_objective) <= EPSILON
                and (best_vector is None or candidate < best_vector)
            ):
                best_vector = candidate
                best_objective = objective
                best_spend = spend
                best_exposure = exposure
            return
        for units in range(maximums[position], -1, -1):
            next_spend = spend + units * prices[position]
            if next_spend > case["capital"] + EPSILON:
                continue
            vector.append(units)
            visit(position + 1, vector, next_spend, objective + units * returns[position])
            vector.pop()

    visit(0, [], 0.0, 0.0)
    if best_vector is None:
        raise Refusal("no portfolio satisfies capital and factor bounds")
    return best_vector, best_objective, best_spend, best_exposure, nodes


def compact_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def build_report(case: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    loadings = recover_loadings(case)
    vector, objective, spend, exposure, nodes = optimize(case, loadings)
    asset_ids = [item["id"] for item in case["assets"]]
    base = {
        "allocations": {name: vector[index] for index, name in enumerate(asset_ids)},
        "case_id": case["case_id"],
        "exposure": exposure,
        "factor_loadings": loadings,
        "objective": objective,
        "schema_version": 1,
        "solver_nodes": nodes,
        "spend": spend,
    }
    checksum = hashlib.sha256(compact_bytes(base)).hexdigest()
    core = {**base, "checksum": checksum}
    digest = hashlib.sha256(compact_bytes(core)).hexdigest()
    entry = {
        "case_id": case["case_id"],
        "input_sha256": hashlib.sha256(compact_bytes(case)).hexdigest(),
        "report_sha256": digest,
        "status": "completed",
    }
    result = {**core, "journal_entry": entry}
    return result, compact_bytes(result)


def read_journal(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line:
            continue
        try:
            value = json.loads(line, object_pairs_hook=reject_duplicates)
        except (json.JSONDecodeError, Refusal) as exc:
            raise Refusal(f"journal line {number} is invalid: {exc}") from exc
        if not isinstance(value, dict):
            raise Refusal("journal entries must be objects")
        records.append(value)
    return records


def stage(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        Path(name).unlink(missing_ok=True)
        raise
    return Path(name)


def publish(report_path: Path, journal_path: Path, result: dict[str, Any], payload: bytes) -> None:
    lock_path = journal_path.with_name(journal_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        records = read_journal(journal_path)
        existing = next(
            (
                item
                for item in records
                if item.get("case_id") == result["journal_entry"]["case_id"]
                and item.get("input_sha256") == result["journal_entry"]["input_sha256"]
                and item.get("report_sha256") == result["journal_entry"]["report_sha256"]
                and item.get("status") == "completed"
            ),
            None,
        )
        report_stage = stage(report_path, payload)
        journal_stage: Path | None = None
        try:
            if existing is None:
                combined = b"".join(compact_bytes(item) for item in [*records, result["journal_entry"]])
                journal_stage = stage(journal_path, combined)
                os.replace(journal_stage, journal_path)
                journal_stage = None
            os.replace(report_stage, report_path)
            report_stage = None
        finally:
            if report_stage is not None:
                report_stage.unlink(missing_ok=True)
            if journal_stage is not None:
                journal_stage.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario")
    parser.add_argument("--journal")
    parser.add_argument("--report")
    parser.add_argument("--inspect", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.inspect:
        return 0
    if not args.scenario or not args.journal or not args.report:
        print(REFUSED + "scenario journal and report are required", file=sys.stderr)
        return 3
    try:
        case = validate(read_json(Path(args.scenario)))
        result, payload = build_report(case)
        publish(Path(args.report), Path(args.journal), result, payload)
        return 0
    except (Refusal, OSError, ValueError, TypeError, KeyError) as exc:
        print(REFUSED + str(exc), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
