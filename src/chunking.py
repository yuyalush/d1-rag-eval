"""T05 チャンク化：data/raw の Markdown を見出し付き約 500 トークンに分割し data/chunks.jsonl へ出力する。

- トークン数は text-embedding-3-small と同じ cl100k_base で数える
- 見出し（## / ###）ごとに区切り、長い節は段落単位で約 500 トークン・重なり 50 トークンに分ける
- 各チャンクの先頭に「記事タイトル > 見出し」を付ける

    uv run python -m src.chunking
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from dataclasses import asdict, dataclass

import tiktoken

from src.config import ROOT

RAW_DIR = ROOT / "data/raw"
OUT = ROOT / "data/chunks.jsonl"
CHUNK_TOKENS = 500
OVERLAP_TOKENS = 50
MIN_TOKENS = 40  # これ未満の節は次の節と結合する

enc = tiktoken.get_encoding("cl100k_base")


def n_tokens(text: str) -> int:
    return len(enc.encode(text))


REPLACEMENT_CHAR = "\ufffd"


def decode_clean(ids: list[int]) -> str:
    """トークン列を文字列に戻す。

    cl100k_base では日本語の 1 文字が複数トークンに分かれることがあり、トークン単位で切り出すと
    両端の文字が途中で切れて U+FFFD になる。両端の壊れた文字だけを落とす。
    落とした文字は、重なりで隣のチャンクにそのまま含まれるので内容は失われない。
    """
    return enc.decode(ids).strip(REPLACEMENT_CHAR)


def find_broken(chunks: list["Chunk"]) -> list[str]:
    """U+FFFD を含むチャンクの ID（元の Markdown には含まれないので、あれば分割処理の不具合）。"""
    return [c.id for c in chunks if REPLACEMENT_CHAR in c.text]


@dataclass
class Chunk:
    id: str
    doc_id: str
    title: str
    heading: str
    text: str
    tokens: int


def clean(md: str) -> tuple[str, str]:
    """front matter を外し、タイトルと本文を返す。"""
    title = ""
    m = re.match(r"^---\n(.*?)\n---\n", md, re.S)
    if m:
        t = re.search(r"^title:\s*(.+)$", m.group(1), re.M)
        if t:
            title = t.group(1).strip().removesuffix(" | Microsoft Learn")
        md = md[m.end():]
    md = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md)            # 画像
    md = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", md)         # リンクは文字だけ残す
    md = re.sub(r"<!--.*?-->", "", md, flags=re.S)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return title, md.strip()


def sections(body: str) -> list[tuple[str, str]]:
    """## / ### 見出しで区切り、(見出しパス, 本文) のリストを返す。"""
    out: list[tuple[str, str]] = []
    h2 = h3 = ""
    buf: list[str] = []

    def flush() -> None:
        text = "\n".join(buf).strip()
        if text:
            out.append((" > ".join(h for h in (h2, h3) if h), text))
        buf.clear()

    for line in body.splitlines():
        if line.startswith("# "):
            continue  # 記事タイトル（front matter の title を使う）
        if line.startswith("## "):
            flush()
            h2, h3 = line[3:].strip(), ""
        elif line.startswith("### "):
            flush()
            h3 = line[4:].strip()
        else:
            buf.append(line)
    flush()
    return out


def split_long(text: str, budget: int) -> list[str]:
    """段落単位で budget トークン以内にまとめ、前のチャンク末尾を OVERLAP_TOKENS 分重ねる。"""
    paras = [p for p in text.split("\n\n") if p.strip()]
    pieces: list[str] = []
    cur: list[str] = []
    cur_tok = 0
    for p in paras:
        pt = n_tokens(p)
        if pt > budget:  # 1 段落が長すぎる場合はトークンで切る
            if cur:
                pieces.append("\n\n".join(cur))
                cur, cur_tok = [], 0
            ids = enc.encode(p)
            step = budget - OVERLAP_TOKENS
            pieces.extend(decode_clean(ids[i : i + budget]) for i in range(0, len(ids), step))
            continue
        if cur and cur_tok + pt > budget:
            pieces.append("\n\n".join(cur))
            tail = decode_clean(enc.encode(pieces[-1])[-OVERLAP_TOKENS:])
            cur, cur_tok = [tail], OVERLAP_TOKENS
        cur.append(p)
        cur_tok += pt
    if cur:
        pieces.append("\n\n".join(cur))
    return pieces


