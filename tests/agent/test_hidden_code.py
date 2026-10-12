"""Changes that hide code from coverage or telemetry are flagged."""

import subprocess
from pathlib import Path

from confidence_agent.gitutil import Diff, read_diff
from confidence_agent.runner import RunResult
from confidence_agent.scoring import score
from confidence_agent.signals import compute


def hidden(added=None, removed=None):
    diff = Diff(added_text=added or {}, removed_text=removed or {})
    return compute(RunResult("b"), RunResult("h"), diff, "tests").hidden_code


def test_new_pragma_no_cover_is_flagged():
    found = hidden(added={"pkg/a.py": ["    if x:  # pragma: no cover", "    y = 1"]})
    assert found == {"coverage exclusions added": ["pkg/a.py: if x:  # pragma: no cover"]}


def test_removed_error_logging_tracing_and_instruments_are_flagged():
    found = hidden(removed={"pkg/a.py": [
        '        log.exception("payment failed")',
        '    with tracer.start_as_current_span("pay"):',
        '        span.record_exception(exc)',
        'errors = meter.create_counter("pay.errors")',
    ]})
    assert set(found) == {"error logging removed", "tracing removed", "metric instruments removed"}
    assert len(found["tracing removed"]) == 2


def test_code_moved_between_files_is_not_flagged():
    line = '    logger.error("could not save %s", key)'
    assert hidden(removed={"pkg/old.py": [line]}, added={"pkg/new.py": [line]}) == {}


def test_only_the_net_change_is_reported():
    found = hidden(removed={"pkg/a.py": ['log.error("a")', 'log.error("b")']},
                   added={"pkg/a.py": ['log.error("c")']})
    assert len(found["error logging removed"]) == 1


def test_test_files_and_non_python_files_are_ignored():
    assert hidden(added={"tests/test_a.py": ["x  # pragma: no cover"], "notes.md": ["# pragma: no cover"]},
                  removed={"tests/test_b.py": ['log.error("x")']}) == {}


def test_warnings_and_info_logs_are_not_error_logging():
    assert hidden(removed={"pkg/a.py": ['log.warning("x")', 'log.info("y")']}) == {}


def test_scoring_deducts_20_then_5_per_kind_up_to_30():
    def visibility(kinds):
        s = compute(RunResult("b"), RunResult("h"), Diff(), "tests")
        s.hidden_code = {k: ["x"] for k in kinds}
        return sum(p for sig, p, _ in score(s).deductions if sig == "Visibility")
    assert visibility(["a"]) == 20
    assert visibility(["a", "b", "c"]) == 30


def git(repo: Path, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_read_diff_collects_added_and_removed_text(tmp_path):
    repo = tmp_path
    git(repo, "init", "-q")
    (repo / "a.py").write_text('def f():\n    log.error("boom")\n    return 1\n')
    (repo / "notes.sql").write_text("-- old comment\nselect 1;\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    (repo / "a.py").write_text("def f():\n    return 1  # pragma: no cover\n")
    (repo / "notes.sql").write_text("select 1;\n")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "head")
    diff = read_diff(repo, "HEAD~1", "HEAD")
    assert diff.removed_text["a.py"] == ['    log.error("boom")', "    return 1"]
    assert diff.added_text["a.py"] == ["    return 1  # pragma: no cover"]
    # A removed line that starts with "--" is content, not a file header.
    assert diff.removed_text["notes.sql"] == ["-- old comment"]
    assert diff.added_lines["a.py"] == {2}


def test_lines_excluded_by_a_new_pragma_still_count_as_untested():
    cov = {"pkg/a.py": {"executed_lines": [1], "missing_lines": [], "excluded_lines": [2, 3]}}
    diff = Diff(added_lines={"pkg/a.py": {2, 3}},
                added_text={"pkg/a.py": ["if huge:  # pragma: no cover", "    trim()"]})
    s = compute(RunResult("b"), RunResult("h", coverage=cov), diff, "tests")
    assert s.changed_code_lines == 2
    assert s.uncovered_changes == {"pkg/a.py": [2, 3]}


def test_existing_exclusions_are_left_alone():
    # Lines excluded by project config (no pragma added by this change) aren't counted.
    cov = {"pkg/a.py": {"executed_lines": [1], "missing_lines": [], "excluded_lines": [2]}}
    diff = Diff(added_lines={"pkg/a.py": {2}}, added_text={"pkg/a.py": ["    raise NotImplementedError"]})
    s = compute(RunResult("b"), RunResult("h", coverage=cov), diff, "tests")
    assert s.changed_code_lines == 0


def test_removed_lines_in_files_with_spaces_or_non_ascii_names(tmp_path):
    repo = tmp_path
    git(repo, "init", "-q")
    (repo / "my file.py").write_text('log.error("a")\nx = 1\n')
    (repo / "café.py").write_text('log.error("b")\ny = 1\n')
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    (repo / "my file.py").write_text("x = 1\n")
    (repo / "café.py").write_text("y = 1\n")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "head")
    diff = read_diff(repo, "HEAD~1", "HEAD")
    assert diff.removed_text == {"my file.py": ['log.error("a")'], "café.py": ['log.error("b")']}
