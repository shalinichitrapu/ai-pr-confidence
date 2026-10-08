from studybuddy.quiz import grade_quiz


def test_all_correct():
    result = grade_quiz({"q1": "paris", "q2": "4"}, {"q1": "paris", "q2": "4"})
    assert result.correct == 2
    assert result.percent == 100.0
    assert result.missed == []


def test_skipped_question_counts_as_missed():
    result = grade_quiz({"q1": "paris"}, {"q1": "paris", "q2": "4"})
    assert result.correct == 1
    assert result.missed == ["q2"]
    assert result.percent == 50.0


def test_numeric_answers():
    result = grade_quiz({"q1": 42, "q2": 7}, {"q1": 42, "q2": 8})
    assert result.correct == 1
    assert result.missed == ["q2"]


def test_unknown_questions_are_ignored():
    result = grade_quiz({"q1": "a", "zz": "b"}, {"q1": "a"})
    assert result.correct == 1
    assert result.total == 1


def test_empty_quiz():
    assert grade_quiz({}, {}).percent == 0.0
