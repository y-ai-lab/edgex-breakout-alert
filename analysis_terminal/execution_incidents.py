"""Fixed, credential-free explanations of persisted execution incidents."""

CLOSE_REASONS = {
    'PARTIAL_EXIT_REMAINDER': 'SL/TPの一部約定後、残りの数量を緊急決済する処理',
    'POSITION_SIZE_MISMATCH': '記録された約定数量と建玉数量の不一致',
    'FILLED_ENTRY_OUTSIDE_LIMITS': '実際の約定が価格・RR・リスク・金額・保護期限の条件から外れた',
    'PROTECTION_SEND_FAILED': 'SL/TP注文の送信または送信前確認に失敗',
    'PROTECTION_QUERY_FAILED': 'SL/TP注文を取引所から確認できなかった',
    'PROTECTION_ACK_UNRESOLVED': '送信したSL/TP注文の応答・記録を確認できなかった',
    'PROTECTION_CONTENT_INVALID': 'SL/TP注文の内容が保存した条件と一致しなかった',
    'PROTECTION_NOT_UNTRIGGERED': 'SL/TPが未発動の有効注文として確認できず、建玉が残っていた',
    'ACTIVE_PROTECTION_UNVERIFIED': '有効注文一覧でボット自身のSL/TPを確認できなかった',
    'RECONCILIATION_FAILED': '約定・建玉・注文の照合中にエラーが発生',
    'UNSPECIFIED_EMERGENCY_CLOSE': '緊急決済の詳細理由は未分類',
}
FAILURE_CODES = {
    'CONTRACT_OWNERSHIP_CONFLICT': 'ACCOUNT_OWNERSHIP_UNCERTAIN',
    'INCOMPLETE_ACCOUNT': 'ACCOUNT_READ_FAILED',
    'STALE_ACCOUNT_OR_SCAN': 'ACCOUNT_READ_FAILED',
    'PROTECTION_NOT_ACTIVE': 'PROTECTION_CHECK_FAILED',
    'PROTECTION_MISMATCH': 'PROTECTION_CHECK_FAILED',
    'UNSAFE_EXIT_ORDER': 'PROTECTION_CHECK_FAILED',
    'ORDER_ACK_UNKNOWN': 'ORDER_RESPONSE_UNCERTAIN',
    'ACCOUNT_OR_POLICY_CHANGED': 'POLICY_CHANGED',
}
FAILURE_REASONS = {
    'ACCOUNT_OWNERSHIP_UNCERTAIN': '同銘柄の建玉・注文の所有権を確認できなかった',
    'ACCOUNT_READ_FAILED': '口座・建玉の取得または鮮度確認に失敗',
    'PROTECTION_CHECK_FAILED': 'SL/TP・決済注文の内容または有効性の確認に失敗',
    'ORDER_RESPONSE_UNCERTAIN': '注文送信の結果を確認できなかった',
    'POLICY_CHANGED': '口座・運用設定の変更により照合が停止',
    'OTHER_EXECUTION_ERROR': '約定・建玉・注文の照合に失敗（詳細分類なし）',
}


def failure(code, observed_ms):
    # Never copy arbitrary SDK/exception text into incident metadata or output.
    return dict(category=FAILURE_CODES.get(code, 'OTHER_EXECUTION_ERROR'), observed_ms=observed_ms)


def public_failure(value, now_ms):
    if not isinstance(value, dict):
        return None
    stamp, category = value.get('observed_ms'), value.get('category')
    if type(stamp) is not int or not 0 < stamp <= now_ms:
        return None
    return dict(category=category if isinstance(category, str) and category in FAILURE_REASONS
                else 'OTHER_EXECUTION_ERROR', observed_ms=stamp)


def describe_failure(value, now_ms):
    clean = public_failure(value, now_ms)
    return dict(label=FAILURE_REASONS[clean['category']], observed_ms=clean['observed_ms'],
                historical=True) if clean else None


def recent_close(records, now_ms):
    candidates = []
    for r in records:
        if not isinstance(r, dict) or r.get('close_attempted') is not True:
            continue
        stamp = r.get('emergency_close_requested_ms')
        if type(stamp) is not int:
            # Existing finalized rows have a completion clock, not a request clock.
            stamp = r.get('outcome_observed_ms')
        if type(stamp) is int and 0 < stamp <= now_ms:
            candidates.append((stamp, r))
    if not candidates:
        return None
    stamp, r = max(candidates, key=lambda pair: pair[0])
    reason = r.get('emergency_close_reason')
    known = isinstance(reason, str) and reason in CLOSE_REASONS
    return dict(label=CLOSE_REASONS[reason] if known else
                '旧記録に緊急決済の詳細理由がありません。原因は未確定です。',
                observed_ms=stamp, reason_recorded=known,
                time_basis='REQUEST_RECORDED' if type(r.get('emergency_close_requested_ms')) is int
                           else 'COMPLETION_RECORDED',
                closed_recorded=r.get('status') == 'CLOSED', historical=True)
