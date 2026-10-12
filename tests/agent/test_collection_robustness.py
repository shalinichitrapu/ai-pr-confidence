"""Evidence collection holds up against real projects' own setup (full-repo review)."""

from pathlib import Path

from confidence_agent.mutate import mutants_for_line, run_mutations
from confidence_agent.runner import junit_id, run_tests

APP = "def f():\n    return 1\n"
TEST = "from pkg import f\n\ndef test_f():\n    assert f() == 1\n"


def project(root: Path, files: dict) -> Path:
    files = {"pkg/__init__.py": APP, "tests/test_f.py": TEST, **files}
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text) if isinstance(text, bytes) else path.write_text(text)
    return root


def test_ci_tokens_dont_reach_the_projects_tests(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghs_secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_secret")
    monkeypatch.setenv("DATABASE_PASSWORD", "kept")  # other variables are left alone
    p = project(tmp_path / "p", {"tests/test_env.py": (
        "import os\n\ndef test_env():\n    assert 'GH_TOKEN' not in os.environ\n"
        "    assert 'GITHUB_TOKEN' not in os.environ\n    assert os.environ['DATABASE_PASSWORD'] == 'kept'\n")})
    r = run_tests(p, "head", "pkg", "tests", track_memory=False)
    assert set(r.tests.values()) == {"passed"}, r.output_tail


def test_the_projects_coverage_config_cant_hide_code(tmp_path):
    p = project(tmp_path / "p", {"pyproject.toml": '[tool.coverage.run]\nomit = ["pkg/*"]\nparallel = true\n'
                                                   'dynamic_context = "test_function"\n'})
    r = run_tests(p, "head", "pkg", "tests", track_memory=False)
    assert "pkg/__init__.py" in r.coverage
    contexts = r.coverage["pkg/__init__.py"]["contexts"]
    assert contexts["2"] == ["tests/test_f.py::test_f"]


def test_common_pytest_settings_dont_break_the_run(tmp_path, monkeypatch):
    p = project(tmp_path / "p", {"pyproject.toml": '[tool.pytest.ini_options]\naddopts = "--lf --ff"\n'})
    assert run_tests(p, "head", "pkg", "tests", track_memory=False).tests == {"tests.test_f::test_f": "passed"}
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    q = project(tmp_path / "q", {})
    assert run_tests(q, "head", "pkg", "tests", track_memory=False).tests == {"tests.test_f::test_f": "passed"}


def test_one_collection_error_doesnt_discard_the_run(tmp_path):
    p = project(tmp_path / "p", {"tests/test_broken.py": "import does_not_exist\n"})
    r = run_tests(p, "head", "pkg", "tests", track_memory=False)
    assert r.tests.get("tests.test_f::test_f") == "passed"


def test_non_utf8_test_output_doesnt_crash(tmp_path):
    p = project(tmp_path / "p", {"tests/test_bytes.py": (
        "import sys\n\ndef test_bytes():\n    sys.stdout.buffer.write(b'\\xff\\xfe')\n    assert False\n")})
    r = run_tests(p, "head", "pkg", "tests", track_memory=False)
    assert r.tests["tests.test_bytes::test_bytes"] == "failed"


def test_project_that_sets_up_opentelemetry_itself_still_gets_spans(tmp_path):
    p = project(tmp_path / "p", {
        "conftest.py": ("from opentelemetry import trace\nfrom opentelemetry.sdk.trace import TracerProvider\n"
                        "trace.set_tracer_provider(TracerProvider())\n"),
        "pkg/__init__.py": ("from opentelemetry import trace\ntracer = trace.get_tracer('pkg')\n\n"
                            "def f():\n    with tracer.start_as_current_span('pkg.f'):\n        return 1\n"),
    })
    r = run_tests(p, "head", "pkg", "tests", track_memory=False)
    assert r.otel_enabled
    assert [(s["name"], s["test"]) for s in r.spans] == [("pkg.f", "tests/test_f.py::test_f")]


def test_logs_survive_the_projects_logging_config(tmp_path):
    p = project(tmp_path / "p", {
        "conftest.py": ("import logging.config, pytest\n\n@pytest.fixture(autouse=True, scope='session')\n"
                        "def cfg():\n    logging.config.dictConfig({'version': 1, 'root': {'handlers': []},\n"
                        "        'loggers': {'quiet': {'propagate': False, 'handlers': []}}})\n"),
        "pkg/__init__.py": ("import logging\n\ndef f():\n    logging.getLogger('quiet').error('hidden %s', 1)\n"
                            "    return 1\n"),
    })
    r = run_tests(p, "head", "pkg", "tests", track_memory=False)
    errors = [(x["message"], x["test"], x["location"]) for x in r.logs if x["level"] == "ERROR"]
    assert errors == [("hidden 1", "tests/test_f.py::test_f", "pkg/__init__.py:f")]


def test_teardown_logs_belong_to_their_test(tmp_path):
    p = project(tmp_path / "p", {"tests/test_f.py": (
        "import logging, pytest\nfrom pkg import f\n\n@pytest.fixture\ndef res():\n    yield\n"
        "    logging.getLogger('app').error('cleanup failed')\n\ndef test_f(res):\n    assert f() == 1\n")})
    r = run_tests(p, "head", "pkg", "tests", track_memory=False)
    assert [x["test"] for x in r.logs if x["message"] == "cleanup failed"] == ["tests/test_f.py::test_f"]


def test_observable_counters_are_per_test(tmp_path):
    p = project(tmp_path / "p", {
        "pkg/__init__.py": ("from opentelemetry import metrics\nfrom opentelemetry.metrics import Observation\n"
                            "TOTAL = [0]\n\ndef f(n=1):\n    TOTAL[0] += n\n    return 1\n\n"
                            "metrics.get_meter('pkg').create_observable_counter('pkg.total', "
                            "callbacks=[lambda options: [Observation(TOTAL[0])]])\n"),
        "tests/test_f.py": ("from pkg import f\n\ndef test_a():\n    f(5)\n\ndef test_b():\n    pass\n\n"
                            "def test_c():\n    f(5)\n"),
    })
    r = run_tests(p, "head", "pkg", "tests", track_memory=False)
    values = {m["test"].rsplit("::", 1)[-1]: m["value"] for m in r.metrics if m["name"] == "pkg.total"}
    assert values.get("test_b", 0) == 0 and values["test_c"] == 5


def test_junit_ids_with_double_colons_in_parameters():
    assert junit_id("tests/test_a.py::test_f[a::b]") == "tests.test_a::test_f[a::b]"


# --- mutation testing ------------------------------------------------------------

def test_tests_that_only_pass_after_another_test_dont_catch_mutants(tmp_path):
    p = project(tmp_path / "p", {
        "pkg/__init__.py": "CACHE = {}\n\ndef get(k):\n    return CACHE.get(k, 0) + 1\n",
        "tests/test_f.py": ("from pkg import CACHE, get\n\ndef test_a_setup():\n    CACHE['k'] = 4\n\n"
                            "def test_b_uses():\n    assert get('k') == 5\n"),
    })
    ctx = {"pkg/__init__.py": {"4": ["tests/test_f.py::test_b_uses"]}}
    r = run_mutations(p, {"pkg/__init__.py": [4]}, ctx, [str(p)])
    assert r.killed == 0 and r.skipped == r.tried > 0


def test_a_timeout_from_the_overall_budget_isnt_a_kill(tmp_path):
    p = project(tmp_path / "p", {
        "pkg/__init__.py": "def f(x):\n    return x >= 18\n",
        "tests/test_f.py": "import time\nfrom pkg import f\n\ndef test_slow():\n    time.sleep(2)\n    assert f(40) or True\n",
    })
    ctx = {"pkg/__init__.py": {"2": ["tests/test_f.py::test_slow"]}}
    r = run_mutations(p, {"pkg/__init__.py": [2]}, ctx, [str(p)], total_budget=5.0,
                      durations={"tests/test_f.py::test_slow": 2.0})
    assert r.killed == 0


def test_mutation_restores_files_byte_for_byte(tmp_path):
    original = "# -*- coding: latin-1 -*-\r\nNAME = '\xe9'\r\n\r\ndef f(x):\r\n    return x >= 18\r\n".encode("latin-1")
    p = project(tmp_path / "p", {"pkg/__init__.py": original,
                                 "tests/test_f.py": "from pkg import f\n\ndef test_f():\n    assert f(40)\n"})
    run_mutations(p, {"pkg/__init__.py": [5]}, {"pkg/__init__.py": {"5": ["tests/test_f.py::test_f"]}}, [str(p)])
    assert (p / "pkg/__init__.py").read_bytes() == original


def test_floats_are_mutated_and_annotations_arent():
    src = "def f(a: int = 1, *, b: bool = True) -> float:\n    return a * 1.5\n\nx: Literal[1] = 2\n"
    assert {(m.original, m.replacement) for m in mutants_for_line(src, "m.py", 2)} == {("*", "/"), ("1.5", "2.5")}
    assert {(m.original, m.replacement) for m in mutants_for_line(src, "m.py", 1)} == {("1", "2"), ("True", "False")}
    assert {m.original for m in mutants_for_line(src, "m.py", 4)} == {"2"}


# --- git diffs and the CLI ------------------------------------------------------

import subprocess  # noqa: E402

from confidence_agent import cli  # noqa: E402
from confidence_agent.gitutil import read_diff  # noqa: E402


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


def two_commits(tmp_path, before: dict, after: dict) -> Path:
    repo = tmp_path / "r"
    project(repo, before)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    git(repo, "checkout", "-qb", "change")
    project(repo, after)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "change")
    return repo


