"""T07 QA 自動生成：judge モデルでチャンクから質問・正解・根拠 ID を作り data/qa/dev.jsonl / test.jsonl へ出力する。

種別と作り方（割合は計画書どおり。150 問 = 開発用 50 ＋ テスト用 100、種別ごとに層化して分割）
- single       40%  1 チャンクだけで答えられる質問
- multi        20%  同じ記事の別の節、または関連記事の 2 チャンクを組み合わせないと答えられない質問
- confusing    15%  チャンク A で答えられ、別記事のよく似たチャンク B（埋め込みの最近傍）が紛らわしい質問
- unanswerable 25%  コーパスにない関連トピックの質問。上位 10 件の検索結果のどれにも答えがないことを judge で確認

judge は即時性が不要なため Flex 処理（service_tier="flex"）で呼ぶ。不合格分を見込んで約 2 割多く作る。

    uv run python -m src.qa_generate
"""

from __future__ import annotations

import argparse
import json
import random
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from openai import APIConnectionError, APIStatusError, APITimeoutError

from src.clients import openai_client, pg_connect
from src.config import ROOT, load_settings
from src.retriever import Retriever

CHUNKS = ROOT / "data/chunks.jsonl"
QA_DIR = ROOT / "data/qa"
MIX = {"single": 0.40, "multi": 0.20, "confusing": 0.15, "unanswerable": 0.25}
MIN_CHUNK_TOKENS = 150
OVERSAMPLE = 1.25

COMMON_RULES = """- 質問は日本語で、Azure Storage の利用者が実際に尋ねそうな自然な文にする。
- 質問だけを読んで意味が通じるようにする（「この文書」「上記」などの言い回しは使わない）。サービス名や機能名を明記する。
- 答えは事実ひとつで決まるものにし、意見や手順の全体説明を求める質問にしない。
- 正解は 1〜2 文で簡潔に書く。"""


@dataclass
class Chunk:
    id: str
    doc_id: str
    text: str
    tokens: int


class Judge:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.client = openai_client()
        self.calls = {"flex": 0, "default": 0}
        self.tokens = {"flex": [0, 0], "default": [0, 0]}  # [入力, 出力]
        self._lock = threading.Lock()

    def ask_json(self, prompt: str) -> dict:
        kwargs = dict(
            model=self.settings.judge_deployment,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            reasoning_effort="none",
        )
        # Flex は混雑時に 429 や長い待ちになるため、短いタイムアウトで諦めて通常処理に切り替える
        try:
            r = self.client.with_options(max_retries=1, timeout=120).chat.completions.create(service_tier="flex", **kwargs)
            tier = "flex"
        except (APIStatusError, APITimeoutError, APIConnectionError) as e:
            print(f"    flex 失敗（{type(e).__name__}）→ 通常処理で再実行")
            r = self.client.with_options(timeout=120).chat.completions.create(**kwargs)
            tier = "default"
        with self._lock:
            self.calls[tier] += 1
            self.tokens[tier][0] += r.usage.prompt_tokens
            self.tokens[tier][1] += r.usage.completion_tokens
        return json.loads(r.choices[0].message.content or "{}")

    def cost_usd(self) -> float:
        flex = self.tokens["flex"][0] * 1.00 + self.tokens["flex"][1] * 5.00
        std = self.tokens["default"][0] * 2.00 + self.tokens["default"][1] * 10.00
        return (flex + std) / 1e6


# ---- 種別ごとのプロンプト ----------------------------------------------------

def prompt_single(c: Chunk) -> str:
    return f"""次の文章だけを根拠に答えられる質問を 1 つ作ってください。
{COMMON_RULES}
- 文章に答えがはっきり書かれている質問にする。

# 文章（id: {c.id}）
{c.text}

JSON で出力: {{"question": "...", "answer": "...", "ok": true}}
文章が目次・コードだけなどで良い質問を作れない場合は {{"ok": false}}"""


def prompt_multi(a: Chunk, b: Chunk) -> str:
    return f"""次の 2 つの文章の両方の情報を組み合わせないと完全には答えられない質問を 1 つ作ってください。
{COMMON_RULES}
- どちらか片方の文章だけでは答えの一部が欠けるようにする（例：2 つの機能の比較、条件と数値の組み合わせ）。

# 文章 A（id: {a.id}）
{a.text}

# 文章 B（id: {b.id}）
{b.text}

JSON で出力: {{"question": "...", "answer": "...", "ok": true}}
2 つの文章に組み合わせられる共通点がない場合は {{"ok": false}}"""


