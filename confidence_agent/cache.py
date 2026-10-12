"""Reuse the base run's results across pull requests.

Every pull request against main re-measures main. The base run depends only on
the base commit, how it's run, the Python environment and the agent itself, so
its results are saved under a key built from exactly those.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import platform
import sys
from importlib import metadata
from pathlib import Path
from typing import Optional

from .runner import RunResult

KEEP = 50  # most recent cache entries to keep


def default_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "confidence-agent"


def _environment() -> str:
    """Python version, platform, installed packages and the agent's own source."""
    h = hashlib.sha256(f"{sys.version}|{sys.platform}|{platform.machine()}".encode())
    for dist in sorted(f"{d.metadata['Name']}=={d.version}" for d in metadata.distributions()
                       if d.metadata["Name"]):
        h.update(dist.encode())
    for path in sorted(Path(__file__).parent.glob("*.py")):
        h.update(path.name.encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def key(commit: str, source: str, tests: str, track_memory: bool) -> str:
    parts = json.dumps([commit, source, tests, track_memory, _environment()])
    return hashlib.sha256(parts.encode()).hexdigest()[:32]


def load(cache_dir: Path, cache_key: str) -> Optional[RunResult]:
    path = cache_dir / f"{cache_key}.json"
    try:
        data = json.loads(path.read_text())
        fields = {f.name for f in dataclasses.fields(RunResult)}
        result = RunResult(**{k: v for k, v in data.items() if k in fields})
        result.from_cache = True
    except (OSError, ValueError, TypeError):
        return None
    os.utime(path)  # mark as recently used
    return result


def save(cache_dir: Path, cache_key: str, result: RunResult) -> None:
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        data = dataclasses.asdict(result)
        data["mutation"] = None  # only head runs are mutation-tested
        tmp = cache_dir / f"{cache_key}.json.tmp"
        tmp.write_text(json.dumps(data))
        tmp.replace(cache_dir / f"{cache_key}.json")
        entries = sorted(cache_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in entries[KEEP:]:
            old.unlink(missing_ok=True)
    except OSError:
        pass  # caching is an optimization; never fail the run over it
