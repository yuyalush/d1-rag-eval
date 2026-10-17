from src.chunking import CHUNK_TOKENS, chunk_document, find_broken, n_tokens

MD = """---
title: BLOB データに関するアクセス層 - Azure Storage | Microsoft Learn
ms.date: 2026-04-02
---
# BLOB データに関するアクセス層

概要の段落です。![図](img.png) 詳細は [こちら](other-page) を参照してください。

## オンライン アクセス層

""" + "\n\n".join(f"ホット層とクール層の説明 {i}。" * 20 for i in range(30)) + """

## アーカイブ アクセス層

短い節。
"""


def test_chunks_have_title_heading_and_fit_budget():
    chunks = chunk_document("blobs__access-tiers-overview", MD, 0)
    assert len(chunks) > 2
    assert chunks[0].id == "c00000"
    assert all(c.text.startswith("BLOB データに関するアクセス層 - Azure Storage") for c in chunks)
    assert any("オンライン アクセス層" in c.heading for c in chunks)
    # 重なり分と区切り文字の誤差を許容する
    assert all(c.tokens <= CHUNK_TOKENS + 60 for c in chunks)
    assert all(c.tokens == n_tokens(c.text) for c in chunks)


def test_japanese_split_has_no_broken_characters():
    # 改行のない長い日本語の段落（トークン単位での分割が必ず起きる）と、重なりが発生する複数段落
    long_para = "アーカイブ層のデータは少なくとも百八十日間保持する必要があり、早期削除料金が発生します。" * 60
    paras = "\n\n".join(f"段落{i}：コールド層はクール層よりストレージ料金が安く、アクセス料金が高い。" * 8 for i in range(20))
    md = f"---\ntitle: 日本語テスト\n---\n## 長い段落\n\n{long_para}\n\n## 複数段落\n\n{paras}\n"
    chunks = chunk_document("ja", md, 0)
    assert len(chunks) > 3
    assert find_broken(chunks) == []
    # 落とした端の文字は重なりで隣のチャンクに残るので、本文の各段落がどこかのチャンクに含まれる
    body = "".join(c.text for c in chunks)
    assert "早期削除料金が発生します" in body
    assert all(f"段落{i}" in body for i in range(20))


def test_links_and_images_are_stripped():
    text = " ".join(c.text for c in chunk_document("d", MD, 0))
    assert "img.png" not in text
    assert "other-page" not in text
    assert "こちら" in text