def prompt_confusing(a: Chunk, b: Chunk) -> str:
    return f"""文章 A だけが正しい答えを含み、よく似た文章 B は紛らわしいが答えにならない質問を 1 つ作ってください。
{COMMON_RULES}
- 文章 B は似たサービスや似た機能の説明です。B の内容で答えると誤りになるような質問にする
  （例：A と B で上限値・既定値・対応状況が異なる点を突く）。

# 文章 A（正解の根拠、id: {a.id}）
{a.text}

# 文章 B（紛らわしい文章、id: {b.id}）
{b.text}

JSON で出力: {{"question": "...", "answer": "...", "distractor_reason": "B で答えると誤りになる理由", "ok": true}}
そのような質問を作れない場合は {{"ok": false}}"""


UNANSWERABLE_STYLES = [
    # 検索では似たチャンクが上位に来るが、答えはコーパス（Blob/Files/Queues/Tables の記事）にない
    "テーマに近いが本コーパスに含まれない隣接サービス（Azure Managed Disks、Azure NetApp Files、Azure Data Box、"
    "Azure Elastic SAN、Azure Backup など）の具体的な仕様（上限値・料金単位・対応リージョンなど）を尋ねる。",
    "文章と同じ機能について、いかにもドキュメントに載っていそうだが実際には書かれていない具体的な数値・条件・組み合わせを尋ねる"
    "（例：文章にない上限値、文章で触れていない別の操作やツールとの組み合わせでの挙動）。"
    "内部実装や社内情報のように、明らかに公開されない情報は避ける。",
]


def prompt_unanswerable(c: Chunk, style: str) -> str:
    return f"""次の文章のテーマに関連するが、この文章にも Azure Storage の公式ドキュメントにも答えが書かれていない可能性が高い質問を 1 つ作ってください。
{COMMON_RULES}
- 作り方：{style}
- Azure Storage の利用者が尋ねそうな、テーマに沿った自然な質問にする。

# 文章（テーマの参考、id: {c.id}）
{c.text}

JSON で出力: {{"question": "...", "ok": true}}"""


def prompt_verify_unanswerable(question: str, passages: list[str]) -> str:
    body = "\n\n".join(f"[{i}]\n{p}" for i, p in enumerate(passages, 1))
    return f"""次の質問に、下の文章のいずれかで答えられるか判定してください。部分的にでも答えられる場合は answerable を true にします。

# 質問
{question}

# 文章
{body}

JSON で出力: {{"answerable": true または false, "reason": "..."}}"""


# ---- 生成 --------------------------------------------------------------------

