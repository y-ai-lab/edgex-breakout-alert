# EdgeX V2 API execution — v19.0.43

ユーザーの自動取引実装依頼に対応した、分析サービス内の任意機能です。既定は **OFF**。
SDKの認証・署名・注文本文と異常系を模擬交換所で検証します。
読み取り接続と、署名者の取引権限・実注文受付・実約定品質・利益の検証は区別します。
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
| `EDGEX_EXEC_ACCOUNT_POLICY` | `DEDICATED`（既定） / 明示的な `COEXISTING_CONTRACTS` |
| `EDGEX_EXEC_ACCOUNT_ID` | Perps V2口座ID |
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
このアプリの同時保有は1ポジション、各注文の元本は口座equity・利用可能額以下です。SLを近づける方向で丸め、
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
日次停止を無効にしても、未照合注文・所有権不一致・保護注文異常・残高不足・
通信エラー等の停止は維持します。損失が続く日の累積損失に日次上限はありません。

公開ステータスの `live_configuration_errors` は、READ_ONLYでもLIVEに必要な
署名鍵の形式と資金方針を検証します。鍵・残高・資金設定値は返しません。
形式検査と読み取り接続は、署名者の取引権限や実注文受付の証明ではありません。
資金方針は従来のfingerprintに含まれ、変更しても自動armしません。

### 開始拒否の読み取り診断（v19.0.42）

既定の `DEDICATED` では、`check` は `preflight_arm_blockers` に専用口座の開始を妨げる理由だけを保存します。
`EXISTING_ACCOUNT_POSITION` は口座の既存建玉、`ACTIVE_EXCHANGE_ORDERS` は
取引所の未処理注文、`UNRESOLVED_EXECUTION_LEDGER` はこのアプリの未解決台帳です。
銘柄・数量・残高・注文ID・秘密情報は公開レスポンスに含めません。
armも同じ条件を使い、条件を緩和したり既存注文を取り消したりしません。

診断は `last_preflight_ms` 時点の観測であり、30秒以内のものだけ
`preflight_arm_blockers_current=true` とします。旧DBや失敗した新しいcheckでは
理由は `null` で、不明を「開始可能」と扱いません。空の配列も、署名権限の証明や
自動armの許可ではありません。原因が解消しても消費済みarmを自動再試行しません。

先に `READ_ONLY` で接続確認し、設定した口座方針の開始条件を確認します。
Railwayコンテナ内の `/app` で、既存の `/data/analysis_terminal.db` を用います。

```bash
python -m analysis_terminal.live_execution_control check
python -m analysis_terminal.live_execution_control status
```

`check` はLIVE設定でも読取専用です。CLIは鍵を引数に受け取らず、応答本文を出しません。
契約メタデータ・口座・全ページの未処理注文を確認し、5秒以上古い口座照合は拒否します。
これはHMAC認証と読み取りの検証で、取引署名者の権限や実注文の受付・約定の証明ではありません。
LIVE設定後も自動ではarmされません。既定では空の専用口座・未管理注文なし・
未解決台帳なしを要求します。併用方針は下記の契約別制限を適用します。

```bash
python -m analysis_terminal.live_execution_control arm
python -m analysis_terminal.live_execution_control pause
```

`pause` は新規注文を停止します。LIVEのままなら既存ポジションの照合と保護管理を継続します。
`OFF`、`READ_ONLY`への切替、口座・リスク方針の変更は既存管理を停止するため、
保有中に行わないでください。変更時は専用口座と台帳を手動照合する必要があります。
armは日次損失基準をリセットしません。日次基準はJST日付の最初の接続成功時equityで、
再接続前の正確な日初残高ではありません。停止幅は明示上限と日初equity 3%の小さい方です。
入出金・手動注文はequity基準を変えます。DEDICATEDでは同じ口座を併用しないでください。
COEXISTING_CONTRACTSでも、自動取引が管理中の同じ銘柄を手動操作しないでください。

### 既存建玉との契約別併用（v19.0.43）

ユーザーの「既存の建玉があってもできるように」という指示に対応し、
`EDGEX_EXEC_ACCOUNT_POLICY=COEXISTING_CONTRACTS` を明示した場合だけ、
別銘柄の既存建玉・未処理注文を維持してarmできます。設定変更はfingerprintで検出し、
新しいUUIDのarmを必要とします。既定DEDICATEDの条件・過去の注文IDは維持します。
未解決の自動取引台帳、利用可能担保なし、不明な注文のcontractIdは開始を拒否します。

- 新規対象のcontractに建玉または未処理注文があれば見送ります。方向を問わず、
  手動のreduce-only注文も対象です。同銘柄への積増し・反転・ヘッジはしません。
- 別銘柄のREADYを検討し、注文直前にも口座・全ページの注文を読み直します。
  最新equityからの費用込み3%等の損失予算、最新availableAmount、元本上限を再確認し、
  凍結した注文量を満たせなくなった場合は送信しません。既存建玉の証拠金拘束は
  取引所のavailableAmountに反映されます。既存建玉のSL・総口座損失は管理しません。
