"""Mutation testing of changed lines: which edits, which tests, and the result."""

import json
import subprocess
from pathlib import Path

from confidence_agent import cli
from confidence_agent.mutate import Mutant, apply, choose, mutants_for_line, run_mutations, telemetry_lines
from confidence_agent.scoring import score
from confidence_agent.signals import Signals

SOURCE = """\
def is_adult(age):
    return age >= 18


def describe(items, flag=True):
    for x in items:
        span.set_attribute("n", 1)
    return len(items) + 1 if flag and items else 0
"""


def test_mutants_cover_comparisons_numbers_booleans_and_arithmetic():
    found = {(m.original, m.replacement) for m in mutants_for_line(SOURCE, "a.py", 2)}
    assert found == {(">=", ">"), ("18", "19")}
    found = {(m.original, m.replacement) for m in mutants_for_line(SOURCE, "a.py", 8)}
    assert {("+", "-"), ("1", "2"), ("and", "or"), ("0", "1")} <= found


def test_mutants_that_dont_compile_are_dropped():
    # "for x in items" -> "for x not in items" is a syntax error.
    assert mutants_for_line(SOURCE, "a.py", 6) == []


def test_telemetry_only_statements_are_skipped():
    assert telemetry_lines(SOURCE) == {7}


def test_apply_changes_exactly_one_token():
    mutant = Mutant("a.py", 2, 15, ">=", ">")
    assert apply(SOURCE, mutant).splitlines()[1] == "    return age > 18"


def test_budget_is_spread_across_lines():
    a = [Mutant("a.py", 1, 0, "<", "<=")] * 3
    b = [Mutant("a.py", 2, 0, "+", "-")] * 3
    chosen = choose({("a.py", 1): a, ("a.py", 2): b}, 3)
    assert [m.line for m in chosen] == [1, 2, 1]


def write(root: Path, files: dict) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def test_run_mutations_kills_with_a_boundary_test_and_survives_without(tmp_path):
    app = "def is_adult(age):\n    return age >= 18\n"
    project = write(tmp_path / "p", {
        "app.py": app,
        "tests/test_weak.py": "from app import is_adult\n\ndef test_weak():\n    assert is_adult(30)\n",
        "tests/test_edge.py": "from app import is_adult\n\ndef test_edge():\n    assert is_adult(18)\n",
    })
    weak = {"app.py": {"2": ["tests/test_weak.py::test_weak"]}}
    result = run_mutations(project, {"app.py": [2]}, weak, [str(project)])
    assert (result.killed, len(result.survived)) == (0, 2)
    assert "app.py:2: `>=` → `>`" in result.survived
    assert (project / "app.py").read_text() == app  # restored

    both = {"app.py": {"2": ["tests/test_weak.py::test_weak", "tests/test_edge.py::test_edge"]}}
    result = run_mutations(project, {"app.py": [2]}, both, [str(project)])
    assert (result.killed, result.survived) == (2, [])


def test_lines_without_a_covering_test_are_not_mutated(tmp_path):
    project = write(tmp_path / "p", {"app.py": "X = 1 + 1\n"})
    result = run_mutations(project, {"app.py": [1]}, {"app.py": {"1": [""]}}, [str(project)])
    assert result.tried == 0


def test_scoring_is_proportional_to_survivors():
    s = Signals(tests_total=1, mutants_tested=10, mutants_survived=["m"] * 4)
    assert [d for d in score(s).deductions if d[0] == "Mutation"][0][1] == 6  # 40% of 15
    s = Signals(tests_total=1, mutants_tested=5)
    assert "Tests caught all 5 small changes made to the changed lines" in score(s).passed_checks


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


def test_end_to_end_weak_test_is_caught_and_can_be_turned_off(tmp_path):
    repo = write(tmp_path / "repo", {
        "app.py": "def f():\n    return 1\n",
        "tests/test_app.py": "from app import f\n\ndef test_f():\n    assert f() == 1\n",
    })
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    git(repo, "checkout", "-qb", "change")
    write(repo, {
        "app.py": "def f():\n    return 1\n\n\ndef is_adult(age):\n    return age >= 18\n",
        "tests/test_app.py": "from app import f, is_adult\n\ndef test_f():\n    assert f() == 1\n\n"
                             "def test_adult():\n    assert is_adult(40)\n",
    })
    git(repo, "commit", "-qam", "change")
    base_args = ["--repo", str(repo), "--base", "main", "--head", "change", "--source", "app",
                 "--no-llm", "--no-memory"]
    cli.main(base_args + ["--out", str(tmp_path / "on")])
    on = json.loads((tmp_path / "on" / "result.json").read_text())
    assert on["signals"]["mutants_tested"] == 2
    assert len(on["signals"]["mutants_survived"]) == 2
    assert any(d["signal"] == "Mutation" and d["points"] == 15 for d in on["deductions"])

    cli.main(base_args + ["--no-mutation", "--out", str(tmp_path / "off")])
    off = json.loads((tmp_path / "off" / "result.json").read_text())
    assert off["signals"]["mutants_tested"] == 0
