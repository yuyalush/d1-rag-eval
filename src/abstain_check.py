"""回答不能な質問への回答を「作り話」と「意味上の控え」に分ける。

控えの判定（abstained）は回答が「わかりません」で始まるかだけを見るので、
「…は記載されていません。したがって、わかりません。」のような書き方は控えとして数えられない。
回答不能な質問で abstained=false の回答だけを judge に渡し、具体的な答えを断定しているか（作り話）、
参考情報にないので答えられないと述べているか（意味上の控え）を判定する。

結果は results/<run_id>/abstain_check.jsonl に追記し、判定済みの回答は飛ばす。

    uv run python -m src.abstain_check --run-id test-final-nano
"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor

from src.evaluate import RESULTS, load_qa
from src.score import Scorer

PROMPT = """次の質問は、参考資料に答えが書かれていない「回答不能」な質問です。回答を次のどちらかに分類してください。
- fabricated：質問に対する具体的な答え（数値、はい／いいえ、手順、名称など）を断定している。推測や一般論で答えている場合も含む
- declined：参考情報に答えがない、またはわからないと述べ、具体的な答えを断定していない

# 質問
{question}

# 回答
{answer}

JSON で出力: {{"label": "fabricated" | "declined", "reason": "..."}}"""


def key(config: str, run: int, qid: str) -> str:
    return f"{config}__r{run}__{qid}"


def load(out_dir) -> dict[str, dict]:
    path = out_dir / "abstain_check.jsonl"
    if not path.exists():
        return {}
    return {d["key"]: d for d in map(json.loads, path.read_text(encoding="utf-8").splitlines())}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--split", default="test")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    out_dir = RESULTS / args.run_id
    qa = {q["qid"]: q for q in load_qa(args.split)}
    done = load(out_dir)
    todo = []
    for log in sorted(out_dir.glob("*__r*.jsonl")):
        if log.name.startswith("scores__"):
            continue
        config, run = log.stem.split("__r")
        for r in map(json.loads, log.read_text(encoding="utf-8").splitlines()):
            k = key(config, int(run), r["qid"])
            if qa[r["qid"]]["type"] == "unanswerable" and not r["abstained"] and k not in done:
                todo.append((k, r))

    scorer = Scorer()

    def judge(item):
        k, r = item
        j = scorer.ask_json(PROMPT.format(question=r["question"], answer=r["answer"]))
        return {"key": k, "label": j.get("label", "fabricated"), "reason": j.get("reason", "")}

    with ThreadPoolExecutor(args.workers) as ex, (out_dir / "abstain_check.jsonl").open("a", encoding="utf-8") as f:
        for d in ex.map(judge, todo):
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    print(f"{args.run_id}: {len(todo)} 件判定（既存 {len(done)}）、judge 約 ${scorer.cost_usd():.4f}")


if __name__ == "__main__":
    main()