def test_diff_parsing_ignores_the_users_git_config_and_odd_files(tmp_path):
    repo = two_commits(
        tmp_path,
        {"pkg/legacy.py": "# -*- coding: latin-1 -*-\nNAME = 'a'\n".encode("latin-1")},
        {"pkg/legacy.py": "# -*- coding: latin-1 -*-\nNAME = '\xe9'\n".encode("latin-1"),
         "pkg/we\"ird.py": "x = 1\n", "pkg/ff.py": "a = 1\n\x0c\nb = 2  # after a form feed\n"},
    )
    git(repo, "config", "diff.noprefix", "true")
    d = read_diff(repo, "main", "change")
    assert d.added_lines["pkg/legacy.py"] == {2}
    assert d.head_source["pkg/legacy.py"].endswith("NAME = '\xe9'\n")
    assert 'pkg/we"ird.py' in d.added_lines
    assert any("after a form feed" in line for line in d.added_text["pkg/ff.py"])


def test_bad_refs_exit_3_not_1(tmp_path, capsys):
    repo = two_commits(tmp_path, {}, {"pkg/b.py": "y = 2\n"})
    assert cli.main(["--repo", str(repo), "--base", "nope", "--head", "change", "--no-llm"]) == cli.COULDNT_RUN
    assert "couldn't resolve" in capsys.readouterr().out


