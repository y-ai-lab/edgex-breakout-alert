# 実時間Shadow台帳の更新間監査

`scripts/live_capture_integrity.py` は、作業環境に保存した2時点の読取専用API
snapshotを比較する。HTTP、署名、注文、DB変更、通知、昇格は実行しない。
GitHubにはコードと架空のテストデータだけを保存し、本番snapshot・価格・口座情報・
監査出力をアップロードしない。既存の戦略・protocol・評価器・週次workflowは変更しない。

```bash
python .github/scripts/live_capture_integrity.py \
  --before /workspace/local-audit/earlier \
  --after /workspace/local-audit/later \
  --endpoint pending \
  --output /workspace/local-audit/new-pending-continuity.json
```

同じ形で `--endpoint vwap` を使用できる。入力には、取得時刻・status・SHA256を持つ
`manifest.json` と全件表示の `pending.json` または `vwap.json` が必要。
API表示limitが全台帳を含まない場合は停止する。500件を超える台帳の取得方法は
この監査で補完せず、完全な読取手段を先に用意する。

初回捕捉起点・登録protocol/engine・分析fingerprint・候補の全固定フィールド、
既存約定価格/時刻、既存TP/SL/AMBIGUOUS/DATA_GAP等の終端レコードを保持する。
signal close+1ms、観測後の未開始足、モデル別の元の期限（比較対照2本、新案4本）、
固定週所属、重複/消失/過去への追加、cursor・MFE/MAEの逆行を検査する。
出力は新規ファイルにのみ書き、既存のraw・manifest・結果を上書きしない。
不一致は修復や有利な再取得で置換せず停止する。エラーに個別銘柄・価格は出さない。

MATCHは保存台帳の継続性の検査であり、公開価格の独立再照合、元のmetrics/stress/
資金制約の再現、収集coverage・事前観測証拠の検査を代替しない。
新規捕捉レコード数や既存レコードの前進数を、仮約定純増や独立トレード数と扱わない。
同じcohortの更新、replayと実時間捕捉を合算せず、監査による追加サンプルは常に0。
RはROIではなく、OPEN/不確定資金・ROI nullを変更しない。
初回起動後にまだfingerprintがない状態から初観測への移行だけは許容する。
