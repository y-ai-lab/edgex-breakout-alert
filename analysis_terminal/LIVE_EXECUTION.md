# EdgeX V2 API execution — v19.0.41

ユーザーの自動取引実装依頼に対応した、分析サービス内の任意機能です。既定は **OFF**。
本リリースで実注文は送信していません。SDKの認証・署名・注文本文と異常系を
模擬交換所で検証しましたが、本人の認証接続、実約定品質、利益は未検証です。
研究結果から本番戦略を変更・昇格する機能はありません。

## 対象と接続

- 現行4H/15M戦略の **READYのみ**。Shadow、BTC研究モデル、NEARは発注しません。
- 起動後にオペレーターがarmし、その後に確定した15M確認足だけを対象にします。
  過去のREADY setupは既存紙取引台帳から除外します。同じsetupは再発注しません。
- 最新スキャンと確定足から120秒未満、口座照合から5秒未満のときだけ検討します。
- 公式SDKの固定ソースcommit `487274d97cd1b77e05a759dbdf75e191582adefc`、
  V2 HMAC認証 + EIP-712署名。アーカイブSHA256も固定します。
  HMACの鍵はAPI secretをBase64エンコードしたbytesです。公式Go SDKとも照合し、
  独立した署名計算テストを通します。API secretを先にデコードしません。
  接続先は `https://edgex-prod-v2.edgex.exchange` 固定です。
- EdgeX API Managementの **Perps V2 / SDK Signer** を使用します。
  ウォレットの秘密鍵は使用しません。鍵をブラウザ、Git、ChatGPTへ入力しないでください。
  取引権限が必要です。出金・送金APIはこの実装にありません。

## オペレーター設定

Railwayの **分析サービスだけ** に秘密変数を設定します。通知サービスと分離します。

| 変数 | 意味 |
| --- | --- |
| `EDGEX_EXEC_MODE` | `OFF`（既定） / `READ_ONLY` / `LIVE` |
| `EDGEX_EXEC_ACCOUNT_ID` | 専用のPerps V2口座ID |
| `EDGEX_EXEC_API_KEY` | SDK SignerのAPI key |
| `EDGEX_EXEC_API_SECRET` | API secret |
| `EDGEX_EXEC_API_PASSPHRASE` | API passphrase |
| `EDGEX_EXEC_SIGNER_KEY` | SDK Signer private key。LIVE時に必須 |
| `EDGEX_EXEC_RISK_PCT` | 1取引の口座リスク%。明示必須、0超〜3以下 |
| `EDGEX_EXEC_MAX_RISK_USDC` | 費用込み想定SL損失の正数上限、または明示的な `ACCOUNT_RISK_PCT` |
| `EDGEX_EXEC_MAX_NOTIONAL_USDC` | 想定元本の正数上限、または明示的な `ACCOUNT_EQUITY`。残高・利用可能額も適用 |
| `EDGEX_EXEC_DAILY_LOSS_USDC` | 日次equity減少の正数停止額、または明示的な `DISABLED` |
| `EDGEX_EXEC_SLIPPAGE_BPS` | 片道想定スリッページ。既定2、0〜25 |
| `EDGEX_EXEC_FEE_BPS` | 片道費用予算。既定5、契約の標準fee以上が必要 |
| `EDGEX_EXEC_CONTROL_REQUEST` | 任意の管理操作。`check:<UUIDv4>` / `arm:<UUIDv4>` / `pause:<UUIDv4>` |

正数の固定上限と、v41の残高連動指定を選べます。資金配分を明示せずLIVEにはできません。
同時保有は1ポジション、元本は口座equity以下です。SLを近づける方向で丸め、
グリッド調整・現在価格でもgross RR >= 2を要求します。これはnet RR 2の保証ではありません。
費用・スリッページ予算は仮定であり、ギャップ時の損失上限を保証しません。

### 最新の取引口座残高を使う明示的な資金方針（v19.0.41）

`MAX_RISK_USDC=ACCOUNT_RISK_PCT` は、各注文直前のAPI口座 `totalEquity`
× `RISK_PCT / 100` を、SL到達時の往復費用と想定スリッページを含む損失予算にします。
`MAX_NOTIONAL_USDC=ACCOUNT_EQUITY` は、同じAPIの `totalEquity` と
`availableAmount` の小さい方まで元本を制限します。丸め、最小注文量、取引所の
サイズ上限も適用されるため、必ず指定リスク率いっぱいの注文になるわけではありません。
これはEdgeX Perps V2のUSDC取引口座の資本で、チェーン上の別ウォレット残高や
EDGEトークンの数量を元本として読み替えません。入金・送金機能は追加しません。

`DAILY_LOSS_USDC=DISABLED` のときだけ、arm時と通常運用時の日次損失停止を無効にします。
空欄・0・負数・不明な文字列を無効化として扱いません。正数の場合は従来通り
日次上限と日初equityの3%の小さい方を使い、再armでも日次基準をリセットしません。
日次停止を無効にしても、未照合注文・外部の建玉・保護注文異常・残高不足・
通信エラー等の停止は維持します。損失が続く日の累積損失に日次上限はありません。

公開ステータスの `live_configuration_errors` は、READ_ONLYでもLIVEに必要な
署名鍵の形式と資金方針を検証します。鍵・残高・資金設定値は返しません。
形式検査と読み取り接続は、署名者の取引権限や実注文受付の証明ではありません。
資金方針は従来のfingerprintに含まれ、変更しても自動armしません。

