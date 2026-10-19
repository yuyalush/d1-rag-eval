# Azure の使用料（rg-d1rag-eval、2026-10-16〜2026-10-20）

確定額は Cost Management Query API（ActualCost、請求通貨 JPY）、トークン数は Azure Monitor メトリックから `scripts/cost_report.py` で取得した。日付は UTC。

**確定額 127.2 JPY ＋ 未反映分の見積もり 1,641.3 JPY ＝ 約 1,768 JPY**

為替レートは Decision-1 の確定額から逆算した 1 USD ≒ 157.6 JPY。

## デプロイ × 日

未反映分は、メトリックのトークン数のうち請求に出ていない分に、確定分の実効単価（確定分がなければ一覧単価 × 為替）を掛けた見積もり。

| 日付 | デプロイ | 入力トークン | 出力トークン | 確定額（JPY） | 未反映の見積もり（JPY） |
| --- | --- | --- | --- | --- | --- |
| 2026-10-16 | decision-eval | 2,024,155 | 6,189 | 13.4 | 0.0 |
| 2026-10-19 | decision-eval | 3,754,324 | 11,781 | 0.0 | 24.8 |
| 2026-10-16 | embed | 2,790,666 | 0 | 8.8 | 0.0 |
| 2026-10-17 | embed | 1,777 | 0 | 0.0 | 0.0 |
| 2026-10-19 | embed | 98,843 | 0 | 0.0 | 0.3 |
| 2026-10-16 | gen | 545,599 | 13,571 | 10.5 | 0.0 |
| 2026-10-19 | gen | 3,294,296 | 89,897 | 5.2 | 59.8 |
| 2026-10-19 | gen-nano | 1,599,513 | 87,745 | 0.0 | 18.1 |
| 2026-10-19 | gen-phi | 1,448,660 | 387,880 | 0.0 | 35.5 |
| 2026-10-16 | judge | 372,065 | 29,068 | 89.3 | 0.2 |
| 2026-10-19 | judge | 5,529,031 | 651,584 | 0.0 | 1,502.6 |

## 確定分の実効単価

キャッシュ書き込みなど一覧単価にない料金を含めた、実際の 100 万トークンあたりの金額（JPY）。

| デプロイ | 入力 | 出力 | 一覧単価での入力 | 一覧単価での出力 |
| --- | --- | --- | --- | --- |
| decision-eval | 6.62 | — | 6.62 | — |
| embed | 3.15 | — | 3.15 | — |
| gen | 17.53 | 78.77 | 15.76 | 78.78 |
| judge | 178.70 | 789.74 | 157.55 | 787.75 |

## メーター別（確定分の期間合計）

| デプロイ | 種類 | メーター | トークン数 | 金額（JPY） |
| --- | --- | --- | --- | --- |
| decision-eval | 入力 | Microsoft Decision 1 1M Tokens | 2,023,954 | 13.39 |
| embed | 入力 | text-embedding-3-small-glbl Tokens | 2,792,443 | 8.80 |
| gen | キャッシュ書き込み | 6-luna ShortCo Cd Wr Std Gl 1M Tokens | 497,807 | 9.80 |
| gen | 入力 | 6-luna ShortCo Inp Std Gl 1M Tokens | 277,874 | 4.38 |
| gen | 出力 | 6-luna ShortCo Opt Std Gl 1M Tokens | 18,718 | 1.47 |
| gen | キャッシュ読み取り | 6-luna ShortCo Cd Inp Std Gl 1M Tokens | 36,600 | 0.06 |
| judge | キャッシュ書き込み（Flex） | 6-sol ShortCo Cd Wr Flex Gl 1M Tokens | 209,984 | 41.35 |
| judge | 入力（Flex） | 6-sol ShortCo Inp Flex Gl 1M Tokens | 158,220 | 24.93 |
| judge | 出力（Flex） | 6-sol ShortCo Opt Flex Gl 1M Tokens | 28,887 | 22.76 |
| judge | 出力 | 6-sol ShortCo Opt Std Gl 1M Tokens | 73 | 0.12 |
| judge | 入力 | 6-sol ShortCo Inp Std Gl 1M Tokens | 184 | 0.06 |
| judge | キャッシュ読み取り（Flex） | 6-sol ShortCo Cd Inp Flex Gl 1M Tokens | 3,111 | 0.05 |
