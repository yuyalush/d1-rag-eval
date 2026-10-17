import io
import json
import shutil

import pytest

import scripts.review_cli as rc
import scripts.review_sheet as rs
from src.config import ROOT


@pytest.fixture
def qa_dir(tmp_path, monkeypatch):
    shutil.copy(ROOT / "data/qa/test.jsonl", tmp_path / "test.jsonl")
    shutil.copy(ROOT / "data/qa/test_ai_review.jsonl", tmp_path / "test_ai_review.jsonl")
    monkeypatch.setattr(rc, "QA", tmp_path)
    monkeypatch.setattr(rc, "AI_FILE", tmp_path / "test_ai_review.jsonl")
    monkeypatch.setattr(rc, "HUMAN_FILE", tmp_path / "test_human_review.jsonl")
    monkeypatch.setattr(rs, "QA", tmp_path)
    return tmp_path


def run_session(monkeypatch, lines: list[str]) -> str:
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(lines) + "\n"))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    rc.run("all", None)
    return out.getvalue()


def test_decisions_are_saved_and_resumable(qa_dir, monkeypatch):
    run_session(monkeypatch, ["", "o", "確認済み", "f", "新しい質問", "", "", "", "q"])
    store = rc.Store()  # 読み直しても判定が残っている
    assert store.human["t001"]["source"] == "ai-accepted"
    assert store.human["t002"] == store.human["t002"] | {"status": "ok", "source": "human", "note": "確認済み"}
    assert store.human["t003"]["status"] == "fix"
    assert store.human["t003"]["fixed_question"] == "新しい質問"
    assert store.human["t003"]["fixed_answer"] == ""  # 変更なしは空（反映時に元の値）


def test_undo_and_unknown_evidence_is_rejected(qa_dir, monkeypatch):
    out = run_session(monkeypatch, ["o", "", "b", "u", "n", "f", "", "", "c99999", "c01926", "", "q"])
    store = rc.Store()
    assert "t001" not in store.human  # 取り消し済み
    assert "存在しないチャンク ID" in out
    assert store.human["t002"]["fixed_evidence"] == "c01926"


def test_apply_is_idempotent_and_prefers_human(qa_dir, monkeypatch):
    run_session(monkeypatch, ["d", "", "q"])  # t001 を drop（AI は ok）
    rc.apply(accept_ai_for_rest=True)
    first = (qa_dir / "test.jsonl").read_text(encoding="utf-8")
    rc.apply(accept_ai_for_rest=True)
    assert (qa_dir / "test.jsonl").read_text(encoding="utf-8") == first
    qids = {json.loads(line)["qid"] for line in first.splitlines()}
    assert "t001" not in qids
    assert "t002" not in qids  # AI の drop 提案が未判定分に使われる


def test_apply_requires_all_reviewed(qa_dir):
    with pytest.raises(SystemExit):
        rc.apply(accept_ai_for_rest=False)
