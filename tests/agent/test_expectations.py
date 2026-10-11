from confidence_agent.expectations import expects_exception, node_file

SOURCE = '''
import pytest
import unittest

def test_raises():
    with pytest.raises(ValueError):
        f()

def test_plain():
    assert f() == 1

class TestThing(unittest.TestCase):
    def test_assert_raises(self):
        with self.assertRaises(KeyError):
            g()

    def test_plain(self):
        self.assertEqual(g(), 2)

@pytest.mark.parametrize("x", [1, 2])
def test_param(x):
    pytest.raises(TypeError, h, x)
'''


def test_detects_pytest_and_unittest_styles():
    assert expects_exception(SOURCE, "tests/test_a.py::test_raises")
    assert expects_exception(SOURCE, "tests/test_a.py::TestThing::test_assert_raises")
    assert expects_exception(SOURCE, "tests/test_a.py::test_param[1]")


def test_plain_tests_dont_expect_exceptions():
    assert not expects_exception(SOURCE, "tests/test_a.py::test_plain")
    assert not expects_exception(SOURCE, "tests/test_a.py::TestThing::test_plain")


def test_unknown_or_missing_source():
    assert not expects_exception(SOURCE, "tests/test_a.py::test_missing")
    assert not expects_exception(None, "tests/test_a.py::test_raises")
    assert not expects_exception("def broken(:", "tests/test_a.py::test_raises")


def test_node_file():
    assert node_file("tests/test_a.py::TestThing::test_x[1]") == "tests/test_a.py"
