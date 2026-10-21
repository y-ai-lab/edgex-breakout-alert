"""Offline audit of pinned retained originals; never restores a GitHub artifact."""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import stat
import sys
import tempfile
import time
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import app as scanner
from analysis_terminal import pending_entry_replay as study, pending_followup as follow
from analysis_terminal import execution_funnel

PROTOCOL = ROOT / '.github/research/retained_cohort_audit_protocol.json'
PIN = '007df73f6131ac28544c0c71ce41d38a212589952945fec40f95fc2d0ded7714'
META = ('base_artifact_id', 'base_artifact_digest', 'base_report_sha256')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    def invalid(_):
        raise ValueError('Non-finite JSON value')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def protocol():
    raw = PROTOCOL.read_bytes()
    if digest(raw) != PIN:
        raise ValueError('Registered continuity protocol changed')
    p = decode(raw)
    for name, expected in p['frozen_dependencies_sha256'].items():
        if digest((ROOT / name).read_bytes()) != expected:
            raise ValueError('Frozen research dependency changed')
    return p


def archive(path, expected):
    raw = Path(path).read_bytes()
    if len(raw) > 32 * 1024 * 1024 or digest(raw) != expected:
        raise ValueError('Retained ZIP checksum mismatch')
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        items = z.infolist()
        if len(items) > 1000 or sum(x.file_size for x in items) > 64 * 1024 * 1024:
            raise ValueError('Oversized retained archive')
        names = [x.filename for x in items]
        if len(set(names)) != len(names):
            raise ValueError('Duplicate archive member')
        for x in items:
            p = PurePosixPath(x.filename)
            if (p.is_absolute() or '..' in p.parts or '\\' in x.filename
                    or str(p) != x.filename.rstrip('/') or stat.S_ISLNK(x.external_attr >> 16)):
                raise ValueError('Unsafe archive member')
        return {x.filename: z.read(x) for x in items if not x.is_dir()}


def original(frozen_files, source_files, p, analyze, settings):
    if set(frozen_files) != {'report.json', 'source.json', 'seal.json'}:
        raise ValueError('Unexpected frozen archive layout')
    rb, sb = frozen_files['report.json'], frozen_files['source.json']
    seal = decode(frozen_files['seal.json'])
    if (digest(rb) != p['original_report_sha256'] or digest(sb) != p['original_source_sha256']
            or seal['report_sha256'] != digest(rb) or seal['source_sha256'] != digest(sb)
            or (seal['start_ms'], seal['end_ms']) != (p['cohort_start_ms'], p['cohort_end_ms'])
            or source_files['review/pending-entry-report.json'] != rb
            or source_files['source/replay-report.json'] != sb):
        raise ValueError('Original seal/source identity mismatch')
    base, source = decode(rb), decode(sb)
    if not base['period_complete'] or (base['start_ms'], base['end_ms']) != (p['cohort_start_ms'], p['cohort_end_ms']):
        raise ValueError('Unregistered original cohort')
    with tempfile.TemporaryDirectory(prefix='retained-source-') as directory:
        destination = Path(directory)
        for name, value in source_files.items():
            if name.startswith('source/'):
                target = destination / name.removeprefix('source/')
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(value)
        if study.run(destination, analyze, settings, role='prospective') != base:
            raise ValueError('Original full ledger does not reproduce')
    return base, source


def retained(files, base, source, p):
    raw = files['followup/followup-report.json']
    if digest(raw) != p['retained_followup_report_sha256']:
        raise ValueError('Retained followup checksum mismatch')
    saved = decode(raw)
    if (saved['base_artifact_id'] != p['archives']['frozen']['artifact_id']
            or saved['base_artifact_digest'] != 'sha256:' + p['archives']['frozen']['sha256']
            or saved['base_report_sha256'] != p['original_report_sha256']
            or saved['followup_end_ms'] != p['retained_end_ms']):
        raise ValueError('Retained followup provenance mismatch')
    manifest = decode(files['followup/candles/manifest.json'])
    if (manifest['start_ms'], manifest['end_ms']) != (p['cohort_end_ms'], p['retained_end_ms']):
        raise ValueError('Retained candle cutoff mismatch')
    candles = {}
    for item in manifest['sources']:
        name = item['file']
        if PurePosixPath(name).name != name or item['ticker'] in candles:
            raise ValueError('Ambiguous retained source')
        raw = files['followup/candles/' + name]
        if digest(raw) != item['sha256']:
            raise ValueError('Retained candle checksum mismatch')
        cs = [scanner.Candle(**c) for c in decode(raw)['MINUTE_15']]
        if len(cs) != item['candles'] or any(not p['cohort_end_ms'] <= c.time_ms < p['retained_end_ms'] for c in cs):
            raise ValueError('Retained candle count/grid mismatch')
        candles[item['ticker']] = cs
    rebuilt = follow.extend(base, source, candles, end_ms=p['retained_end_ms'])
    if rebuilt != {k: v for k, v in saved.items() if k not in META}:
        raise ValueError('Retained full followup does not reproduce')
    return rebuilt, candles


