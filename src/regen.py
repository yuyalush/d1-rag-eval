"""生成モデルの差し替え比較：最終評価のログを使い、回答の生成だけを別のモデルでやり直す。

検索結果・Decision-1 の判定・構成 E の LLM 判定・構成 C のゲートの結果は元のログのまま使い、
LLM に渡したチャンク（passed_ids）から回答だけを作り直す。違うのは生成モデルだけになる。
- 構成 C でゲートが回答を控えた行は、生成しないのでそのままコピーする
- 中断しても、作り直し済みの (qid) は飛ばして再開できる

    uv run python -m src.regen --source test-final --target test-final-nano --deployment gen-nano
    uv run python -m src.score --run-id test-final-nano --split test     # 以降は通常と同じ

Phi-4-mini-instruct は 1 分あたり 2 万トークン（入力＋max_tokens で見積もられる）の上限があり、
1 回約 2,200 トークンなので --rpm 7 程度に抑えないと 429 の再試行待ちで逆に遅くなる。
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from src.evaluate import RESULTS
from src.config import ROOT
from src.generator import Generator


class Limiter:
    """リクエストの開始間隔を 60/rpm 秒以上に保つ（スレッド用）。"""

    def __init__(self, rpm: float) -> None:
        self.interval = 60.0 / rpm
        self.next_at = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            delay = self.next_at - now
            self.next_at = max(now, self.next_at) + self.interval
        if delay > 0:
            time.sleep(delay)


def regenerate(row: dict, gen: Generator, chunks: dict[str, str], limiter: Limiter, deployment: str) -> dict:
    new = dict(row)
    new["gen_deployment"] = deployment
    new["source_run_id"] = row.get("run_id", "")
    if row.get("gated"):
        return new  # ゲートで控えた行は生成していないので、そのまま
    limiter.wait()
    g = gen.generate(row["question"], [chunks[cid] for cid in row["passed_ids"]])
    lat = dict(row["latency_ms"])
    lat["total"] = round(lat["total"] - lat.get("generate", 0.0) + g.latency_ms, 1)
    lat["generate"] = round(g.latency_ms, 1)
    tokens = dict(row["tokens"])
    tokens["gen_in"], tokens["gen_out"] = g.input_tokens, g.output_tokens
    new.update(answer=g.answer, abstained=g.is_unknown, latency_ms=lat, tokens=tokens)
    return new


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, help="元の run_id（例: test-final）")
    parser.add_argument("--target", required=True, help="作り直した結果の run_id（例: test-final-nano）")
    parser.add_argument("--deployment", required=True, help="生成に使うデプロイ（config/models.yaml の name）")
    parser.add_argument("--rpm", type=float, default=200, help="1 分あたりのリクエスト上限")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--runs", type=int, nargs="*", help="作り直す実行回（省略時は全部）。例: --runs 1")
    args = parser.parse_args()

    src_dir, out_dir = RESULTS / args.source, RESULTS / args.target
    out_dir.mkdir(parents=True, exist_ok=True)
    chunks = {d["id"]: d["text"] for d in map(json.loads, (ROOT / "data/chunks.jsonl").read_text(encoding="utf-8").splitlines())}
    gen = Generator(deployment=args.deployment)
    limiter = Limiter(args.rpm)
    print(f"生成: {args.deployment}  パラメーター: {gen.params}")

    for log in sorted(src_dir.glob("*__r*.jsonl")):
        if log.name.startswith("scores__"):
            continue
        if args.runs and int(log.stem.split("__r")[1]) not in args.runs:
            continue
        dest = out_dir / log.name
        done = {json.loads(line)["qid"] for line in dest.read_text(encoding="utf-8").splitlines()} if dest.exists() else set()
        rows = [r for r in map(json.loads, log.read_text(encoding="utf-8").splitlines()) if r["qid"] not in done]
        start = time.perf_counter()
        lock = threading.Lock()
        with ThreadPoolExecutor(args.workers) as ex, dest.open("a", encoding="utf-8") as f:
            for new in ex.map(lambda r: regenerate(r, gen, chunks, limiter, args.deployment), rows):
                new["run_id"] = args.target
                with lock:
                    f.write(json.dumps(new, ensure_ascii=False) + "\n")
                    f.flush()
        print(f"{log.name}: {len(rows)} 件（既存 {len(done)}）{time.perf_counter() - start:.0f} 秒")


if __name__ == "__main__":
    main()
