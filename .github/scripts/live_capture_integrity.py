"""Offline continuity audit of immutable local live-Shadow snapshots.

No network, DB, notifications, orders, performance recalculation or promotion.
Never upload source snapshots or this report to GitHub. Synthetic CI only.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

STEP, WEEK = 900000, 604800000
WAIT_BARS = {'current_next_open': 2, 'shadow_v2_next_open': 2,
             'confirmed_pullback_cost_2r': 4, 'trend_session_vwap_reclaim_net2r': 4}
TERMINAL = {'TP', 'SL', 'AMBIGUOUS', 'DATA_GAP', 'EXPIRED',
            'INVALIDATED', 'INVALIDATED_GAP', 'REJECTED_AT_FILL'}
FILL = {'filled_ms', 'entry', 'stop_exit', 'target_exit', 'net_risk',
        'net_reward', 'net_rr', 'nominal_fill', 'gross_rr'}
DYNAMIC = FILL | {'status', 'next_candle_ms', 'outcome_ms', 'reason',
                  'exit_price', 'net_pnl_per_unit', 'final_net_r', 'mfe_r', 'mae_r'}
ORIGIN = ('activated_ms', 'capture_start_ms', 'protocol_sha256', 'engine_sha256')


def require(condition, message):
    # Error messages deliberately contain no ticker, record, price or policy.
    if not condition:
        raise ValueError(message)


def integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def ledger(payload):
    require(payload['dataset'] == 'LIVE_CAPTURE_HYPOTHETICAL', 'Wrong dataset')
    require(not any(payload[k] for k in ('real_orders_enabled', 'automatic_promotion',
                'eligible_for_live_promotion', 'notifications_enabled')), 'Wrong research scope')
    rows = payload['latest']
    require(len(rows) == payload['total_records'], 'Truncated ledger view')
    require(len({r['key'] for r in rows}) == len(rows), 'Duplicate record key')
    require(len({(r['model'], r['setup_id']) for r in rows}) == len(rows), 'Duplicate setup')
    origin = payload['meta']['capture_start_ms']
    for r in rows:
        clocks = [r[k] for k in ('created_ms', 'signal_candle_ms', 'observed_ms',
                  'execution_start_ms', 'expires_ms', 'next_candle_ms', 'cohort_start_ms')]
        require(all(integer(t) for t in clocks), 'Invalid record clock')
        close = r['signal_candle_ms'] + STEP
        require(r['signal_candle_ms'] % STEP == 0 and r['created_ms'] == close+1,
                'Signal close chronology changed')
        require(close <= r['observed_ms'] < close+STEP, 'Observation outside signal bucket')
        require(r['execution_start_ms'] == (r['observed_ms']//STEP+1)*STEP,
                'Elapsed capture bar used for execution')
        require(r['model'] in WAIT_BARS, 'Unknown frozen model')
        require(r['expires_ms'] == close+WAIT_BARS[r['model']]*STEP,
                'Original model expiry changed')
        require(r['cohort_start_ms'] == origin+(close-origin)//WEEK*WEEK
                and close >= origin, 'Wrong fixed cohort')
        require(r['next_candle_ms'] >= r['execution_start_ms']
                and r['next_candle_ms'] % STEP == 0, 'Invalid outcome cursor')
        require(r['status'] in TERMINAL | {'PENDING', 'OPEN'}, 'Unknown status')
        if r['filled_ms'] is not None:
            require(integer(r['filled_ms']) and r['filled_ms'] % STEP == 0
                    and r['execution_start_ms'] <= r['filled_ms'] < r['expires_ms'],
                    'Invalid hypothetical fill time')
    return {r['key']: r for r in rows}


def compare(before, after):
    """Accept forward updates; reject rewrites without repairing either input."""
    for k in ('protocol', 'dataset'):
        require(before[k] == after[k], 'Research scope changed')
    for k in ORIGIN:
        require(before['meta'][k] == after['meta'][k], 'Capture origin or frozen code changed')
    fingerprint = before['meta'].get('rule_fingerprint')
    require(fingerprint is None or fingerprint == after['meta'].get('rule_fingerprint'),
            'Analyzer fingerprint changed')
    old, new = ledger(before), ledger(after)
    require(old.keys() <= new.keys(), 'Previously captured record disappeared')
    changed = terminal = fills = 0
    for key, a in old.items():
        b = new[key]
        require({k:v for k,v in a.items() if k not in DYNAMIC} ==
                {k:v for k,v in b.items() if k not in DYNAMIC}, 'Frozen candidate changed')
        require(b['next_candle_ms'] >= a['next_candle_ms'], 'Outcome cursor moved backward')
        if a['filled_ms'] is not None:
            require({k:a.get(k) for k in FILL} == {k:b.get(k) for k in FILL},
                    'Existing hypothetical fill changed')
        if a['status'] in TERMINAL:
            require(a == b, 'Existing terminal outcome changed')
            terminal += 1
        else:
            require(a['status'] != 'OPEN' or b['status'] != 'PENDING', 'Open position reset')
            for field in ('mfe_r', 'mae_r'):
                require(b[field] >= a[field], 'Excursion history moved backward')
            changed += a != b
            fills += a['filled_ms'] is None and b['filled_ms'] is not None
    for key in new.keys()-old.keys():
        last = before['meta'].get('last_success_ms')
        require(last is None or new[key]['observed_ms'] > last, 'New record backfilled before prior observation')
    last = before['meta'].get('last_success_ms')
    require(last is None or (after['meta'].get('last_success_ms') is not None
            and after['meta']['last_success_ms'] >= last), 'Collector clock moved backward')
    return dict(previous_records=len(old), current_records=len(new),
                new_captured_records=len(new)-len(old), preserved_terminals=terminal,
                advanced_existing_records=changed, newly_observed_fills=fills,
                current_statuses=dict(Counter(r['status'] for r in new.values())),
                new_samples_added_by_audit=0, actual_execution_evidence=False)


def snapshot(directory, endpoint):
    directory = Path(directory)
    manifest = json.loads((directory/'manifest.json').read_text())
    entries = [e for e in manifest if e['endpoint'] == endpoint]
    require(len(entries) == 1 and entries[0]['status'] == 200, 'Missing successful source request')
    raw = (directory/(endpoint+'.json')).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    require(digest == entries[0]['sha256'], 'Source hash mismatch')
    return json.loads(raw), digest, entries[0]['fetched_ms']


def audit(before_dir, after_dir, endpoint):
    require(endpoint in ('pending', 'vwap'), 'Unsupported Shadow endpoint')
    a, ah, at = snapshot(before_dir, endpoint)
    b, bh, bt = snapshot(after_dir, endpoint)
    require(bt > at, 'Snapshots are not ordered forward in time')
    result = compare(a, b)
    # Re-read hashes before returning to catch source writes during inspection.
    require(snapshot(before_dir, endpoint)[1] == ah and
            snapshot(after_dir, endpoint)[1] == bh, 'Source changed during audit')
    return dict(dataset='LIVE_CAPTURE_CONTINUITY_AUDIT', endpoint=endpoint,
                before_sha256=ah, after_sha256=bh, before_fetched_ms=at,
                after_fetched_ms=bt, status='MATCH', **result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before', required=True)
    parser.add_argument('--after', required=True)
    parser.add_argument('--endpoint', choices=('pending', 'vwap'), required=True)
    parser.add_argument('--output', required=True, help='New local-only file; never upload')
    args = parser.parse_args()
    result = audit(args.before, args.after, args.endpoint)
    with Path(args.output).open('x') as f:
        json.dump(result, f, indent=2); f.write('\n')
    print('Continuity audit passed; local report saved; no extra trade samples.')


if __name__ == '__main__':
    main()