def chunk_document(doc_id: str, md: str, start: int) -> list[Chunk]:
    title, body = clean(md)
    chunks: list[Chunk] = []
    pending_heading, pending_text = "", ""
    for heading, text in sections(body):
        if pending_text:  # 短すぎた前の節と結合
            heading, text = pending_heading or heading, f"{pending_text}\n\n{text}"
            pending_heading, pending_text = "", ""
        if n_tokens(text) < MIN_TOKENS:
            pending_heading, pending_text = heading, text
            continue
        prefix = f"{title} > {heading}\n\n" if heading else f"{title}\n\n"
        for piece in split_long(text, CHUNK_TOKENS - n_tokens(prefix)):
            full = prefix + piece
            chunks.append(Chunk(f"c{start + len(chunks):05d}", doc_id, title, heading, full, n_tokens(full)))
    if pending_text:
        prefix = f"{title} > {pending_heading}\n\n" if pending_heading else f"{title}\n\n"
        full = prefix + pending_text
        chunks.append(Chunk(f"c{start + len(chunks):05d}", doc_id, title, pending_heading, full, n_tokens(full)))
    return chunks


def select_documents(target: int) -> list[str]:
    """目次順（manifest の順）に、サービスごとの比率を保って合計 target チャンク程度になる文書を選ぶ。"""
    doc_ids = [json.loads(line)["doc_id"] for line in (RAW_DIR / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]
    counts = {d: len(chunk_document(d, (RAW_DIR / f"{d}.md").read_text(encoding="utf-8"), 0)) for d in doc_ids}
    if target <= 0:
        return doc_ids
    total = sum(counts.values())
    service_total: dict[str, int] = {}
    for d, n in counts.items():
        service_total[d.split("__")[0]] = service_total.get(d.split("__")[0], 0) + n
    quota = {s: target * n / total for s, n in service_total.items()}
    used: dict[str, int] = {s: 0 for s in quota}
    selected = []
    for d in doc_ids:
        s = d.split("__")[0]
        if used[s] < quota[s]:
            selected.append(d)
            used[s] += counts[d]
    return selected


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-chunks", type=int, default=0, help="0（既定）で全文書を使う。正の値で目次順にサービス比率を保って絞る")
    args = parser.parse_args()

    files = [RAW_DIR / f"{d}.md" for d in select_documents(args.target_chunks)]
    all_chunks: list[Chunk] = []
    for f in files:
        all_chunks.extend(chunk_document(f.stem, f.read_text(encoding="utf-8"), len(all_chunks)))
    broken = find_broken(all_chunks)
    if broken:
        raise SystemExit(f"壊れた文字（U+FFFD）を含むチャンクが {len(broken)} 件あります: {broken[:10]}。chunks.jsonl は更新しません")
    with OUT.open("w", encoding="utf-8") as out:
        for c in all_chunks:
            out.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")

    toks = [c.tokens for c in all_chunks]
    q = statistics.quantiles(toks, n=10)
    print(f"文書 {len(files)} 件 → チャンク {len(all_chunks)} 件、合計 {sum(toks):,} トークン")
    print(f"トークン数 min={min(toks)} p10={q[0]:.0f} median={statistics.median(toks):.0f} p90={q[-1]:.0f} max={max(toks)}")
    per_service: dict[str, int] = {}
    for c in all_chunks:
        per_service[c.doc_id.split("__")[0]] = per_service.get(c.doc_id.split("__")[0], 0) + 1
    print("サービス別:", per_service)


if __name__ == "__main__":
    main()
