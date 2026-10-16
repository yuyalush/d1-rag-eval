from src.chunking import CHUNK_TOKENS, chunk_document, n_tokens

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


def test_links_and_images_are_stripped():
    text = " ".join(c.text for c in chunk_document("d", MD, 0))
    assert "img.png" not in text
    assert "other-page" not in text
    assert "こちら" in text
