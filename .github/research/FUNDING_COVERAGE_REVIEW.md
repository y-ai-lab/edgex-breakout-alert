# Fundingの履歴・費用・時系列の診断

2026-10-11。既閲覧の固定週9/26 03:00–10/3 03:00 UTCの説明分析。
[計画fc9f9453](https://github.com/y-ai-lab/edgex-breakout-alert/commit/fc9f94530ecc182cb98f53de714b62ced1307367)
を市場別Fundingと仮約定の照合前に保存した。新戦略の検証ではない。
元のRSI回復・pivot neckline・inside-barはすべてKILLを保持する。
利益の出た区分や銘柄だけを抽出せず、3案の全326候補を保存した。
同じ市場週の重複モデルなので、成績・決着を独立サンプルとして合算しない。

## 取得できた情報

元の仮約定がある全123市場（未決済も含む）について、署名不要の公開
`/api/v2/public/funding/getFundingRatePage`を取得した。
全ページの終了・HTTP・cursor・checksum・contract identityを確認。
対象週と24時間の先行履歴、元cutoffの清算時点だけを取得し、後続の結果期間は開いていない。
この取得時点では全123市場で4時間間隔・UTCに整列した清算記録が揃った。
別期間や他の契約に4時間間隔を仮定しない。
forecastを清算と混ぜず、欠損・重複・可変間隔・不正価格・未完了ページは判定を停止する。

[公式Funding説明](https://edgex-1.gitbook.io/edgeX-documentation/trading/funding-fees)に従い、
仮の1単位当たり`indexPrice × fundingRate`をLONGの支払額とし、SHORTは符号を逆にする。
負の費用は受取。LAST_PRICEやoraclePriceへ置き換えない。
公式説明の一律の周期ではなく、各履歴の`fundingRateIntervalMin`を検証して使った。
これは公開市場の過去清算情報であり、ユーザーの実際の支払・受取や板約定の証拠ではない。

## 結果：足内のFunding順序を確定できない

| 元モデル | 元決着 | Fundingを全て照合可能な仮決着 | 約定・出口との順序不明 | 元OPEN/AMBIGUOUS | Funding込み全体AvgR/PF |
| --- | ---: | ---: | ---: | ---: | --- |
| RSI回復 | 116 | 98 | 18 | 5/6 | null / null |
| pivot neckline | 148 | 114 | 34 | 4/2 | null / null |
| inside-bar | 40 | 32 | 8 | 3/2 | null / null |

市場履歴の欠損による停止は0市場だが、OHLC約定の時刻が正確という意味ではない。
元`filled_ms`は15分足の開始で、実際の仮約定接触はその足の途中かもしれない。
元`outcome_ms`は出口足の終わりで、TP/SL接触はその足の途中かもしれない。
約定足・出口足にFundingがあれば、先に約定／決済していたと推測しない。
出口足の開始・終了に一致する清算も保守的に順序不明とした。
確実な仮保有区間は`[filled_ms+15m, outcome_ms−15m)`だけ。
足内の順序不明60件を除外して244件の成績を成功例として表示しない。
全体を確認できないため、Funding込み全体AvgR/PF/net勝率はnullを維持する。
元のTP/SL決着そのものを不明に書き換えたのではなく、追加費用の順序不明を別に記録した。

確実な仮保有区間の部分費用はRSI +0.152862R、pivot −2.097808R、
inside −1.067492R（それぞれの元net stop-riskで測った単純合計）。
これは総Funding、全体期待値、口座損益ではない。正は支払、負は受取。
未確定の清算・未決済の費用を0に置き換えず、この部分値でKILLや条件を変更しない。
異なるモデルや口座へ合算しない。RをUSDC・口座ROIへ読み替えない。

## 判断と実装

`BLOCKED_EXACT_FUNDING_TIMING_DIAGNOSTIC_ONLY`を記録した。
元の手数料・滑り込み全体AvgRはRSI −0.1466、pivot −0.3311、inside −0.2500。
Fundingを追加すればこれらが勝てるという根拠はなく、元のKILLを維持する。
前回の正の部分区分も、Funding未確定のまま本番へ採用しない。
active volume/pending/VWAPの凍結ルールと封印期間を維持し、新たな仮説に差し替えていない。

研究用監査器と17件の時系列・品質テストを追加した。
欠損を0費用として扱うこと、約定・出口の前後を推測すること、
一部だけの結果を全体のFunding込み成績と表示することを防ぐ。
元全モデルのmetrics/stress/portfolios・原市場SHAを再現し、変更していない。
独立した標準ライブラリ実装で326候補・全123市場の照合が一致した。
新しい候補・約定・決着サンプルは0。これは収益性の追加検証期間ではない。

資金制約口座は再計算していない。Fundingは保有中の利用可能資金にも影響するため、
過去の決着利益から費用を引くだけでは連続口座ROIを作れない。
元の1万USDC研究口座、1%リスク・3件上限等を実運用policyへ置き換えない。
OPEN/AMBIGUOUS/DATA_GAPの不確かな資金を解放せず、ROIはnull。
実約定・資金・Fundingの時刻証跡が揃うまで、口座の利益として提示しない。

本番戦略、注文・署名・ON/OFF、通知、UI、DB、購読、捕捉起点は変更していない。
変更は`.github/`の研究ファイルのみ。本番サービスは再デプロイ対象外。
本番レスポンスは作業環境にのみ保持し、Gitへ出していない。
公開Fundingの取得本文も最初の時刻付き作業アーカイブを保持し、後の有利な結果へ差し替えない。
過去APIの改訂・OHLC仮約定・市場相関・未解決の偏りは残る。
利益保証や最適な手法が完成したという判断はしない。

[計画](funding_audit_plan.json) / [全体集計・元成績・証跡](funding_audit_latest.json)。