def nearest_other_doc(conn, chunk_id: str, doc_id: str, k: int = 5) -> list[tuple[str, float]]:
    rows = conn.execute(
        """SELECT c.id, 1 - (c.embedding <=> s.embedding) FROM chunks c, chunks s
           WHERE s.id = %s AND c.doc_id <> %s ORDER BY c.embedding <=> s.embedding LIMIT %s""",
        (chunk_id, doc_id, k),
    ).fetchall()
    return [(r[0], float(r[1])) for r in rows]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--total", type=int, default=150)
    parser.add_argument("--dev", type=int, default=50)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    chunks = [Chunk(d["id"], d["doc_id"], d["text"], d["tokens"]) for d in map(json.loads, CHUNKS.read_text(encoding="utf-8").splitlines())]
    by_id = {c.id: c for c in chunks}
    by_doc: dict[str, list[Chunk]] = {}
    for c in chunks:
        by_doc.setdefault(c.doc_id, []).append(c)
    eligible = [c for c in chunks if c.tokens >= MIN_CHUNK_TOKENS]

    targets = {t: round(args.total * p) for t, p in MIX.items()}
    targets["unanswerable"] = args.total - sum(v for t, v in targets.items() if t != "unanswerable")
    print("目標:", targets)

    # 記事の偏りを避けるため、記事をシャッフルして 1 記事から 1 チャンクずつ順に使う
    docs = list({c.doc_id for c in eligible})
    rng.shuffle(docs)

    def pick_chunks(n: int) -> list[Chunk]:
        out: list[Chunk] = []
        while len(out) < n:
            for d in docs:
                cands = [c for c in by_doc[d] if c.tokens >= MIN_CHUNK_TOKENS and c not in out]
                if cands:
                    out.append(rng.choice(cands))
                if len(out) >= n:
                    break
        return out

    need = {t: int(v * OVERSAMPLE) + 2 for t, v in targets.items()}
    pool = pick_chunks(sum(need.values()))
    rng.shuffle(pool)
    seeds = {}
    i = 0
    for t, n in need.items():
        seeds[t] = pool[i : i + n]
        i += n

    conn = pg_connect()
    jobs: list[tuple[str, tuple]] = []
    for c in seeds["single"]:
        jobs.append(("single", (c,)))
    for c in seeds["multi"]:
        siblings = [s for s in by_doc[c.doc_id] if s.id != c.id and s.tokens >= MIN_CHUNK_TOKENS]
        if siblings and rng.random() < 0.5:
            jobs.append(("multi", (c, rng.choice(siblings))))
        else:  # 関連記事の近いチャンク（類似しすぎない 2〜5 番目）
            near = nearest_other_doc(conn, c.id, c.doc_id, 5)
            jobs.append(("multi", (c, by_id[rng.choice(near[1:])[0]])))
    for c in seeds["confusing"]:
        b_id, sim = nearest_other_doc(conn, c.id, c.doc_id, 1)[0]
        jobs.append(("confusing", (c, by_id[b_id], sim)))
    for j, c in enumerate(seeds["unanswerable"]):
        jobs.append(("unanswerable", (c, UNANSWERABLE_STYLES[j % len(UNANSWERABLE_STYLES)])))
    conn.close()

    judge = Judge()
    retriever = Retriever()

    def run(job: tuple[str, tuple]) -> dict | None:
        kind, a = job
        try:
            if kind == "single":
                r = judge.ask_json(prompt_single(a[0]))
                if not r.get("ok", True) or not r.get("question"):
                    return None
                return {"type": kind, "question": r["question"], "answer": r["answer"], "evidence_chunk_ids": [a[0].id],
                        "meta": {"seed": a[0].id}}
            if kind == "multi":
                r = judge.ask_json(prompt_multi(a[0], a[1]))
                if not r.get("ok", True) or not r.get("question"):
                    return None
                return {"type": kind, "question": r["question"], "answer": r["answer"],
                        "evidence_chunk_ids": [a[0].id, a[1].id], "meta": {"same_doc": a[0].doc_id == a[1].doc_id}}
            if kind == "confusing":
                r = judge.ask_json(prompt_confusing(a[0], a[1]))
                if not r.get("ok", True) or not r.get("question"):
                    return None
                return {"type": kind, "question": r["question"], "answer": r["answer"], "evidence_chunk_ids": [a[0].id],
                        "meta": {"distractor": a[1].id, "distractor_sim": round(a[2], 4),
                                 "distractor_reason": r.get("distractor_reason", "")}}
            if kind == "unanswerable":
                r = judge.ask_json(prompt_unanswerable(a[0], a[1]))
                q = r.get("question")
                if not q:
                    return None
                hits = retriever_search(q)
                v = judge.ask_json(prompt_verify_unanswerable(q, [h.content for h in hits]))
                if v.get("answerable", True):
                    return None
                return {"type": kind, "question": q, "answer": "わかりません", "evidence_chunk_ids": [],
                        "meta": {"theme": a[0].id, "style": UNANSWERABLE_STYLES.index(a[1]),
                                 "verify_reason": v.get("reason", "")}}
        except Exception as e:  # 1 件の失敗で全体を止めない
            print(f"  error {kind}: {type(e).__name__}: {str(e)[:200]}")
        return None

    # Retriever は DB 接続を 1 本だけ持つので、検索だけ直列化する
    lock = threading.Lock()

    def retriever_search(q: str):
        with lock:
            return retriever.search(q, 10).hits

    start = time.perf_counter()
    results: dict[str, list[dict]] = {t: [] for t in MIX}
    with ThreadPoolExecutor(args.workers) as ex:
        for n, res in enumerate(ex.map(run, jobs), 1):
            if res:
                results[res["type"]].append(res)
            if n % 10 == 0 or n == len(jobs):
                print(f"  {n}/{len(jobs)} 合格 {sum(len(v) for v in results.values())}")
    retriever.close()

    print("合格数:", {t: len(v) for t, v in results.items()}, "/ 目標", targets)

    # 種別ごとに目標数だけ取り、開発用とテスト用に層化して分割
    dev, test = [], []
    for t, items in results.items():
        rng.shuffle(items)
        items = items[: targets[t]]
        n_dev = round(len(items) * args.dev / args.total)
        for j, it in enumerate(items):
            (dev if j < n_dev else test).append(it)
    QA_DIR.mkdir(parents=True, exist_ok=True)
    for split, items, prefix in (("dev", dev, "d"), ("test", test, "t")):
        rng.shuffle(items)
        with (QA_DIR / f"{split}.jsonl").open("w", encoding="utf-8") as f:
            for k, it in enumerate(items, 1):
                row = {"qid": f"{prefix}{k:03d}", "question": it["question"], "answer": it["answer"],
                       "evidence_chunk_ids": it["evidence_chunk_ids"], "type": it["type"], "split": split, "meta": it["meta"]}
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"{split}: {len(items)} 問 {dict(Counter(it['type'] for it in items))}")

    print(f"judge 呼び出し {judge.calls}、トークン {judge.tokens}、約 ${judge.cost_usd():.3f}、"
          f"{time.perf_counter() - start:.0f} 秒")


if __name__ == "__main__":
    main()
