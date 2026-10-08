"""Bounded public-OHLC experiment: two fixed unused weeks, one sealed result.

Never changes strategies, sends notifications, reads live DB data or places orders.
"""
import argparse
import asyncio
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import zipfile

from analysis_terminal import vwap_reclaim_replay as study
from analysis_terminal.replay import rule_fingerprint

REPO = 'y-ai-lab/edgex-breakout-alert'
LATEST = Path(__file__).with_name('vwap_reclaim_latest.json')
ORIGINAL_ANALYZER = '9b2df54f9d908d4b653fc270992ad3e1ada2e812f2d38133581f476516e55afd'


def original_strategy_check(server):
    if rule_fingerprint(server.analyze_contract, server.SETTINGS) != ORIGINAL_ANALYZER:
        raise ValueError('Frozen production comparator changed; do not reinterpret the experiment')
    raw = Path(server.scanner.__file__).read_bytes()
    if hashlib.sha256(raw).hexdigest() not in {
            'b1f4d4d15dc1ce4665f0f9f776de050059b60fb18faa4cf1583b04bf02a6c4af',
            '2e24f61e47ee40fdb3ae5f18152129f6632e83d508b73d27bf33fda0919ac9ab'}:
        raise ValueError('Frozen scanner changed')


def gh(path, *, binary=False):
    result = subprocess.run(['gh', 'api', path], capture_output=True)
    if result.returncode:
        # Do not put signed artifact redirect URLs or authentication into logs.
        raise RuntimeError('Public research artifact request failed')
    return result.stdout if binary else json.loads(result.stdout)


def first_seal(listing, name):
    if listing['total_count'] > len(listing['artifacts']):
        raise ValueError('Incomplete artifact listing; do not replace the first result')
    candidates = [a for a in listing['artifacts'] if a['name'] == name
                  and a.get('workflow_run', {}).get('head_branch') == 'main']
    return min(candidates, key=lambda a: (a['created_at'], a['id'])) if candidates else None


def development_archive(raw, meta):
    if 'sha256:' + hashlib.sha256(raw).hexdigest() != meta['digest']:
        raise ValueError('Development artifact hash mismatch')
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        names = archive.namelist()
        if any(Path(n).is_absolute() or '..' in Path(n).parts for n in names):
            raise ValueError('Unsafe research archive')
        candidates = [n for n in names if Path(n).name == 'development.json']
        if len(candidates) != 1:
            raise ValueError('Missing or duplicate frozen development report')
        return archive.read(candidates[0])


