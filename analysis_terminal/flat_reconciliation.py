"""Explicit owner review of a quarantined, now-flat contract; GETs only.

This ends position management, not a financial outcome. Original fills, risk
fields and quarantine evidence remain; no TP/SL/PnL or exit price is inferred.
"""
import asyncio
import copy
from analysis_terminal import live_execution as live
from analysis_terminal.edgex_orders import ExecutionError, decimal


def eligible(records):
    try:
        return bool(records) and all(
            r.get('ownership_quarantined') is True and r.get('isolated_contract') is True
            and r.get('status') == 'OWNERSHIP_CONFLICT'
            and decimal(r.get('filled_size', 0)) > 0 for r in records)
    except Exception:
        return False


async def reconcile(engine, *, authorize, expected_epoch):
    with engine.db_connect() as conn:
        before = copy.deepcopy([r for r in live.orders(conn) if r['status'] not in live.TERMINAL])
        state = live.state(conn)
    if not eligible(before) or state['armed'] or state.get('control_epoch', 0) != expected_epoch:
        raise ExecutionError('FLAT_REVIEW_STATE_CHANGED')
    if state['bound_fingerprint'] != engine.config.fingerprint():
        raise ExecutionError('FLAT_REVIEW_STATE_CHANGED')
    contracts = {r['contract_id'] for r in before}
    if any(not isinstance(cid, str) or not cid.isdigit() or int(cid) <= 0 for cid in contracts):
        raise ExecutionError('FLAT_REVIEW_STATE_CHANGED')

    async def verify_flat():
        active = await engine.adapter.active_orders()  # Complete authenticated pages.
        account = await engine.adapter.account()  # Missing position lists are rejected by adapter.
        occupied = live.occupied_contracts(account, active)
        if not 0 <= engine.clock()-account['observed_ms'] < 5000:
            raise ExecutionError('FLAT_REVIEW_DATA_UNAVAILABLE')
        if contracts & occupied:
            raise ExecutionError('FLAT_REVIEW_POSITION_OR_ORDERS_REMAIN')

    async def reads():
        await verify_flat()
        for r in before:
            for kind in ('entry', 'sl', 'tp', 'close'):
                if not r.get(kind+'_attempted'):
                    continue
                order = await engine.adapter.order(r[kind+'_client_id'])
                if order is None:
                    raise ExecutionError('FLAT_REVIEW_ORDER_UNCONFIRMED')
                live.verify_order(order, r, kind)
                if order.get('status') not in {'FILLED', 'CANCELED'}:
                    raise ExecutionError('FLAT_REVIEW_ORDER_UNCONFIRMED')
                if kind == 'entry' and decimal(order['cumFillSize']) != decimal(r['filled_size']):
                    raise ExecutionError('FLAT_REVIEW_ORDER_UNCONFIRMED')
            if not r.get('entry_attempted'):
                raise ExecutionError('FLAT_REVIEW_ORDER_UNCONFIRMED')
        await verify_flat()  # Recheck after individual order reads, never cancel.

    try:
        await asyncio.wait_for(reads(), timeout=20)
    except ExecutionError as exc:
        if str(exc) in {'FLAT_REVIEW_POSITION_OR_ORDERS_REMAIN','FLAT_REVIEW_ORDER_UNCONFIRMED'}:
            raise
        raise ExecutionError('FLAT_REVIEW_DATA_UNAVAILABLE') from None
    except Exception:
        raise ExecutionError('FLAT_REVIEW_DATA_UNAVAILABLE') from None
    with engine.db_connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        authorize(conn)  # Recheck owner, policy, epoch and OFF priority after all GETs.
        state = live.state(conn)
        if state['armed'] or state.get('control_epoch', 0) != expected_epoch:
            raise ExecutionError('FLAT_REVIEW_STATE_CHANGED')
        current = [r for r in live.orders(conn) if r['status'] not in live.TERMINAL]
        if current != before:
            raise ExecutionError('FLAT_REVIEW_STATE_CHANGED')
        for r in current:
            r.update(status='EXTERNAL_FLAT_VERIFIED', external_flat_verified_ms=engine.clock(),
                     reconciliation_original_status=r['status'], financial_outcome_verified=False)
            live.save(conn, r, engine.clock())
        live.pause(conn, 'MANUAL_PAUSE')  # Never auto-arm after review.
        state = live.state(conn);state['last_error'] = None;live.save_state(conn, state)