def test_temp_folders_are_cleaned_up(tmp_path, monkeypatch):
    import tempfile
    repo = two_commits(tmp_path, {}, {"pkg/b.py": "y = 2\n"})
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    cli.main(["--repo", str(repo), "--base", "main", "--head", "change", "--source", "pkg", "--no-llm",
              "--no-memory", "--no-mutation", "--no-cache", "--out", str(tmp_path / "out")])
    assert list(scratch.iterdir()) == []


def test_log_capture_doesnt_change_records_and_respects_logger_filters(tmp_path):
    p = project(tmp_path / "p", {
        "pkg/__init__.py": ("import logging\n\ndef f():\n    noisy = logging.getLogger('noisy')\n"
                            "    noisy.addFilter(lambda r: False)\n    noisy.error('dropped')\n"
                            "    logging.getLogger('app').error('kept', extra={'user': 'u'})\n    return 1\n"),
        "tests/test_f.py": ("import logging\nfrom pkg import f\n\nSTD = set(vars(logging.makeLogRecord({})))\n\n"
                            "def test_f(caplog):\n    assert f() == 1\n"
                            "    assert set(vars(caplog.records[-1])) - STD - {'message', 'asctime'} == {'user'}\n"),
    })
    r = run_tests(p, "head", "pkg", "tests", track_memory=False)
    assert r.tests == {"tests.test_f::test_f": "passed"}, r.output_tail
    assert [x["message"] for x in r.logs if x["level"] == "ERROR"] == ["kept"]


def test_shared_fixture_teardown_isnt_credited_to_the_last_test(tmp_path):
    p = project(tmp_path / "p", {"tests/test_f.py": (
        "import logging, pytest\n\n@pytest.fixture(scope='module')\ndef db():\n    yield\n"
        "    logging.getLogger('app').error('module teardown')\n\n"
        "def test_one(db):\n    pass\n\ndef test_two(db):\n    pass\n")})
    r = run_tests(p, "head", "pkg", "tests", track_memory=False)
    assert [x["test"] for x in r.logs if x["message"] == "module teardown"] == [None]


def test_quoted_non_ascii_file_names():
    from confidence_agent.gitutil import _unquote
    assert _unquote('"\\303\\251\\t.py"') == "é\t.py"
    assert _unquote('"é\\"日.py"') == 'é"日.py'


def test_a_crash_exits_3_not_1(monkeypatch, tmp_path):
    repo = two_commits(tmp_path, {}, {"pkg/b.py": "y = 2\n"})
    monkeypatch.setattr(cli.gitutil, "read_diff", lambda *a: 1 / 0)
    assert cli.main(["--repo", str(repo), "--base", "main", "--head", "change", "--no-llm"]) == cli.COULDNT_RUN


def test_a_log_message_that_cant_be_formatted_is_still_captured(tmp_path):
    p = project(tmp_path / "p", {"pkg/__init__.py": (
        "import logging\n\ndef f():\n    logging.getLogger('app').error('total %d', 'oops')\n    return 1\n")})
    r = run_tests(p, "head", "pkg", "tests", track_memory=False)
    [record] = [x for x in r.logs if x["template"] == "total %d"]
    assert "couldn't format" in record["message"] and "TypeError" in record["message"]