def scheduled(output, *, now_ms, head_branch=None):
    p = json.loads(study.PROTOCOL.read_text())
    periods = p['validation_periods']
    start, end = periods[0]['start_ms'], periods[-1]['end_ms']
    state = dict(dataset='REGISTERED_PUBLIC_VWAP_WEEK_PAIR', start_ms=start, end_ms=end,
                 real_orders_enabled=False, automatic_promotion=False, changes_live_rules=False,
                 captured_live_sample=False, independent_daily_samples_added=False)
    if now_ms // study.DAY * study.DAY < end:
        return dict(state, status='WAITING_FOR_COMPLETE_REGISTERED_WEEK_PAIR', seal=False)
    if (head_branch if head_branch is not None else os.getenv('GITHUB_REF_NAME')) != 'main':
        raise ValueError('Public future collection is restricted to main')
    name = f'edgex-vwap-frozen-pair-{start}-{end}'
    state['artifact_name'] = name
    first = first_seal(gh(f'repos/{REPO}/actions/artifacts?name={name}&per_page=100'), name)
    if first is not None:
        return dict(state, status='FROZEN_RESULT_EXPIRED' if first['expired'] else 'FROZEN_RESULT_ALREADY_RECORDED',
                    first_artifact_id=first['id'], first_artifact_digest=first.get('digest'),
                    first_source_sha=first['workflow_run']['head_sha'], seal=False)
    latest = json.loads(LATEST.read_text())
    pin = latest['pinned_development_artifact']
    meta = gh(f'repos/{REPO}/actions/artifacts/{pin["id"]}')
    if (meta['expired'] or meta['id'] != pin['id'] or meta['digest'] != pin['digest']
            or meta['workflow_run']['head_sha'] != pin['head_sha']):
        raise ValueError('Pinned development missing, expired or changed; do not regenerate')
    raw = development_archive(gh(f'repos/{REPO}/actions/artifacts/{pin["id"]}/zip', binary=True), meta)
    development = json.loads(raw)
    ignore = {'baseline_report_sha256'}  # Both preregistered immutable control archives.
    summary = {k: v for k, v in development.items() if k != 'records' and k not in ignore}
    expected = {k: v for k, v in latest['periods'][0].items() if k not in ignore}
    if summary != expected:
        raise ValueError('Frozen development differs from the reviewed result')
    output.mkdir(parents=True, exist_ok=True)
    dev_path = output / 'frozen-development.json'
    dev_path.write_bytes(raw)
    study.validation_gate(dev_path, p)  # Before importing transport or fetching future prices.
    from analysis_terminal import server, run_replay
    original_strategy_check(server)
    universe = json.loads(Path(__file__).with_name('replay_latest.json').read_text())['manifest']['universe']
    if hashlib.sha256(json.dumps(universe, sort_keys=True, separators=(',', ':')).encode()).hexdigest() != p['source_universe_sha256']:
        raise ValueError('Registered universe changed')
    contracts = {c['contract_id']: server.scanner.Contract(**c) for c in universe}
    # Preserve a started attempt for the workflow's always-run seal step if the
    # process is interrupted before main can record a final or failed status.
    (output / 'run-status.json').write_text(json.dumps(dict(state, status='COLLECTION_STARTED', seal=True)) + '\n')
    reports = [development]
    quality_blocked = False
    for i, period in enumerate(periods, 1):
        role = f'validation_{i}'
        source = output / role / 'source'
        asyncio.run(run_replay.run(period['end_ms'], 7, source, universe=contracts))
        report = study.build(source, server.analyze_contract, server.SETTINGS, role=role,
                             development_path=dev_path, now_ms=now_ms)
        (output / role / 'report.json').write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
        reports.append(report)
        quality_blocked = report['coverage']['valid_points'] == 0
        result = 'BLOCKED_DATA_QUALITY' if quality_blocked else study.decision(reports)
        if result in {'KILL_NO_LIVE_PROMOTION', 'BLOCKED_DATA_QUALITY'}:
            break  # Keep the second unused period sealed if the first kills this specification.
    review = dict(state, decision='BLOCKED_DATA_QUALITY' if quality_blocked else study.decision(reports), source_development_artifact=pin,
                  periods=[{k: v for k, v in r.items() if k != 'records'} for r in reports],
                  skipped_registered_periods=len(periods) - (len(reports) - 1),
                  limitations=p['limitations'], weekly_portfolios_are_independent=True,
                  total_portfolio_roi_pct=None, eligible_for_live_promotion=False)
    (output / 'decision-summary.json').write_text(json.dumps(review, indent=2, allow_nan=False) + '\n')
    return dict(state, status='FIXED_EXPERIMENT_RECORDED', decision=review['decision'], seal=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    now = int(time.time() * 1000)
    error = None
    try:
        state = scheduled(args.output, now_ms=now)
    except Exception as exc:
        error = exc
        periods = json.loads(study.PROTOCOL.read_text())['validation_periods']
        start, end = periods[0]['start_ms'], periods[-1]['end_ms']
        state = dict(status='FAILED_COLLECTION', error_type=type(exc).__name__,
                     start_ms=start, end_ms=end, artifact_name=f'edgex-vwap-frozen-pair-{start}-{end}',
                     seal=now // study.DAY * study.DAY >= end and os.getenv('GITHUB_REF_NAME') == 'main',
                     real_orders_enabled=False, automatic_promotion=False, changes_live_rules=False)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'run-status.json').write_text(json.dumps(state, indent=2) + '\n')
    print(json.dumps(state))
    if error is not None:
        raise SystemExit('Public research stopped; keep the first failure evidence, do not regenerate.')


if __name__ == '__main__':
    main()
