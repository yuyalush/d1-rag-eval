# d1-rag-eval

検索結果を LLM に渡す前に Microsoft-Decision-1 で評価・判定する構成が、通常の RAG より回答品質を上げるかを検証したリポジトリです。

## 結果の要約

テスト用 96 問 × 5 構成 × 3 回（2026-10-19 実施、生成モデル gpt-6-luna）。詳細は [`docs/test-final_report.md`](docs/test-final_report.md)、生成モデルの差し替え比較は [`docs/gen_compare.md`](docs/gen_compare.md)。

| 構成 | 正答率 | Faithfulness | Context Precision | Context Recall | 1 問あたり費用 |
| --- | --- | --- | --- | --- | --- |
| A ベースライン | 0.906 | 0.966 | 0.173 | 0.743 | $0.0002 |
| B D1 関連性フィルタ | 0.915 | 0.961 | 0.444 | 0.815 | $0.0002 |
| C B＋十分性ゲート | 0.911 | 0.957 | 0.440 | 0.815 | $0.0002 |
| D D1 リランク | **0.938**（A との差が有意） | 0.974 | 0.205 | 0.872 | $0.0004 |
| E 汎用 LLM フィルタ | 0.898 | 0.971 | 0.436 | 0.754 | $0.0006 |

- Decision-1 はリランカー（構成 D）として使うと正答率が有意に上がる。効果は複数根拠の質問に集中（0.783 → 0.900）。
- フィルタ（B）とゲート（C）は gpt-6-luna では差が出ない。指示を守らない小型モデル（Phi-4-mini-instruct）では、通過 0 件なら回答を控える C が作り話を大きく減らす。
- 正答率と Faithfulness は LLM（gpt-6-sol）の採点で、人による採点の抜き取り確認はしていない。

## 検証仮説

- H1：Decision-1 で無関係・矛盾したチャンクを除外すると、faithfulness が上がる。
- H2：根拠不十分と判定した場合に回答を控えさせると、ハルシネーションが減る。
- H3：判定処理の追加によるレイテンシとコストの増加は、品質向上に見合う。

## 比較する構成

| ID | 構成 | 検索件数 → LLM へ渡す件数 |
| --- | --- | --- |
| A | ベースライン RAG | 5 → 5 |
| B | A ＋ D1 関連性フィルタ（noul、確率 0.8 以上） | 10 → 最大 5 |
| C | B ＋ D1 根拠十分性ゲート（choice） | 10 → 最大 5。通過 0 件か「不十分」なら回答を控える |
| D | D1 リランク（score、4 段階） | 20 → 上位 5 |
| E | 汎用 LLM（gpt-6-luna）で B と同じ関連性判定 | 10 → 最大 5 |

閾値・質問文などの設定は `config.yaml` にあります（開発用セットで調整済み）。

## 使用モデル

確認日：2026-10-16〜19。詳細は `config/models.yaml`。単価は Azure Retail Prices API（eastus2）の USD / 100 万トークン。

| デプロイ | モデル | バージョン | SKU・容量 | 入力 / 出力 | 用途 |
| --- | --- | --- | --- | --- | --- |
| decision-eval | Microsoft-Decision-1 | 1 | DataZoneStandard・60（= 60 リクエスト / 分） | 0.042 / 0 | 判定 |
| embed | text-embedding-3-small | 1 | GlobalStandard・200 | 0.02 / 0 | 埋め込み |
| gen | gpt-6-luna | 2026-09-22 | GlobalStandard・250 | 0.10 / 0.50 | 回答生成、構成 E |
| judge | gpt-6-sol | 2026-09-22 | GlobalStandard・250 | 1.00 / 5.00（Flex） | 質問生成、採点 |
| gen-nano | gpt-5-nano | 2025-08-07 | GlobalStandard・250 | 0.05 / 0.40 | 生成モデルの差し替え比較 |
| gen-phi | Phi-4-mini-instruct | 1 | GlobalStandard・20 | 0.075 / 0.30 | 生成モデルの差し替え比較 |

