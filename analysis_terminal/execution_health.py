"""Read-only bot-ledger audit. Never orders, re-arms, infers flatness or repairs data."""
from collections import Counter
from decimal import Decimal, InvalidOperation
from analysis_terminal import execution_incidents

TERMINAL = {"SKIPPED", "NO_FILL", "CLOSED", "EXTERNAL_FLAT_VERIFIED"}
PENDING = {"PREPARED", "SENDING_ENTRY", "ACKED_ENTRY"}
PROTECTING = {"SENDING_SL", "ACKED_SL", "SENDING_TP", "ACKED_TP"}
EXITING = {"SENDING_CLOSE", "ACKED_CLOSE", "CLOSE_PENDING"}
KNOWN = TERMINAL | PENDING | PROTECTING | EXITING | {"PROTECTED", "OWNERSHIP_CONFLICT", "OWNERSHIP_UNVERIFIED"}
LABELS = {
    "EXTERNAL_FLAT_VERIFIED": "隔離後の建玉ゼロ・注文終了を照合（損益未確定）",
    "SKIPPED": "注文対象外・過去setupの基準記録",
    "NO_FILL": "未約定を照合した記録",
    "CLOSED": "約定照合による決着の記録（Funding別）",
    "PREPARED": "発注意図を保存",
    "SENDING_ENTRY": "エントリー送信試行の記録（結果未確定）",
    "ACKED_ENTRY": "発注応答の記録（約定未確認）",
    "SENDING_SL": "SL送信試行の記録（有効性未確認）",
    "ACKED_SL": "SL応答の記録（有効性未確認）",
    "SENDING_TP": "TP送信試行の記録（有効性未確認）",
    "ACKED_TP": "TP応答の記録（有効性未確認）",
    "PROTECTED": "SL/TP照合の記録（現在の有効性は未保証）",
    "SENDING_CLOSE": "決済送信試行の記録（決着未確認）",
    "ACKED_CLOSE": "決済応答の記録（決着未確認）",
    "CLOSE_PENDING": "決済注文の照合待ち",
    "OWNERSHIP_CONFLICT": "同銘柄の所有権競合・隔離",
    "OWNERSHIP_UNVERIFIED": "建玉の所有権を確認できない状態",
}
EXCHANGE_CHECK = "取引所で建玉・注文・約定履歴を確認し、ボット自身のSL/TPと決済の状態を確認してください。"
NO_RETRY = "結果不明の注文を再送したり、未決着のまま再有効化したりせず、照合を先に行ってください。"
KEEP_MANAGEMENT = "未決着中のOFF・READ_ONLYへの切替は保護管理を停止します。運用設定を確認してください。"
STOP_REASONS = {
    "MANUAL_PAUSE": "手動で新規エントリーを停止しています。",
    "NOT_ARMED": "新規注文は未有効化です。",
    "ARM_CHECK_IN_PROGRESS": "有効化の事前照合中です。",
    "OPERATOR_REQUEST_FAILED": "直前の運営操作の事前条件・接続確認が失敗しました。",
    "RECONCILIATION_REQUIRED": "口座・注文の復旧照合が必要です。",
    "CONTRACT_OWNERSHIP_CONFLICT": "同銘柄の手動・外部注文との所有権競合があります。",
    "EXIT_OWNERSHIP_UNVERIFIED": "決済対象の所有権を確認できません。",
    "ORDER_ACK_UNKNOWN": "注文送信の応答が未確定です。",
    "ENTRY_ACK_UNRESOLVED": "エントリー注文・約定を確認できません。",
    "EXIT_CANCEL_UNCONFIRMED": "保護注文の取消完了を確認できません。",
    "EMERGENCY_CLOSE_UNRESOLVED": "決済注文の結果を確認できません。",
    "PROTECTION_ACK_UNRESOLVED": "SL/TPの受理・有効性を確認できません。",
    "PROTECTION_OR_FILL_INVALID": "約定または保護注文の照合で異常があります。",
    "CONTROL_INTERRUPTED": "運営操作の完了を確認できません。",
    "CONTROL_CONTENT_CHANGED": "運営操作の内容に不整合があります。",
    "DAILY_EQUITY_LOSS_LIMIT": "設定済みの日次損失制限に達しています。",
}


def timestamp(value, now_ms):
    return value if type(value) is int and 0 < value <= now_ms else None


