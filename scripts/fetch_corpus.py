"""コーパス取得：Microsoft Learn 日本語版の Azure Storage 系記事を Markdown で保存する。

各サービスの toc.json から記事一覧を作り、`Accept: text/markdown` で本文を取得して
data/raw/<service>__<slug>.md に保存する。取得済みの記事は飛ばすので中断・再開できる。
記事のメタデータ（URL、タイトル、更新日、翻訳種別、原文の Git URL）は data/raw/manifest.jsonl に残す。

元記事は MicrosoftDocs/azure-docs（CC BY 4.0）。日本語版は機械翻訳（ms.translationtype: MT）が中心。

    uv run python -m scripts.fetch_corpus --max-docs 200
"""

from __future__ import annotations

import argparse
import json
import re
import time

import httpx

from src.config import ROOT

BASE = "https://learn.microsoft.com/ja-jp/azure/storage"
SERVICES = ["blobs", "files", "queues", "tables"]
RAW_DIR = ROOT / "data/raw"
MANIFEST = RAW_DIR / "manifest.jsonl"

# SDK ごとのクイックスタートやコード中心の記事は、内容がほぼ同じでコードばかりなので除外する
EXCLUDE = re.compile(
    r"(dotnet|java|python|javascript|typescript|nodejs|-go$|-go-|cpp|ruby|php|powershell|-cli$|"
    r"quickstart|tutorial|sample|reference|release-notes|whats-new|faq|troubleshoot)",
    re.I,
)
HEADERS = {"Accept": "text/markdown", "User-Agent": "d1-rag-eval corpus fetcher (research)"}


def walk(items: list[dict], acc: list[str]) -> None:
    for item in items:
        href = item.get("href")
        if href:
            acc.append(href)
        walk(item.get("children", []), acc)


def list_articles(client: httpx.Client) -> list[tuple[str, str]]:
    articles: list[tuple[str, str]] = []
    seen: set[str] = set()
    for service in SERVICES:
        toc = client.get(f"{BASE}/{service}/toc.json").raise_for_status().json()
        hrefs: list[str] = []
        walk(toc.get("items", []), hrefs)
        for href in hrefs:
            slug = href.split("?")[0].split("#")[0].strip("./")
            # 同じサービス内の記事だけ（外部リンク・他サービスへのリンクを除く）
            if not slug or "/" in slug or href.startswith(("http", "/", "..")) or EXCLUDE.search(slug):
                continue
            key = f"{service}/{slug}"
            if key not in seen:
                seen.add(key)
                articles.append((service, slug))
    return articles


def front_matter(md: str) -> dict[str, str]:
    m = re.match(r"^---\n(.*?)\n---\n", md, re.S)
    meta: dict[str, str] = {}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line and not line.startswith(" "):
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
    return meta


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-docs", type=int, default=200)
    parser.add_argument("--interval", type=float, default=1.0, help="リクエスト間隔（秒）")
    args = parser.parse_args()

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    done = set()
    if MANIFEST.exists():
        done = {json.loads(line)["doc_id"] for line in MANIFEST.read_text(encoding="utf-8").splitlines()}

    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        articles = list_articles(client)[: args.max_docs]
        print(f"対象 {len(articles)} 記事（取得済み {len(done)}）")
        with MANIFEST.open("a", encoding="utf-8") as manifest:
            for i, (service, slug) in enumerate(articles, 1):
                doc_id = f"{service}__{slug}"
                if doc_id in done:
                    continue
                url = f"{BASE}/{service}/{slug}"
                r = client.get(url)
                if r.status_code != 200 or "markdown" not in r.headers.get("content-type", ""):
                    print(f"  skip {url} ({r.status_code})")
                    time.sleep(args.interval)
                    continue
                (RAW_DIR / f"{doc_id}.md").write_text(r.text, encoding="utf-8")
                meta = front_matter(r.text)
                manifest.write(
                    json.dumps(
                        {
                            "doc_id": doc_id,
                            "url": url,
                            "title": meta.get("title", "").removesuffix(" | Microsoft Learn"),
                            "ms_date": meta.get("ms.date"),
                            "translation": meta.get("ms.translationtype"),
                            "original": meta.get("original_content_git_url"),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                manifest.flush()
                if i % 20 == 0:
                    print(f"  {i}/{len(articles)}")
                time.sleep(args.interval)
    print("完了")


if __name__ == "__main__":
    main()
