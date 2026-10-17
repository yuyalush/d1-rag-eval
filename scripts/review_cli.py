"""T08 テスト用セットのレビューをターミナルで行う対話型ツール。

    uv run python -m scripts.review_cli                 # 未判定の質問から順にレビュー
    uv run python -m scripts.review_cli --start t050    # t050 から
    uv run python -m scripts.review_cli --only ai-fix   # AI が fix / drop を提案した質問だけ（ai-fix / ai-drop / pending / all）
    uv run python -m scripts.review_cli status          # 進捗の表示
    uv run python -m scripts.review_cli apply           # 判定を data/qa/test.jsonl に反映

判定は 1 問ごとに data/qa/test_human_review.jsonl に保存されるので、途中で終了しても続きから再開できる。
AI の提案（data/qa/test_ai_review.jsonl）を初期値として表示し、人の判定を優先して反映する。
各画面で ? を入力するとコマンドの一覧を表示する。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from src.config import ROOT

QA = ROOT / "data/qa"
CHUNKS = ROOT / "data/chunks.jsonl"
MANIFEST = ROOT / "data/raw/manifest.jsonl"
AI_FILE = QA / "test_ai_review.jsonl"
HUMAN_FILE = QA / "test_human_review.jsonl"
STATUSES = ("ok", "fix", "drop")

# ---- 表示 --------------------------------------------------------------------

USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def color(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


def bold(t: str) -> str:
    return color(t, "1")


def dim(t: str) -> str:
    return color(t, "2")


STATUS_COLOR = {"ok": "32", "fix": "33", "drop": "31"}


def status_label(s: str) -> str:
    return color(s, "1;" + STATUS_COLOR.get(s, "37")) if s else dim("未判定")


HELP = f"""
{bold('判定')}
  Enter / a        AI の提案をそのまま採用して次へ
  o                ok（そのまま使う）
  f                fix（質問・模範解答・根拠を修正。AI の修正案が入力欄に入った状態で始まる）
  d                drop（評価から除外）
  m                メモ（note）だけを書き換える
  u                この質問の判定を取り消す
{bold('チャンクを見る')}（less で全文表示。q で戻る）
  v                根拠チャンクと囮チャンクの全文
  v 2              2 番目の根拠チャンクの全文
  v c01234 ...     指定したチャンクの全文（複数可）
  c                根拠チャンクの前後のチャンクも含めて全文
  s <語句>         コーパス全体を語句（正規表現可）で検索
  r                この質問で実際に検索した上位 10 件（Azure に接続）
{bold('移動')}
  n / b            次へ / 前へ（判定せずに移動）
  g t050           指定した質問へ移動
  l                進捗の一覧
  q                終了（判定は都度保存済み）
