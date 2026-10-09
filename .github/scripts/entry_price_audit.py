"""Reproduce saved public-research price diagnostics; never infer extra fills."""
import hashlib
import json
from pathlib import Path

from analysis_terminal import pending_fill_diagnostics as prices


def summarize(directory, *, analyze=None, settings=None):
    path = directory / 'review/pending-fill-diagnostics.json'
    if not path.exists():
        return dict(status='NOT_RECORDED', summary=None, new_samples_added=False)
    raw = path.read_bytes()
    saved = json.loads(raw)
    if analyze is None or settings is None:
        from analysis_terminal import server
        analyze, settings = server.analyze_contract, server.SETTINGS
    # The original evaluator verifies candle hashes, candidate identity, closed
    # indicator windows, unchanged four-bar expiry and the full saved ledger.
    rebuilt = prices.build(directory / 'source',
                           directory / 'review/pending-entry-report.json',
                           analyze, settings)
    if rebuilt != saved:
        raise ValueError('Saved entry-price diagnostics do not reproduce from public sources')
    return dict(status='VERIFIED_FROM_FROZEN_PUBLIC_SOURCES',
                source_report_sha256=saved['source_report_sha256'],
                source_manifest_sha256=saved['source_manifest_sha256'],
                diagnostics_sha256=hashlib.sha256(raw).hexdigest(),
                source_files_verified=saved['source_files_verified'],
                original_candidates_reproduced=saved['original_candidates_reproduced'],
                observation=saved['observation'], summary=saved['summary'],
                new_samples_added=False, actual_execution_evidence=False,
                limitations=['Same historical cohort; do not add these observations as trades.',
                             'Late touches do not change expiry, entries, outcomes or portfolio ROI.',
                             'Price distance R differs from net stop-risk P&L R.',
                             'Do not tune prices, wait bars or select symbols from these diagnostics.'])
