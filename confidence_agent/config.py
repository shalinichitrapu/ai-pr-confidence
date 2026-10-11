"""Per-project settings from the [tool.confidence] table in pyproject.toml.

The agent reads this from the *base* commit, never from the pull request, so a
change can't lower its own thresholds or mark its own errors as expected.

    [tool.confidence]
    source = "my_package"               # default for --source
    tests = "tests"                     # default for --tests
    min_score = 60                      # default for --min-score
    ignore = ["*/migrations/*", "*_pb2.py"]          # changed files never scored
    expected_errors = ["payments.py:refund: TimeoutError"]  # error signatures not scored

    [tool.confidence.bands]
    high = 85
    medium = 60

    [tool.confidence.thresholds]
    large_diff_lines = 400
    latency_ratio = 2.0

    [tool.confidence.weights]
    error_logs = [20, 5, 25]            # first, each additional, maximum
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field, fields
from fnmatch import fnmatch
from typing import Any, Dict, List, Optional

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on Python < 3.11
    import tomli as tomllib


class ConfigError(ValueError):
    """The [tool.confidence] table has a mistake; the message says where."""


@dataclass(frozen=True)
class Tier:
    """Points for the first finding, each additional one, and the most in total."""

    first: int
    each: int
    cap: int

    def points(self, count: int) -> int:
        if count <= 0:
            return 0
        return min(self.cap, self.first + self.each * (count - 1))


@dataclass
class Weights:
    tests_failing: Tier = Tier(45, 5, 55)
    tests_flaky: Tier = Tier(5, 0, 5)
    no_tests: Tier = Tier(50, 0, 50)
    tests_removed: Tier = Tier(15, 0, 15)
    coverage: Tier = Tier(30, 0, 30)  # proportional: cap is what 0% coverage costs
    error_logs: Tier = Tier(20, 5, 25)
    span_exceptions: Tier = Tier(10, 5, 15)
    latency: Tier = Tier(10, 10, 20)
    visibility: Tier = Tier(10, 10, 20)
    metrics: Tier = Tier(10, 5, 20)
    memory: Tier = Tier(10, 10, 20)
    size: Tier = Tier(5, 0, 5)


@dataclass
class Thresholds:
    large_diff_lines: int = 400
    latency_ratio: float = 2.0
    latency_min_delta_ms: float = 5.0
    metric_ratio: float = 2.0
    metric_min_delta: float = 3.0
    memory_ratio: float = 2.0
    memory_min_delta_mb: float = 5.0


@dataclass
class Bands:
    high: int = 85
    medium: int = 60


@dataclass
class Config:
    source: Optional[str] = None
    tests: Optional[str] = None
    min_score: Optional[int] = None
    ignore: List[str] = field(default_factory=list)
    expected_errors: List[str] = field(default_factory=list)
    bands: Bands = field(default_factory=Bands)
    thresholds: Thresholds = field(default_factory=Thresholds)
    weights: Weights = field(default_factory=Weights)

    def ignored(self, path: str) -> bool:
        return any(fnmatch(path, pattern) for pattern in self.ignore)

    def expected(self, signature: str) -> bool:
        return any(text in signature for text in self.expected_errors)


def _check_keys(table: Dict[str, Any], allowed, where: str) -> None:
    unknown = sorted(set(table) - set(allowed))
    if unknown:
        raise ConfigError(f"unknown key(s) in {where}: {', '.join(unknown)}; allowed: {', '.join(sorted(allowed))}")


def _number(value: Any, where: str, kind=(int, float)) -> Any:
    if isinstance(value, bool) or not isinstance(value, kind):
        raise ConfigError(f"{where} must be a number, got {value!r}")
    return value


def _strings(value: Any, where: str) -> List[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{where} must be a list of strings")
    return value


def parse(text: str) -> Config:
    """Read [tool.confidence] from pyproject.toml text. Missing table means defaults."""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"pyproject.toml is not valid TOML: {exc}") from exc
    table = doc.get("tool", {}).get("confidence", {})
    cfg = Config()
    if not table:
        return cfg
    top = ("source", "tests", "min_score", "ignore", "expected_errors", "bands", "thresholds", "weights")
    _check_keys(table, top, "[tool.confidence]")

    for key in ("source", "tests"):
        if key in table:
            if not isinstance(table[key], str):
                raise ConfigError(f"[tool.confidence] {key} must be a string")
            setattr(cfg, key, table[key])
    if "min_score" in table:
        cfg.min_score = _number(table["min_score"], "[tool.confidence] min_score", int)
    if "ignore" in table:
        cfg.ignore = _strings(table["ignore"], "[tool.confidence] ignore")
    if "expected_errors" in table:
        cfg.expected_errors = _strings(table["expected_errors"], "[tool.confidence] expected_errors")

    for name, target, kind in (("bands", cfg.bands, int), ("thresholds", cfg.thresholds, (int, float))):
        sub = table.get(name, {})
        where = f"[tool.confidence.{name}]"
        _check_keys(sub, [f.name for f in fields(target)], where)
        for key, value in sub.items():
            setattr(target, key, _number(value, f"{where} {key}", kind))
    if not 0 <= cfg.bands.medium <= cfg.bands.high <= 100:
        raise ConfigError("[tool.confidence.bands] needs 0 <= medium <= high <= 100")

    weights = table.get("weights", {})
    _check_keys(weights, [f.name for f in fields(cfg.weights)], "[tool.confidence.weights]")
    for key, value in weights.items():
        where = f"[tool.confidence.weights] {key}"
        if not (isinstance(value, list) and len(value) == 3
                and all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in value)):
            raise ConfigError(f"{where} must be [first, each, maximum] as three whole numbers >= 0")
        setattr(cfg.weights, key, Tier(*value))
    return cfg
