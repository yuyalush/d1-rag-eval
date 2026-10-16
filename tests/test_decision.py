import pytest

from src.decision import ChoiceAnswer, DecisionError, NoulAnswer, ScoreAnswer, choice, noul, parse_response, score

# T04 で取得した実際のレスポンス（数値は一部省略）
RESPONSE = {
    "model": "microsoft-decision-1",
    "answers": {
        "relevant": {"noul": 0.0086, "type": "noul"},
        "sufficiency": {
            "choice": "sufficient",
            "confidence": 0.98,
            "probabilities": {"cannot_tell": 0.001, "insufficient": 0.001, "partial": 0.012, "sufficient": 0.985},
            "type": "choice",
        },
        "usefulness": {
            "confidence": 0.736,
            "legend": {"0": "Irrelevant", "1": "Related topic only", "2": "Partially answers", "3": "Fully answers"},
            "probabilities": {"0": 0.802, "1": 0.179, "2": 0.013, "3": 0.006},
            "score": 0.223,
            "type": "score",
        },
    },
    "usage": {"input_tokens": 85, "output_tokens": 2},
}


def test_parse_response_into_typed_answers():
    r = parse_response(RESPONSE, latency_ms=12.3)
    assert r.input_tokens == 85
    assert r.latency_ms == 12.3
    assert r.answers["relevant"] == NoulAnswer(0.0086)
    suff = r.answers["sufficiency"]
    assert isinstance(suff, ChoiceAnswer) and suff.choice == "sufficient"
    assert max(suff.probabilities, key=suff.probabilities.get) == "sufficient"
    use = r.answers["usefulness"]
    assert isinstance(use, ScoreAnswer)
    assert use.probabilities[0] == 0.802 and use.legend[3] == "Fully answers"


def test_missing_answers_raises():
    with pytest.raises(DecisionError):
        parse_response({"model": "x"})


def test_question_builders():
    assert noul("Q?") == {"type": "noul", "instructions": "Q?"}
    assert noul("Q?", "yes", "no")["criteria"] == {"true": "yes", "false": "no"}
    assert choice("Q?", {"a": "A"})["criteria"] == {"a": "A"}
    assert score("Q?", ["low", "high"])["criteria"] == ["low", "high"]