def amount(value):
    try:
        if isinstance(value, bool):
            return None
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def report(records, connection, *, now_ms, events=()):
    counts = Counter({k: 0 for k in ("entry_pending", "protection_pending", "protection_recorded", "exit_pending", "ownership_uncertain", "unknown", "closed", "no_fill", "skipped", "recorded_fills")})
    active = 0
    expiring = False
    quality = False
    entry_ages = []
    for r in records:
        if not isinstance(r, dict) or not isinstance(r.get("status"), str) or r["status"] not in KNOWN:
            active += 1
            counts["unknown"] += 1
            quality = True
            continue
        status = r["status"]
        flags = ("ownership_quarantined", "entry_attempted", "sl_attempted", "tp_attempted", "close_attempted", "sl_cancel_attempted", "tp_cancel_attempted")
        invalid = any(k in r and type(r[k]) is not bool for k in flags)
        fill = amount(r["filled_size"]) if "filled_size" in r else Decimal(0)
        invalid = invalid or fill is None
        if status == "CLOSED":
            invalid = invalid or not fill or timestamp(r.get("outcome_observed_ms"), now_ms) is None
        if status in {"SKIPPED", "NO_FILL"} and fill:
            invalid = True
        if status == "EXTERNAL_FLAT_VERIFIED":
            invalid = invalid or not fill or r.get('ownership_quarantined') is not True or r.get('isolated_contract') is not True or r.get('reconciliation_original_status') != 'OWNERSHIP_CONFLICT' or r.get('financial_outcome_verified') is not False or timestamp(r.get('external_flat_verified_ms'), now_ms) is None
        elif status in TERMINAL and (r.get("ownership_quarantined") or status != "CLOSED" and r.get("close_attempted")):
            invalid = True
        if invalid:
            active += 1
            counts["unknown"] += 1
            quality = True
            continue
        if fill > 0:
            counts["recorded_fills"] += 1
        if status in TERMINAL:
            counts[{"SKIPPED": "skipped", "NO_FILL": "no_fill", "CLOSED": "closed", "EXTERNAL_FLAT_VERIFIED": "external_flat_verified"}[status]] += 1
            continue
        active += 1
        if r.get("ownership_quarantined") or status in {"OWNERSHIP_CONFLICT", "OWNERSHIP_UNVERIFIED"}:
            counts["ownership_uncertain"] += 1
        elif r.get("close_attempted") or status in EXITING:
            counts["exit_pending"] += 1
        elif status == "PROTECTED":
            deadline = r.get("protection_deadline_ms")
            if not fill or not r.get("sl_attempted") or not r.get("tp_attempted") or type(deadline) is not int or deadline <= 0:
                counts["unknown"] += 1
                quality = True
            elif r.get("sl_cancel_attempted") or r.get("tp_cancel_attempted"):
                counts["protection_pending"] += 1
            elif deadline <= now_ms + 60000:
                counts["protection_pending"] += 1
                expiring = True
            else:
                counts["protection_recorded"] += 1
        elif fill or status in PROTECTING:
            counts["protection_pending"] += 1
        else:
            counts["entry_pending"] += 1
            observed = timestamp(r.get("observed_ms"), now_ms)
            entry_ages.append(now_ms-observed if observed is not None else None)

    quality = quality or active > 1

    last = timestamp(connection.get("last_success_ms"), now_ms)
    fresh = last is not None and 0 <= now_ms-last < 30000
    reason = connection.get("reason")
    if reason is not None and not isinstance(reason, str):
        reason = "UNRECOGNIZED_STOP_REASON"
    error = connection.get("last_error")
    if counts["ownership_uncertain"]:
        state, message = "OWNERSHIP_UNCERTAIN", "同銘柄の所有権が不明です。ボットの保護注文が取り消されている可能性があります。"
    elif quality:
        state, message = "LEDGER_DATA_UNCERTAIN", "注文記録に未認識・不整合があり、建玉や決着を判断できません。"
    elif active and (connection.get("mode") != "LIVE" or connection.get("protective_management_enabled") is not True):
        state, message = "MANAGEMENT_DISABLED", "未決着のボット記録がある一方で、保護管理の稼働設定が無効です。"
    elif active and error == "ACCOUNT_OR_POLICY_CHANGED":
        state, message = "MANAGEMENT_BLOCKED", "口座・運用設定の変更により照合が停止しています。保護管理を確認してください。"
    elif counts["exit_pending"] or reason in {"EMERGENCY_CLOSE_UNRESOLVED", "EXIT_CANCEL_UNCONFIRMED"}:
        state, message = "EXIT_UNCONFIRMED", "決済または保護注文の取消を照合中です。決着済みとは判断できません。"
    elif counts["protection_pending"] or reason in {"PROTECTION_ACK_UNRESOLVED", "PROTECTION_OR_FILL_INVALID"}:
        state, message = "PROTECTION_UNCONFIRMED", "SL/TPの有効性が未確認です。建玉の保護状態を確認してください。"
        if expiring:
            message = "SL/TPの失効が近いか、期限を過ぎています。建玉の保護状態を確認してください。"
    elif active and not fresh:
        state, message = "ACTIVE_OBSERVATION_STALE", "未決着のボット記録があり、口座接続の確認が古いか不明です。"
    elif error or connection.get("status") == "ERROR" or reason not in {None, "MANUAL_PAUSE", "NOT_ARMED", "ARM_CHECK_IN_PROGRESS"}:
        state, message = "RECONCILIATION_REQUIRED", "接続・注文の照合に異常があります。復旧判断の前に取引所を確認してください。"
    elif counts["entry_pending"]:
        state = "ENTRY_PENDING" if fresh and all(a is not None and a < 10000 for a in entry_ages) else "ENTRY_UNCONFIRMED"
        message = "エントリーの発注意図・送信・応答を記録しています。約定はまだ確定できません。"
    elif counts["protection_recorded"]:
        state, message = "PROTECTION_RECORDED", "SL/TP照合の記録があります。これは現在の有効性の保証ではありません。"
    else:
        state, message = "NO_ACTIVE_BOT_RECORD", "ボットの未決着注文記録はありません。手動・他の建玉はこの集計に含みません。"
    attention = state not in {"NO_ACTIVE_BOT_RECORD", "PROTECTION_RECORDED", "ENTRY_PENDING"}
    guidance = [EXCHANGE_CHECK, NO_RETRY] if attention else []
    if active and connection.get("mode") != "LIVE":
        guidance.append(KEEP_MANAGEMENT)
    if state == "OWNERSHIP_UNCERTAIN":
        guidance.append("同じ銘柄への手動SL/TP追加・変更も競合の対象です。隔離された場合、ボット自身のSL/TPを取り消す処理を行います。手動注文は操作しないため、取引所で手動SL/TPが有効か確認してください。")
    if state == "PROTECTION_RECORDED":
        guidance.append("SL/TPは条件注文です。建玉欄に表示されない場合もあるため、取引所の条件注文一覧で確認してください。")
    recent = []
    for event in events:
        status = event.get("status") if isinstance(event, dict) else None
        observed = timestamp(event.get("observed_ms"), now_ms) if isinstance(event, dict) else None
        label = LABELS.get(status, "未認識の状態変化（照合が必要）") if isinstance(status, str) else "未認識の状態変化（照合が必要）"
        recent.append({"label": label if observed is not None else "記録時刻の照合が必要な状態変化", "observed_ms": observed})
    return {
        "scope": "BOT_LEDGER_ONLY", "read_only": True, "state": state,
        "summary": message, "attention_required": attention,
        "severity": "CRITICAL" if state in {"OWNERSHIP_UNCERTAIN", "LEDGER_DATA_UNCERTAIN", "MANAGEMENT_DISABLED", "MANAGEMENT_BLOCKED", "EXIT_UNCONFIRMED", "PROTECTION_UNCONFIRMED", "ACTIVE_OBSERVATION_STALE"} else "ATTENTION" if attention else "INFO",
        "active_records": active, "counts": dict(counts), "guidance": guidance,
        "connection_observation_fresh": fresh, "evaluated_ms": now_ms,
        "per_order_freshness_verified": False, "exchange_positions_queried": False,
        "stop_explanation": STOP_REASONS.get(reason, "停止理由の照合が必要です。" if reason else None),
        "entry_review": "UNRESOLVED_LEDGER_REVIEW_REQUIRED" if active else "NOT_ENABLED" if connection.get("real_orders_enabled") is not True or not fresh else "CURRENT_READY_RULES_AND_EXECUTION_CHECKS_REQUIRED",
        "recent_events": recent,
        "last_failure": execution_incidents.describe_failure(connection.get("last_failure"), now_ms),
        "last_emergency_close": execution_incidents.recent_close(records, now_ms),
    }