"""


def clear() -> None:
    if sys.stdout.isatty():
        print("\033[2J\033[H", end="")


def pager(text: str) -> None:
    """less で表示する。less がなければそのまま出力する。"""
    if sys.stdout.isatty() and shutil.which("less"):
        subprocess.run(["less", "-R", "-X", "-F"], input=text.encode("utf-8"), check=False)
    else:
        print(text)


def wrap_lines(text: str, limit: int) -> str:
    """先頭 limit 文字に切り詰める（改行は保つ）。"""
    return text if len(text) <= limit else text[:limit] + dim(f" …（残り {len(text) - limit} 文字。v で全文）")


# ---- データ ------------------------------------------------------------------


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class Store:
    def __init__(self) -> None:
        # 反映後も元の 100 問でレビューできるように、退避した元データがあればそちらを使う
        src = QA / "test.orig.jsonl" if (QA / "test.orig.jsonl").exists() else QA / "test.jsonl"
        self.questions = sorted(load_jsonl(src), key=lambda q: q["qid"])
        self.qindex = {q["qid"]: i for i, q in enumerate(self.questions)}
        self.chunks = load_jsonl(CHUNKS)
        self.cindex = {c["id"]: i for i, c in enumerate(self.chunks)}
        self.urls = {m["doc_id"]: m["url"] for m in load_jsonl(MANIFEST)}
        self.ai = {r["qid"]: r for r in load_jsonl(AI_FILE)}
        self.human: dict[str, dict] = {}
        for r in load_jsonl(HUMAN_FILE):  # 追記型。後の行が優先、deleted は取り消し
            if r.get("deleted"):
                self.human.pop(r["qid"], None)
            else:
                self.human[r["qid"]] = r

    def save(self, record: dict) -> None:
        record["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        with HUMAN_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        if record.get("deleted"):
            self.human.pop(record["qid"], None)
        else:
            self.human[record["qid"]] = record

    def chunk(self, cid: str) -> dict | None:
        i = self.cindex.get(cid)
        return self.chunks[i] if i is not None else None

    def neighbor(self, cid: str, offset: int) -> dict | None:
        i = self.cindex.get(cid)
        if i is None or not 0 <= i + offset < len(self.chunks):
            return None
        c = self.chunks[i + offset]
        return c if c["doc_id"] == self.chunks[i]["doc_id"] else None

    def chunk_full(self, cid: str, label: str = "") -> str:
        c = self.chunk(cid)
        if not c:
            return f"[{cid}] 見つかりません\n"
        url = self.urls.get(c["doc_id"], c["doc_id"])
        head = f"===== {label}[{cid}] {c['doc_id']}  {url}"
        return f"{bold(head)}\n{c['text']}\n"


# ---- 入力 --------------------------------------------------------------------

try:
    import readline  # 入力欄の編集（矢印キー・履歴）を有効にする
except ImportError:  # Windows など
    readline = None


def input_prefill(prompt: str, text: str = "") -> str:
    """初期値を入力欄に入れた状態で入力を受け付ける（readline がない環境では初期値を表示するだけ）。"""
    if readline is None:
        if text:
            print(dim(f"  現在の値: {text}"))
        value = input(prompt)
        return value if value else text

    def hook() -> None:
        readline.insert_text(text)
        readline.redisplay()

    readline.set_pre_input_hook(hook)
    try:
        value = input(prompt)
    finally:
        readline.set_pre_input_hook()
    # 入力欄を空にして Enter した場合も「現在の値のまま」とする（空にしたいときは :- を使う）
    return value if value.strip() else text


def edit_in_editor(text: str) -> str:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or ("nano" if shutil.which("nano") else "vi")
    with tempfile.NamedTemporaryFile("w+", suffix=".txt", encoding="utf-8", delete=False) as f:
        f.write(text)
        path = f.name
    try:
        subprocess.run([editor, path], check=False)
        return Path(path).read_text(encoding="utf-8").strip()
    finally:
        os.unlink(path)


def ask_field(label: str, current: str, store: Store | None = None, evidence: bool = False) -> str:
    """1 項目を入力させる。:e でエディター、:- で空にする。"""
    print(dim(f"  {label}（Enter で確定 / :e でエディター / :- で空にする）"))
    while True:
        value = input_prefill(f"  {label}> ", current).strip()
        if value == ":e":
            value = edit_in_editor(current)
            print(f"  {label}: {value}")
        elif value == ":-":
            value = ""
        if evidence and store is not None:
            unknown = [cid for cid in value.split() if store.chunk(cid) is None]
            if unknown:
                print(color(f"  存在しないチャンク ID: {' '.join(unknown)}（もう一度入力）", "31"))
                current = value
                continue
        return value


# ---- 画面 --------------------------------------------------------------------


def render(store: Store, q: dict) -> None:
    clear()
    reviewed = len(store.human)
    total = len(store.questions)
    ai = store.ai.get(q["qid"], {})
    human = store.human.get(q["qid"])
    print(dim(f"レビュー済み {reviewed}/{total}　（? でヘルプ）"))
    print(bold(f"■ {q['qid']}  [{q['type']}]") + "　判定: " + status_label(human["status"] if human else "")
          + (dim("（AI の提案を採用）") if human and human.get("source") == "ai-accepted" else ""))
    print(f"{bold('質問')}　{q['question']}")
    print(f"{bold('模範解答')}　{q['answer']}")
    print()
    for n, cid in enumerate(q["evidence_chunk_ids"], 1):
        c = store.chunk(cid)
        heading = c["text"].split("\n", 1)[0] if c else "（見つかりません）"
        body = c["text"].split("\n", 1)[1].strip() if c and "\n" in c["text"] else ""
        print(bold(f"根拠 {n}: {cid}") + dim(f"  {heading}"))
        print(wrap_lines(body, 400))
        print()
    distractor = q.get("meta", {}).get("distractor")
    if distractor:
        c = store.chunk(distractor)
        print(bold(f"囮: {distractor}") + dim(f"  {c['text'].split(chr(10), 1)[0] if c else ''}"))
        print()
    if not q["evidence_chunk_ids"]:
        print(dim("（回答不能の質問。r で実際の検索結果、s で語句検索を確認できます）"))
        print()
    if ai:
        print(bold("AI の提案: ") + status_label(ai.get("ai_status", "")))
        print(f"  理由: {ai.get('ai_reason', '')}")
        for key, label in (("ai_fixed_question", "修正後の質問"), ("ai_fixed_answer", "修正後の模範解答"),
                           ("ai_fixed_evidence", "修正後の根拠")):
            if ai.get(key):
                print(color(f"  {label}: {ai[key]}", "33"))
    if human and human.get("source") == "human":
        print()
        print(bold("あなたの判定: ") + status_label(human["status"]))
        for key, label in (("fixed_question", "修正後の質問"), ("fixed_answer", "修正後の模範解答"),
                           ("fixed_evidence", "修正後の根拠"), ("note", "メモ")):
            if human.get(key):
                print(f"  {label}: {human[key]}")
    print()


def view_chunks(store: Store, q: dict, args: list[str]) -> None:
    if args and all(a.isdigit() for a in args):
        ids = [q["evidence_chunk_ids"][int(a) - 1] for a in args if 0 < int(a) <= len(q["evidence_chunk_ids"])]
        pager("\n".join(store.chunk_full(cid, "根拠 ") for cid in ids) or "該当なし")
    elif args:
        pager("\n".join(store.chunk_full(cid) for cid in args))
    else:
        parts = [store.chunk_full(cid, f"根拠 {n} ") for n, cid in enumerate(q["evidence_chunk_ids"], 1)]
        d = q.get("meta", {}).get("distractor")
        if d:
            parts.append(store.chunk_full(d, "囮 "))
        pager("\n".join(parts) if parts else "根拠チャンクはありません（回答不能の質問）。r か s を使ってください。")


def view_context(store: Store, q: dict) -> None:
    parts = []
    for n, cid in enumerate(q["evidence_chunk_ids"], 1):
        prev_c, next_c = store.neighbor(cid, -1), store.neighbor(cid, 1)
        if prev_c:
            parts.append(store.chunk_full(prev_c["id"], f"根拠 {n} の前 "))
        parts.append(store.chunk_full(cid, f"根拠 {n} "))
        if next_c:
            parts.append(store.chunk_full(next_c["id"], f"根拠 {n} の次 "))
    pager("\n".join(parts) if parts else "根拠チャンクはありません（回答不能の質問）。")


def search_corpus(store: Store, pattern: str, limit: int = 30) -> None:
    try:
        rx = re.compile(pattern)
    except re.error:
        rx = re.compile(re.escape(pattern))
    lines, hits = [], 0
    for c in store.chunks:
        m = rx.search(c["text"])
        if not m:
            continue
        hits += 1
        if hits <= limit:
            s = c["text"]
            snippet = s[max(0, m.start() - 60): m.end() + 80].replace("\n", " ")
            lines.append(f"{bold(c['id'])} {dim(c['doc_id'])}\n  …{snippet}…\n")
    header = f"「{pattern}」の検索結果: {hits} 件" + (f"（先頭 {limit} 件を表示）" if hits > limit else "")
    pager(header + "\n\n" + "\n".join(lines) + "\n全文は v <チャンク ID> で表示できます。")


_retriever = None


def search_vector(q: dict) -> None:
    global _retriever
    try:
        if _retriever is None:
            print(dim("検索の準備中…"))
            from src.retriever import Retriever

            _retriever = Retriever()
        hits = _retriever.search(q["question"], 10).hits
    except Exception as e:  # Azure に接続できないときもツール自体は使えるようにする
        input(color(f"検索できませんでした: {type(e).__name__}: {e}（Enter で戻る）", "31"))
        return
    gold = set(q["evidence_chunk_ids"])
    lines = [f"質問: {q['question']}\n"]
    for rank, h in enumerate(hits, 1):
        mark = color(" ← 根拠", "32") if h.id in gold else ""
        lines.append(f"{rank:2}. {bold(h.id)} 類似度 {h.score:.3f} {dim(h.doc_id)}{mark}\n{h.content[:500]}\n")
    pager("\n".join(lines))


# ---- 判定 --------------------------------------------------------------------


def decide(store: Store, q: dict, status: str) -> bool:
    ai = store.ai.get(q["qid"], {})
    prev = store.human.get(q["qid"], {})
    record = {"qid": q["qid"], "status": status, "source": "human",
              "fixed_question": "", "fixed_answer": "", "fixed_evidence": "", "note": prev.get("note", "")}
    if status == "fix":
        print(bold("\n修正内容を入力します。") + dim("（AI の修正案、または現在の値が入った状態で始まります）"))
        record["fixed_question"] = ask_field("質問", prev.get("fixed_question") or ai.get("ai_fixed_question") or q["question"])
        record["fixed_answer"] = ask_field("模範解答", prev.get("fixed_answer") or ai.get("ai_fixed_answer") or q["answer"])
        record["fixed_evidence"] = ask_field("根拠（チャンク ID をスペース区切り）",
                                             prev.get("fixed_evidence") or ai.get("ai_fixed_evidence") or " ".join(q["evidence_chunk_ids"]),
                                             store, evidence=True)
        # 元と同じ値は「変更なし」として空にしておく（反映時に元の値が使われる）
        if record["fixed_question"] == q["question"]:
            record["fixed_question"] = ""
        if record["fixed_answer"] == q["answer"]:
            record["fixed_answer"] = ""
        if record["fixed_evidence"] == " ".join(q["evidence_chunk_ids"]):
            record["fixed_evidence"] = ""
        if not (record["fixed_question"] or record["fixed_answer"] or record["fixed_evidence"]):
            print(color("  何も変更されていません。ok として登録する場合は o を使ってください。", "33"))
            return False
    record["note"] = ask_field("メモ（任意）", record["note"] or (f"AI: {ai['ai_reason']}" if status == ai.get("ai_status") and ai else ""))
    store.save(record)
    return True


def accept_ai(store: Store, q: dict) -> bool:
    ai = store.ai.get(q["qid"])
    if not ai:
        print(color("この質問には AI の提案がありません。", "31"))
        return False
    store.save({"qid": q["qid"], "status": ai["ai_status"], "source": "ai-accepted",
                "fixed_question": ai.get("ai_fixed_question", ""), "fixed_answer": ai.get("ai_fixed_answer", ""),
                "fixed_evidence": ai.get("ai_fixed_evidence", ""), "note": f"AI: {ai.get('ai_reason', '')}"})
    return True


def edit_note(store: Store, q: dict) -> None:
    rec = store.human.get(q["qid"])
    if not rec:
        print(color("先に判定してください。", "31"))
        return
    rec = dict(rec)
    rec["note"] = ask_field("メモ", rec.get("note", ""))
    store.save(rec)


def show_progress(store: Store) -> None:
    lines = []
    for q in store.questions:
        h = store.human.get(q["qid"])
        ai = store.ai.get(q["qid"], {}).get("ai_status", "")
        mark = status_label(h["status"]) if h else dim("・")
        diff = color(" ≠AI", "35") if h and ai and h["status"] != ai else ""
        lines.append(f"{q['qid']} [{q['type']:12}] AI:{ai:4} 判定:{mark}{diff}  {q['question'][:40]}")
    counts = {s: sum(1 for h in store.human.values() if h["status"] == s) for s in STATUSES}
    pager(f"レビュー済み {len(store.human)}/{len(store.questions)}  {counts}\n\n" + "\n".join(lines))


# ---- メインループ ----------------------------------------------------------------


def select(store: Store, only: str) -> list[dict]:
    if only == "all":
        return store.questions
    if only == "pending":
        return [q for q in store.questions if q["qid"] not in store.human]
    want = {"ai-fix": {"fix", "drop"}, "ai-drop": {"drop"}}[only]
    return [q for q in store.questions if store.ai.get(q["qid"], {}).get("ai_status") in want]


def run(only: str, start: str | None) -> None:
    store = Store()
    targets = select(store, only)
    if not targets:
        print("対象の質問はありません。" + ("（すべてレビュー済みです。status で確認、apply で反映できます）" if only == "pending" else ""))
        return
    pos = next((i for i, q in enumerate(targets) if q["qid"] >= start), 0) if start else 0
    message = ""
    while True:
        q = targets[pos]
        render(store, q)
        if message:
            print(message)
            message = ""
        try:
            raw = input(bold(f"{q['qid']}> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        cmd, *args = raw.split() if raw else ["a"]
        advanced = False
        if cmd == "?":
            pager(HELP)
        elif cmd == "a":
            advanced = accept_ai(store, q)
        elif cmd in ("o", "f", "d"):
            advanced = decide(store, q, {"o": "ok", "f": "fix", "d": "drop"}[cmd])
        elif cmd == "m":
            edit_note(store, q)
        elif cmd == "u":
            store.save({"qid": q["qid"], "deleted": True})
            message = color("判定を取り消しました。", "33")
        elif cmd == "v":
            view_chunks(store, q, args)
        elif cmd == "c":
            view_context(store, q)
        elif cmd == "s":
            if args:
                search_corpus(store, " ".join(args))
            else:
                message = color("検索する語句を指定してください（例: s 早期削除）", "31")
        elif cmd == "r":
            search_vector(q)
        elif cmd == "n":
            pos = min(pos + 1, len(targets) - 1)
        elif cmd == "b":
            pos = max(pos - 1, 0)
        elif cmd == "g" and args:
            qid = args[0] if args[0].startswith("t") else f"t{int(args[0]):03d}"
            idx = next((i for i, t in enumerate(targets) if t["qid"] == qid), None)
            if idx is None:
                message = color(f"{qid} は現在の対象にありません（--only all で起動すると全問を移動できます）", "31")
            else:
                pos = idx
        elif cmd == "l":
            show_progress(store)
        elif cmd == "q":
            break
        else:
            message = color(f"不明なコマンド: {raw}（? でヘルプ）", "31")
        if advanced:
            if pos == len(targets) - 1:
                message = color("最後の質問です。q で終了、b で戻れます。", "32")
            else:
                pos += 1
    print(f"レビュー済み {len(store.human)}/{len(store.questions)}。続きは同じコマンドで再開できます。")


def status() -> None:
    store = Store()
    counts = {s: sum(1 for h in store.human.values() if h["status"] == s) for s in STATUSES}
    differ = [qid for qid, h in store.human.items() if store.ai.get(qid, {}).get("ai_status") not in (None, h["status"])]
    accepted = sum(1 for h in store.human.values() if h.get("source") == "ai-accepted")
    print(f"レビュー済み {len(store.human)}/{len(store.questions)}  {counts}")
    print(f"  AI の提案をそのまま採用: {accepted}、自分で判定: {len(store.human) - accepted}")
    print(f"  AI と異なる判定: {len(differ)} {' '.join(sorted(differ))}")
    pending = [q["qid"] for q in store.questions if q["qid"] not in store.human]
    if pending:
        print(f"  未判定: {len(pending)} {' '.join(pending[:20])}{' …' if len(pending) > 20 else ''}")


def apply(accept_ai_for_rest: bool) -> None:
    from scripts.review_sheet import import_

    store = Store()
    pending = [q["qid"] for q in store.questions if q["qid"] not in store.human]
    if pending and not accept_ai_for_rest:
        sys.exit(f"未判定の質問が {len(pending)} 問あります: {' '.join(pending[:20])}\n"
                 "すべて判定するか、残りに AI の提案を使う場合は --accept-ai-for-rest を付けてください。")
    reviews = []
    for q in store.questions:
        h = store.human.get(q["qid"])
        if h is None:
            ai = store.ai[q["qid"]]
            h = {"status": ai["ai_status"], "fixed_question": ai.get("ai_fixed_question", ""),
                 "fixed_answer": ai.get("ai_fixed_answer", ""), "fixed_evidence": ai.get("ai_fixed_evidence", ""),
                 "note": f"AI: {ai.get('ai_reason', '')}"}
        reviews.append({"qid": q["qid"], "status": h["status"], "fixed_question": h.get("fixed_question", ""),
                        "fixed_answer": h.get("fixed_answer", ""), "fixed_evidence": h.get("fixed_evidence", ""),
                        "note": h.get("note", "")})
    # 何度反映しても同じ結果になるよう、退避した元データに戻してから反映する
    orig = QA / "test.orig.jsonl"
    if orig.exists():
        shutil.copy(orig, QA / "test.jsonl")
    import_("test", reviews)


def main() -> None:
    parser = argparse.ArgumentParser(description="テスト用セットの対話型レビュー")
    parser.add_argument("command", nargs="?", default="review", choices=["review", "status", "apply"])
    parser.add_argument("--only", default="pending", choices=["pending", "all", "ai-fix", "ai-drop"],
                        help="レビュー対象（既定: 未判定のみ）")
    parser.add_argument("--start", help="この qid から始める（例: t050）")
    parser.add_argument("--accept-ai-for-rest", action="store_true", help="apply で未判定の質問に AI の提案を使う")
    args = parser.parse_args()
    if args.command == "status":
        status()
    elif args.command == "apply":
        apply(args.accept_ai_for_rest)
    else:
        run(args.only, args.start)


if __name__ == "__main__":
    main()
