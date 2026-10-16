"""T14 LLM 採点：ログの各回答を judge で採点する（テスト用セットの最終評価で使う）。

- 正答率：正解と比べて correct / partial / wrong の 3 段階（1 / 0.5 / 0 点）
- Faithfulness：回答中の主張のうち、LLM に渡したチャンクで裏付けられる割合
- 回答を控えた場合は LLM を使わずに採点する（回答不能なら correct、回答可能なら wrong。Faithfulness は対象外）

採点結果は results/<run_id>/scores__<config>__r<n>.jsonl に追記し、中断・再開できる。

    uv run python -m src.score --run-id test-final --split test
"""

from __future__ import annotations

import argparse
import json
import threading
from concurrent.futures import ThreadPoolExecutor

from openai import APIConnectionError, APIStatusError, APITimeoutError

from src.clients import openai_client
from src.config import ROOT, load_settings
from src.evaluate import RESULTS, load_qa

CHUNKS = ROOT / "data/chunks.jsonl"

CORRECTNESS_PROMPT = """あなたは質問応答の採点者です。正解と照らして、回答を 3 段階で採点してください。
- correct：正解の要点をすべて含み、誤りがない
- partial：正解の要点の一部だけを含む、または一部に誤りがある
- wrong：正解の要点を含まない、誤っている、または「わかりません」と答えている

# 質問
{question}

# 正解
{gold}

# 回答
{answer}

JSON で出力: {{"grade": "correct" | "partial" | "wrong", "reason": "..."}}"""

FAITHFULNESS_PROMPT = """回答を事実の主張に分解し、それぞれが参考情報で裏付けられるか判定してください。
参考情報に書かれていない主張は、内容が正しくても裏付けなしとします。

# 参考情報
{context}

# 回答
{answer}

JSON で出力: {{"claims": [{{"claim": "...", "supported": true | false}}]}}"""


class Scorer:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.client = openai_client()
        self.tokens = {"flex": [0, 0], "default": [0, 0]}
        self.lock = threading.Lock()

    def ask_json(self, prompt: str) -> dict:
        kwargs = dict(
            model=self.settings.judge_deployment,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0,
            reasoning_effort="none",
        )
        try:
            r = self.client.with_options(max_retries=1, timeout=120).chat.completions.create(service_tier="flex", **kwargs)
            tier = "flex"
        except (APIStatusError, APITimeoutError, APIConnectionError):
            r = self.client.with_options(timeout=120).chat.completions.create(**kwargs)
            tier = "default"
        with self.lock:
            self.tokens[tier][0] += r.usage.prompt_tokens
            self.tokens[tier][1] += r.usage.completion_tokens
        return json.loads(r.choices[0].message.content or "{}")

    def cost_usd(self) -> float:
        return (self.tokens["flex"][0] * 1.0 + self.tokens["flex"][1] * 5.0
                + self.tokens["default"][0] * 2.0 + self.tokens["default"][1] * 10.0) / 1e6


def score_one(scorer: Scorer, res: dict, q: dict, chunks: dict[str, str]) -> dict:
    answerable = q["type"] != "unanswerable"
    out = {"qid": res["qid"], "config": res["config"], "abstained": res["abstained"]}
    if res["abstained"]:
        out.update(grade="wrong" if answerable else "correct", reason="rule: abstained", faithfulness=None, claims=[])
        return out
    if not answerable:  # 回答不能なのに答えた
        out.update(grade="wrong", reason="rule: answered unanswerable question")
    else:
        g = scorer.ask_json(CORRECTNESS_PROMPT.format(question=q["question"], gold=q["answer"], answer=res["answer"]))
        out.update(grade=g.get("grade", "wrong"), reason=g.get("reason", ""))
    context = "\n\n".join(f"[{i}]\n{chunks[cid]}" for i, cid in enumerate(res["passed_ids"], 1)) or "（なし）"
    f = scorer.ask_json(FAITHFULNESS_PROMPT.format(context=context, answer=res["answer"]))
    claims = f.get("claims", [])
    out["claims"] = claims
    out["faithfulness"] = (sum(bool(c.get("supported")) for c in claims) / len(claims)) if claims else None
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--split", default="test", choices=["dev", "test"])
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    out_dir = RESULTS / args.run_id
    qa = {q["qid"]: q for q in load_qa(args.split)}
    chunks = {d["id"]: d["text"] for d in map(json.loads, CHUNKS.read_text(encoding="utf-8").splitlines())}
    scorer = Scorer()
    for log in sorted(out_dir.glob("*__r*.jsonl")):
        if log.name.startswith("scores__"):
            continue
        dest = out_dir / f"scores__{log.name}"
        done = {json.loads(line)["qid"] for line in dest.read_text(encoding="utf-8").splitlines()} if dest.exists() else set()
        todo = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        todo = [r for r in todo if r["qid"] not in done]
        with ThreadPoolExecutor(args.workers) as ex, dest.open("a", encoding="utf-8") as f:
            for s in ex.map(lambda r: score_one(scorer, r, qa[r["qid"]], chunks), todo):
                f.write(json.dumps(s, ensure_ascii=False) + "\n")
        print(f"{log.name}: {len(todo)} 件採点（既存 {len(done)}）")
    print(f"judge トークン {scorer.tokens}、約 ${scorer.cost_usd():.3f}")


if __name__ == "__main__":
    main()