リージョン：Foundry は East US 2（Decision-1 は Japan East・Southeast Asia では未提供、GlobalStandard も未提供）。評価ランナーが Foundry と DB の両方を呼ぶ構成のため、DB（ローカル Docker、または Japan East の Azure PostgreSQL）と別リージョンでも問題ありません。

gpt-6 系は入力の一部が「キャッシュ書き込み」（入力の 1.25 倍）として課金されるため、実際の費用は上の単価で計算した値より数 % 高くなります（`docs/azure_cost.md`）。

## セットアップ

前提：Linux（WSL2 可）、[uv](https://docs.astral.sh/uv/)（Python 3.12 は `.python-version` で固定、uv が自動取得）、Linux 版 Azure CLI と Bicep（`az bicep install`）、Docker。Azure には Decision-1（DataZoneStandard）と GPT-6 系のクォータが必要です。

```bash
uv sync
uv run pytest

# WSL では Windows 版 az と設定が混ざらないように分ける（Windows 版 az では Python からトークンが取れない）
export AZURE_CONFIG_DIR="$HOME/.azure-wsl"
az login --use-device-code
az account set --subscription <サブスクリプション ID>

infra/deploy.sh what-if          # 変更内容の確認
infra/deploy.sh                  # Foundry ＋ デプロイ ＋ ロール ＋ 予算アラート（月 1 万円）。.env を書き出す
docker compose up -d             # ローカルの pgvector（db/schema.sql で初期化）
uv run python -m scripts.smoke_test   # 5 つの接続先の疎通確認
```

- `.env` は `.env.example` を元に `infra/deploy.sh` が書き出します。ローカル DB を使うときは `PG_PASSWORD` を設定し、空にすると Entra 認証（Azure PostgreSQL）になります。
- Azure PostgreSQL も作る場合は `DEPLOY_POSTGRES=true infra/deploy.sh`（常時稼働の固定費がかかります。今回の検証では作っていません）。
- PC を再起動すると pgvector のコンテナーは止まるので、`docker compose up -d` で起動し直してください。

## 実行手順

```bash
# 1. コーパス（Microsoft Learn 日本語版の Azure Storage 302 記事）
uv run python -m scripts.fetch_corpus --max-docs 400   # 既定の 200 では全件取れない
uv run python -m src.chunking                          # data/chunks.jsonl（約 6,100 チャンク）
uv run python -m src.ingest                            # 埋め込んで pgvector に投入（差分のみ）

# 2. 質問セット（作り直す場合のみ。リポジトリにはレビュー済みのものがある）
uv run python -m src.qa_generate
uv run python -m scripts.review_cli                    # AI の一次レビューを初期値に 1 問ずつ判定
uv run python -m scripts.review_cli apply              # data/qa/test.jsonl に反映

# 3. 開発用セットでのチューニングと通し実行（LLM 採点なし）
uv run python -m src.tune
uv run python -m src.evaluate --split dev --runs 1 --run-id dev-all

# 4. 最終評価（約 4 時間。Decision-1 の 60 リクエスト / 分の制限で決まる）と採点・集計
uv run python -m src.evaluate --split test --runs 3 --run-id test-final
uv run python -m src.score --run-id test-final --split test
uv run python -m src.significance --run-id test-final --split test
uv run python -m src.significance --run-id test-final --split test --baseline e_llm_filter
uv run python -m src.report --run-id test-final --split test

# 5. 生成モデルの差し替え比較（任意。検索と判定は test-final のログを再利用し、回答だけ作り直す）
uv run python -m src.regen --source test-final --target test-final-nano --deployment gen-nano --rpm 200 --workers 4
uv run python -m src.regen --source test-final --target test-final-phi --deployment gen-phi --rpm 7 --workers 4 --timeout 25
uv run python -m src.score --run-id test-final-nano --split test     # phi も同様
uv run python -m src.abstain_check --run-id test-final-nano          # 作り話と意味上の控えの分類
uv run python -m src.compare_gen

# 6. Azure の使用料（確定額は使用から 12〜24 時間で反映）
uv run python -m scripts.cost_report --from 2026-10-16 --to 2026-10-21
```

どの処理も中断して再実行すると続きから再開します。数時間かかる処理は `nohup setsid bash -c "..." > results/<name>.log 2>&1 &` のように端末から切り離して実行してください。

## データについて

- `data/qa/dev.jsonl`（開発用 49 問）と `data/qa/test.jsonl`（テスト用 96 問、人手レビュー済み）は、judge（gpt-6-sol）が生成した質問・模範解答と、根拠チャンクの ID を含みます。レビュー前のテスト用 100 問は `test.orig.jsonl`、AI の一次レビューと人の判定は `test_ai_review.jsonl`・`test_human_review.jsonl` です。
- **チャンク本文（`data/chunks.jsonl`）、取得した記事（`data/raw/`）、レビュー用 CSV（`data/qa/*.csv`）はリポジトリに含めていません。** いずれも Microsoft Learn の記事本文を含むためです。必要な場合は上の手順で手元に作成してください。
- 質問セットの根拠 ID（`c00118` など）は、検証時（2026-10-16 取得）のチャンクに対応します。記事は随時更新されるため、記事を取り直すとチャンクの分割や ID がずれ、Context Precision / Recall が正しく計算できなくなります。同じ質問セットで再現する場合は検証時の `data/chunks.jsonl` が必要です。手に入らない場合は、新しいチャンクで質問セットを作り直してください。
- 実行ログ（`results/`）もリポジトリに含めていません。集計結果は `docs/` にあります。

## ディレクトリ構成

```text
infra/              Bicep とデプロイ／削除スクリプト
db/                 pgvector のスキーマ
config.yaml         構成ごとの検索件数、閾値、Decision-1 の質問文、レート制限
config/models.yaml  モデル・バージョン・SKU・容量・単価・確認日
src/                チャンク化、取り込み、検索・判定・生成、構成 A〜E（pipelines/）、評価・採点・有意差・レポート
scripts/            記事の取得、疎通確認、質問セットのレビュー、使用料の取得
data/qa/            質問セット（dev.jsonl / test.jsonl）とレビュー記録
docs/               結果の表（最終評価、チューニング、生成モデル比較、使用料）
tests/              単体テスト
```

## 後片付け

```bash
infra/destroy.sh          # 確認のうえリソースグループを削除し、論理削除された Foundry を purge
docker compose down -v    # ローカル DB をデータごと削除する場合
```

リソースグループを削除すると Azure Monitor のメトリックも消えるため、`scripts/cost_report.py` での使用料の確認は削除前に行ってください。

## 出典

検証用コーパスと質問セットは、Microsoft Learn の次のドキュメントの日本語版（2026-10-16 取得）を元に作成しています。

- Azure Blob Storage・Azure Files・Azure Queue Storage・Azure Table Storage の各ドキュメント（[learn.microsoft.com/ja-jp/azure/storage/](https://learn.microsoft.com/ja-jp/azure/storage/)）
- 原文：[MicrosoftDocs/azure-docs](https://github.com/MicrosoftDocs/azure-docs)、© Microsoft Corporation、[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)
- 日本語版には機械翻訳（302 記事中 203 記事）と人手翻訳（99 記事）が含まれます。

加工内容：記事を見出し単位・約 500 トークンのチャンクに分割し、画像を削除してリンクを文字だけにしました。質問・模範解答はチャンクを元に LLM で生成し、人がレビューしています。`docs/test-final_report.md` の失敗例には、チャンク本文の冒頭（最大 200 文字）を引用しています。

Microsoft、Azure、Microsoft Learn は Microsoft グループの商標です。本リポジトリは Microsoft による公式のものではありません。

## ライセンス

このリポジトリのコード、質問セット、ドキュメントは [クリエイティブ・コモンズ 表示 4.0 国際（CC BY 4.0）](https://creativecommons.org/licenses/by/4.0/deed.ja) で提供します（全文は [`LICENSE`](LICENSE)）。質問セットなど Microsoft Learn を元にした部分は、上の「出典」の表示もあわせて行ってください。