- SL/TPは所有約定量に限定したreduce-only条件注文です。
  `isPositionTpsl=false` をSDK本文に指定し、口座の全ポジションを対象とする設定を避けます。
  応答の数量・条件・所有IDを照合します。公式の注文照会schemaはこのflagを省略しますが、
  明示的にtrueが返れば拒否します。
- 保護・緊急決済前にも所有数量を再照合します。手動の同銘柄注文や説明不能なnet数量変更を
  検出したらOWNERSHIP_CONFLICTとして隔離し、新規を停止します。所有するSL/TPだけを
  一度取り消し、手動の建玉・注文は決済・取消ししません。不明な資金を解放しません。
  手動変更が消えても隔離を自動解除しません。通信断で所有数量が確認できなければ、
  想定数量で緊急決済せずOWNERSHIP_UNVERIFIEDとして保留します。
- 取引所の口座照会と注文は原子的ではありません。照会直後の手動注文との競合、
  照会間で同量の建玉を閉じて開き直す変更等は完全には検出できません。
  自動管理中の銘柄は他のクライアントで操作しないでください。損失3%は費用・slippage仮定の
  予算であり、ギャップや手動建玉を含めた実損失の保証ではありません。

旧DBは既存JSON台帳を保持し、新しいintentだけにisolated_contractを保存します。
本番READY条件、Shadow、通知、UI、模擬口座は変更しません。テスト注文の実送信は行いません。

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
無認証の管理POST APIはありません。v19.0.50から専用の元登録端末認証による
新規エントリーON/OFFを追加しました（[制御仕様](EXECUTION_CONTROLS.md)）。

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

併用方針の `isPositionTpsl=false` では建玉欄にSL/TPが表示されない場合があるため、
取引所の条件注文一覧で確認します。建玉欄の空欄だけで未登録と判断しません。
同銘柄に手動SL/TPを追加・変更する操作も外部注文との競合になり、自動管理は隔離されます。
隔離時はボット自身の保護注文の取消を試み、手動注文の管理は引き継ぎません。
手動で設定したSL/TPの現在の有効性は取引所で確認してください。照合なしの再armは行いません。

v19.0.53では個別照会の注文IDを作成応答と突合し、両方のSL/TPが未約定注文一覧にも
同じID・銘柄・方向・種類・数量で `UNTRIGGERED` として存在することを確認してから
`PROTECTED` を記録します。以後の照合でも確認し、欠損・重複・不一致・照会失敗を
成功扱いにしません。異常時は従来の新規停止と、所有確認できる残数量だけの一度限りの
reduce-only緊急決済へ進みます。所有不明の建玉・手動注文は操作しません。
一覧反映遅延でも未確認として扱うため、これは注文の原子性やSL約定価格の保証ではありません。

登録端末の口座閲覧認証で `GET /api/account/conditional-orders` を利用できます。
APIタブの「SL/TPを確認（読取専用）」は、署名鍵を持たない読取クライアントで
取引所の未約定一覧を全ページ取得し、条件注文だけを表示します。未取得・不完全・古い結果を
0件と扱わず、30秒で非表示にします。口座ID・注文ID・認証情報・未加工レスポンスは返しません。
注文の存在から建玉全体が保護されたと推定せず、建玉と注文は別時点の取得であることを表示します。
この表示は注文追加・変更・取消・再armを実行しません。

**入口と保護注文は原子的な同時注文ではなく、短い未保護期間があります。**
条件注文の登録が不明、拒否、内容不一致、費用込みリスク超過等なら、新規を停止し
reduce-only緊急決済を一度だけ試みます。併用時は所有する残数量の新しい証拠がある場合だけです。
通信断、未確定IOC、部分決済、ギャップ、
取引所の拒否では完全決済を保証できません。ACKだけで約定・flatと見なしません。
不明な注文は勝手に再送・台帳削除・資金解放せず、オペレーターが取引所で確認します。
入口IOCの状態が確定しない場合もキャンセルのACKだけを根拠に保護サイズを推測しません。

SL/TPの期限は21日です。期限直前まで残る場合は停止して緊急決済を試みます。
close後は所有する残存出口だけを取消し、取引所の約定とflatの一致でCLOSEDにします。
記録する取引損益は実価格・実手数料による **funding控除前** で、口座ROIではありません。

v39の3テーブルとv40の操作台帳は追加移行で、既存DB・Push・模擬口座・Shadow台帳を変更しません。
公開 `GET /api/live-execution` は接続・停止状態の集計だけを返し、鍵・口座情報・注文価格・
注文IDは返しません。無認証arm・任意発注APIはありません。画面の既存API検証に状態を表示します。
新タブやShadow通知は追加しません。

同一DBのfile lockで実行workerを1つに限定します。Railwayは **単一replica** で運用してください。
別DB・別volume・別サービスの同じ口座には分散lockが効きません。

公式資料: [SDK](https://github.com/edgex-Tech/edgex-python-sdk)、
[V2注文API](https://edgex-1.gitbook.io/edgex-documentation/api-v2/private-api/order-api)、
[認証](https://edgex-1.gitbook.io/edgex-documentation/api-v2/authentication)。
