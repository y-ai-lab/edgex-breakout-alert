"""Fixed public explanations of skipped entries; no prices, accounts or orders."""

SKIP_LABELS = {
    "PRE_ARM_BASELINE": "有効化前のsetupを基準記録に登録（新規注文なし）",
    "CURRENT_READY_REQUIRED": "READY・確認・リテストまたはsetupの整合性が未確認",
    "CONTRACT_MISMATCH": "分析銘柄と取引所の契約情報が一致しない",
    "CONTRACT_DISABLED": "取引所で新規建玉が無効",
    "STALE_OR_PRE_ARM_SIGNAL": "確認足から120秒以上経過・時刻不整合・有効化前のシグナル",
    "STALE_ACCOUNT_OR_SCAN": "口座またはスキャンの鮮度条件を満たさない",
    "ACCOUNT_NOT_FLAT": "専用口座の建玉ゼロ条件を満たさない",
    "CONTRACT_ALREADY_OCCUPIED": "同銘柄の既存建玉がある",
    "INVALID_CONTRACT_GRID": "取引所の価格・数量刻みが無効",
    "FEE_BUDGET_TOO_LOW": "取引所の手数料が設定済みの費用予算を超える",
    "INVALID_STRUCTURAL_READY": "価格刻みへの丸め後に元のSL/TPとRR条件を満たさない",
    "INVALID_LIVE_LEVELS": "現在の発注価格が固定SL/TPの範囲外",
    "LIVE_RR_BELOW_2": "現在の発注価格でRRが2未満",
    "SIZE_BELOW_MINIMUM": "証拠金・損失予算・取引所数量制限から算出した数量が最小注文未満",
    "INCOMPLETE_QUOTE": "取引所の価格・注文可能数量の応答が不足",
    "INVALID_QUOTE": "取引所の価格または注文可能数量が無効",
    "INVALID_DECIMAL": "価格・数量等の数値を検証できない",
    "EXCHANGE_REJECTED": "価格・数量照会を取引所が受理しなかった",
    "TRANSPORT_OR_SIGNING_UNKNOWN": "価格・数量照会の通信または認証結果が不明",
    "ENTRY_INPUT_INVALID": "発注前データの形式を検証できない",
    "ENTRY_CHECK_UNCLASSIFIED": "発注前チェックで対象外（詳細は未分類）",
}


def rejection_code(error, *, execution_error):
    # Only exact known machine codes. Never persist or echo exception messages.
    code = error.args[0] if isinstance(error, execution_error) and len(error.args) == 1 else None
    if type(code) is str and code in SKIP_LABELS and code != "PRE_ARM_BASELINE":
        return code
    return "ENTRY_CHECK_UNCLASSIFIED" if isinstance(error, execution_error) else "ENTRY_INPUT_INVALID"


def label(code):
    return SKIP_LABELS.get(code, "注文対象外（詳細理由の記録なし）") if type(code) is str else "注文対象外（詳細理由の記録なし）"
