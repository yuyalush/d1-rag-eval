# d1-rag-eval

検索結果を LLM に渡す前に Microsoft-Decision-1 で評価・判定する構成が、通常の RAG より回答品質を上げるかを検証するリポジトリです。

## 検証仮説

- H1：Decision-1 で無関係・矛盾したチャンクを除外すると、faithfulness が上がる。
- H2：根拠不十分と判定した場合に回答を控えさせると、ハルシネーションが減る。
- H3：判定処理の追加によるレイテンシとコストの増加は、品質向上に見合う。

## 比較する構成

| ID | 構成 | 検索件数 → LLM へ渡す件数 |
| --- | --- | --- |
| A | ベースライン RAG | 5 → 5 |
| B | A ＋ D1 関連性フィルタ（noul） | 10 → 最大 5 |
| C | B ＋ D1 根拠十分性ゲート（choice） | 10 → 最大 5、不十分なら回答控え |
| D | D1 リランク（score） | 20 → 上位 5 |
| E | 小型 LLM で関連性フィルタ | 10 → 最大 5 |

## セットアップ

前提：Python 3.11 以上、[uv](https://docs.astral.sh/uv/)、Azure CLI。

```bash
uv sync
cp .env.example .env   # infra/deploy.sh を使う場合は自動で埋まる
uv run pytest
```

## ディレクトリ構成

```text
infra/          Bicep とデプロイ／削除スクリプト
data/raw/       元文書（Git 管理外）
data/qa/        dev.jsonl / test.jsonl
src/            config, ingest, retriever, decision, generator, pipelines/, evaluate
config.yaml     構成ごとの k、閾値、質問文
config/models.yaml  選定モデル・バージョン・リージョン・単価・確認日（T01a）
results/        実行ログ（JSONL）と集計 CSV（Git 管理外）
scripts/        疎通確認など
tests/
```

## 使用モデル

T01a で確定後に `config/models.yaml` とこの節に記録します（モデル名・バージョン・リージョン・単価・確認日）。

## 後片付け

検証後は `infra/destroy.sh` でリソースグループごと削除し、予算アラートが消えたことを確認してください。