def finish(base, source, saved, candles, p, *, now_ms):
    cutoff = p['fixed_cutoff_ms']
    if (cutoff != p['cohort_end_ms'] + 7 * follow.DAY or now_ms < cutoff
            or not p['cohort_end_ms'] <= p['retained_end_ms'] <= cutoff
            or saved['followup_end_ms'] != p['retained_end_ms']):
        raise ValueError('Unfinished or changed fixed horizon')
    if len(base['records']) != len(saved['records']):
        raise ValueError('Changed record count')
    for old, new in zip(base['records'], saved['records']):
        if old['key'] != new['key'] or old['status'] != 'OPEN' and old != new:
            raise ValueError('Changed original identity/terminal record')
        if old['status'] == 'OPEN' and new['status'] == 'OPEN':
            raise ValueError('Existing OPEN requires unavailable later source; no guessed exit')
    final = follow.extend(base, source, candles, end_ms=cutoff)
    # No outcome, MFE/MAE, money, pending censoring or uncertainty may change.
    omit = {'followup_end_ms', 'followup_complete'}
    if {k:v for k,v in final.items() if k not in omit} != {k:v for k,v in saved.items() if k not in omit}:
        raise ValueError('Cutoff advance changed frozen outcomes or capital')
    blocked = sum(x['status'] in {'AMBIGUOUS', 'DATA_GAP', 'INVALIDATED_GAP'} for x in final['records'])
    return dict(dataset='LOCAL_RETAINED_ORIGINAL_AUDIT_NOT_GITHUB_FINAL_ARTIFACT',
                status='FIXED_HORIZON_AUDITED_WITH_UNCERTAINTY' if blocked else 'FIXED_HORIZON_AUDITED',
                actual_evaluated_source_end_ms=p['retained_end_ms'], fixed_cutoff_ms=cutoff,
                no_new_prices=True, new_independent_samples_added=False, changed_outcomes=0,
                original_regenerated=False, github_final_outcome_artifact_available=False,
                latest_missing_artifact_recovered=False, practical_assessment='NOT_QUALIFIED',
                changes_live_rules=False, real_orders_enabled=False, automatic_promotion=False,
                fixed_horizon_report=final,
                comparison=execution_funnel.summarize(final)['proposal_vs_current'])


def run(frozen, source_zip, retained_zip, output, *, now_ms=None, analyze=None, settings=None):
    p = protocol()
    output = Path(output)
    if output.exists():
        raise ValueError('Output already exists; never overwrite audit evidence')
    now_ms = int(time.time()*1000) if now_ms is None else now_ms
    if now_ms < p['fixed_cutoff_ms']:
        raise ValueError('Fixed horizon has not completed')
    frozen_files = archive(frozen, p['archives']['frozen']['sha256'])
    source_files = archive(source_zip, p['archives']['source']['sha256'])
    if analyze is None or settings is None:
        from analysis_terminal import server
        analyze, settings = server.analyze_contract, server.SETTINGS
    base, source = original(frozen_files, source_files, p, analyze, settings)
    saved, candles = retained(archive(retained_zip, p['archives']['retained']['sha256']), base, source, p)
    report = finish(base, source, saved, candles, p, now_ms=now_ms)
    report.update(protocol_sha256=PIN, original_main_run_id=p['first_main_run_id'],
                  original_main_sha=p['first_main_sha'], original_artifact_provenance=p['archives'])
    output.mkdir(parents=True, exist_ok=False)
    with (output/'audit-report.json').open('x') as f:
        json.dump(report, f, indent=2, allow_nan=False); f.write('\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('frozen', 'source', 'retained', 'output'):
        parser.add_argument('--'+name, required=True, type=Path)
    args = parser.parse_args()
    result = run(args.frozen, args.source, args.retained, args.output)
    print(json.dumps({k:result[k] for k in ('status','no_new_prices','changed_outcomes','new_independent_samples_added')}))


if __name__ == '__main__':
    main()