先に `READ_ONLY` で接続確認し、専用口座が空であることを確認します。
Railwayコンテナ内の `/app` で、既存の `/data/analysis_terminal.db` を用います。

```bash
python -m analysis_terminal.live_execution_control check
python -m analysis_terminal.live_execution_control status
```

`check` はLIVE設定でも読取専用です。CLIは鍵を引数に受け取らず、応答本文を出しません。
契約メタデータ・口座・全ページの未処理注文を確認し、5秒以上古い口座照合は拒否します。
これはHMAC認証と読み取りの検証で、取引署名者の権限や実注文の受付・約定の証明ではありません。
LIVE設定後も自動ではarmされません。空の専用口座・未管理注文なし・未解決台帳なしでのみ
次の操作が可能です。

```bash
python -m analysis_terminal.live_execution_control arm
python -m analysis_terminal.live_execution_control pause
```

`pause` は新規注文を停止します。LIVEのままなら既存ポジションの照合と保護管理を継続します。
`OFF`、`READ_ONLY`への切替、口座・リスク方針の変更は既存管理を停止するため、
保有中に行わないでください。変更時は専用口座と台帳を手動照合する必要があります。
armは日次損失基準をリセットしません。日次基準はJST日付の最初の接続成功時equityで、
再接続前の正確な日初残高ではありません。停止幅は明示上限と日初equity 3%の小さい方です。
入出金・手動注文は停止基準や照合を乱すため、同じ口座を併用しないでください。

### Railway設定からの管理操作（v19.0.40）

SSHできない場合も、Railwayの分析サービス変数だけで同じ管理操作を実行できます。
ローカルでDB・認証情報なしに操作IDを生成できます。

```bash
python -m analysis_terminal.live_execution_control request --operation check
```

出力された `EDGEX_EXEC_CONTROL_REQUEST=check:<UUID>` を分析サービスに設定し、
READ_ONLYでデプロイします。`GET /api/live-execution` の `operator_control.status=DONE` と
`READ_ONLY`、最新 `last_preflight_ms` を確認します。REFUSEDなら新規は停止したままです。
鍵はRailwayの秘密変数だけに保存し、操作IDには鍵・口座ID・金額を含めません。

開始は、認証情報・署名者鍵・4つの明示的な資金設定をすべて指定したLIVEモードで、
**新しいUUIDの `arm` 操作**を指定します。設定済みという理由だけで自動armはしません。
停止は新しいUUIDの `pause` 操作です。LIVEのまま停止すれば既存の保護管理は継続します。
管理用の公開POST APIはありません。

各UUIDはSQLiteの追加台帳で一度だけ消費します。同じ操作が環境変数に残っていても、
再デプロイ・異常停止後に再armしません。失敗した操作を、後の設定修正で勝手に再試行しません。
同じUUIDで操作を変えると停止します。PROCESSINGのまま中断した操作も再実行せず停止し、
オペレーターによる照合と新しいUUIDを必要とします。開始の照合中にpauseされた場合は開始を拒否します。
公開ステータスには操作種別・結果・時刻だけを返し、UUIDや認証応答を返しません。

## 注文・復旧の限界

固定client order IDと送信前SQLiteコミットにより、HTTP応答喪失・再起動時に
同じ注文を再送しません。入口は価格上限付きLIMIT IOCで、取引所の確定した
約定サイズ・価格・費用を照合してから、reduce-only SL、続いてTPを発注します。
SL/TPはLAST_PRICEを基準とする条件付き市場注文です。

**入口と保護注文は原子的な同時注文ではなく、短い未保護期間があります。**
条件注文の登録が不明、拒否、内容不一致、費用込みリスク超過等なら、新規を停止し
reduce-only緊急決済を一度だけ試みます。通信断、未確定IOC、部分決済、ギャップ、
取引所の拒否では完全決済を保証できません。ACKだけで約定・flatと見なしません。
不明な注文は勝手に再送・台帳削除・資金解放せず、オペレーターが取引所で確認します。
入口IOCの状態が確定しない場合もキャンセルのACKだけを根拠に保護サイズを推測しません。

SL/TPの期限は21日です。期限直前まで残る場合は停止して緊急決済を試みます。
close後は所有する残存出口だけを取消し、取引所の約定とflatの一致でCLOSEDにします。
記録する取引損益は実価格・実手数料による **funding控除前** で、口座ROIではありません。

v39の3テーブルとv40の操作台帳は追加移行で、既存DB・Push・模擬口座・Shadow台帳を変更しません。
公開 `GET /api/live-execution` は接続・停止状態の集計だけを返し、鍵・口座情報・注文価格・
注文IDは返しません。公開arm・発注APIはありません。画面の既存API検証に状態を表示します。
新タブやShadow通知は追加しません。

同一DBのfile lockで実行workerを1つに限定します。Railwayは **単一replica** で運用してください。
別DB・別volume・別サービスの同じ口座には分散lockが効きません。

公式資料: [SDK](https://github.com/edgex-Tech/edgex-python-sdk)、
[V2注文API](https://edgex-1.gitbook.io/edgex-documentation/api-v2/private-api/order-api)、
[認証](https://edgex-1.gitbook.io/edgex-documentation/api-v2/authentication)。
