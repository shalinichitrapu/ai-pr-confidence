"""--select-tests runs only the tests a change can affect, or everything when unsure."""

import json
import subprocess

from confidence_agent import cli
from confidence_agent.choose_tests import Selection, junit_id, plan, restrict
from confidence_agent.gitutil import Diff
from confidence_agent.runner import RunResult

APP = """\
LIMIT = 10


def f():
    return 1


class Box:
    size = 3

    def g(self):
        return 2
"""

T_F, T_G = "tests/test_f.py::test_f", "tests/test_g.py::test_g"


def base():
    contexts = {"5": [T_F], "12": [T_G], "1": [""]}
    functions = {"f": {"executed_lines": [5]}, "Box.g": {"executed_lines": [12]}, "": {"executed_lines": [1]}}
    cov = {"executed_lines": [1, 5, 12], "contexts": contexts, "functions": functions}
    return RunResult("base", tests={"tests.test_f::test_f": "passed", "tests.test_g::test_g": "passed"},
                     node_ids=[T_F, T_G], coverage={"app.py": cov},
                     logs=[{"test": T_F}, {"test": T_G}, {"test": None}], memory={T_F: 1, T_G: 2})


def diff(changed, files=None, removed=None):
    files = files or list(changed)
    return Diff(added_lines=changed, files_changed=files, removed_text=removed or {},
                head_source={p: APP for p in files if p.endswith(".py")})


def test_function_body_change_selects_the_tests_that_ran_that_function():
    selection, why = plan(diff({"app.py": {5}}), base(), "tests", "app")
    assert (selection.node_ids, selection.test_files, why) == ([T_F], [], "")
    selection, _ = plan(diff({"app.py": {12}}), base(), "tests", "app")
    assert selection.node_ids == [T_G]  # a method, matched as "Box.g"


def test_new_function_and_blank_lines_dont_force_a_full_run():
    source = APP + "\n\n@cache\ndef brand_new():\n    return f()\n"
    d = Diff(added_lines={"app.py": {14, 15, 16, 17, 18}}, files_changed=["app.py"], head_source={"app.py": source})
    selection, why = plan(d, base(), "tests", "app")
    # Not "module-level code": the decorator, def and blank lines belong to the new function.
    # Nothing in the base ran it and no test file changed, so there's nothing to select.
    assert selection is None and why == "nothing to select"
    assert plan(Diff(added_lines={"app.py": {14}}, files_changed=["app.py", "tests/test_new.py"],
                     head_source={"app.py": source, "tests/test_new.py": ""}), base(), "tests", "app")[0] is not None


def test_changed_import_selects_every_test_that_ran_the_file():
    source = "import os\n" + APP
    d = Diff(added_lines={"app.py": {1}}, files_changed=["app.py"], head_source={"app.py": source})
    assert plan(d, base(), "tests", "app")[0].node_ids == [T_F, T_G]


def test_a_new_file_needs_no_existing_tests():
    d = Diff(added_lines={"app.py": {5}, "newmod.py": {1, 2, 3}}, files_changed=["app.py", "newmod.py"],
             head_source={"app.py": APP, "newmod.py": "X = 1\n\ny = X + 1\n"})
    selection, why = plan(d, base(), "tests", "app,newmod")
    assert why == "" and selection.node_ids == [T_F]


def test_removed_definition_selects_every_test_that_ran_the_file():
    selection, _ = plan(diff({"app.py": {5}}, removed={"app.py": ["def old_helper():"]}), base(), "tests", "app")
    assert selection.node_ids == [T_F, T_G]


def test_changed_test_files_run_whole():
    selection, _ = plan(diff({"tests/test_new.py": {1}}), base(), "tests", "app")
    assert selection.test_files == ["tests/test_new.py"] and selection.node_ids == []
    assert Selection([T_F, "tests/test_new.py::x"], ["tests/test_new.py"]).targets() == ["tests/test_new.py", T_F]


def test_unsafe_changes_fall_back_to_every_test():
    cases = {
        "module level": diff({"app.py": {1}}),
        "class level": diff({"app.py": {9}}),
        "conftest": diff({"tests/conftest.py": {1}}),
        "config file": diff({}, files=["settings.yaml"]),
        "outside source": diff({"scripts/tool.py": {1}}),
        "deleted": Diff(files_changed=["app.py"]),
        "nothing selected": diff({}, files=["README.md"]),
    }
    for name, d in cases.items():
        selection, why = plan(d, base(), "tests", "app")
        assert selection is None and why, name


def test_base_without_per_test_coverage_falls_back():
    run = base()
    run.coverage["app.py"].pop("contexts")
    assert plan(diff({"app.py": {5}}), run, "tests", "app")[0] is None


def test_junit_ids():
    assert junit_id("tests/test_a.py::test_x") == "tests.test_a::test_x"
    assert junit_id("tests/test_a.py::TestX::test_y[1]") == "tests.test_a.TestX::test_y[1]"


def test_restrict_keeps_only_selected_tests_and_unattributed_records():
    narrowed = restrict(base(), Selection([T_G], []))
    assert narrowed.tests == {"tests.test_g::test_g": "passed"}
    assert narrowed.node_ids == [T_G]
    assert narrowed.logs == [{"test": T_G}, {"test": None}]
    assert narrowed.memory == {T_G: 2}


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


def test_end_to_end(tmp_path):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "app.py").write_text("LIMIT = 1\n\n\ndef f():\n    return 1\n\n\ndef g():\n    return 2\n")
    (repo / "tests" / "test_f.py").write_text("from app import f\n\ndef test_f():\n    assert f() == 1\n")
    (repo / "tests" / "test_g.py").write_text("from app import g\n\ndef test_g():\n    assert g() == 2\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    git(repo, "checkout", "-qb", "body")
    (repo / "app.py").write_text("LIMIT = 1\n\n\ndef f():\n    return 1\n\n\ndef g():\n    return 1 + 1\n")
    git(repo, "commit", "-qam", "body change")
    git(repo, "checkout", "-qb", "module", "main")
    (repo / "app.py").write_text("LIMIT = 2\n\n\ndef f():\n    return 1\n\n\ndef g():\n    return 2\n")
    git(repo, "commit", "-qam", "module change")

    def run(head):
        out = tmp_path / head
        cli.main(["--repo", str(repo), "--base", "main", "--head", head, "--source", "app", "--no-llm",
                  "--no-memory", "--no-mutation", "--select-tests", "--out", str(out)])
        return json.loads((out / "result.json").read_text()), (out / "report.md").read_text()

    body, report = run("body")
    assert body["signals"]["tests_total"] == 1  # only test_g ran
    assert body["signals"]["tests_removed"] == []
    assert body["score"] == 100
    assert "Ran only the tests the changed code can affect: 1 existing test" in report

    module, report = run("module")
    assert module["signals"]["tests_total"] == 2
    assert "Ran every test" in report and "app.py:1" in report
