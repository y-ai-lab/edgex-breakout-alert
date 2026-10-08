"""Causal VWAP reclaim, immutable first observation, costs and capital gates."""
import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from analysis_terminal import vwap_reclaim_replay as study, pending_entry_replay as control
from analysis_terminal.test_pending_entry_replay import CONTRACT, candle

STEP, DAY = study.STEP, study.DAY
ORIGIN = 900 * DAY
SIGNAL = ORIGIN + 4 * STEP


def market(side='LONG', volume=20):
    monitor = [replace(candle(ORIGIN - (200 - i) * study.STEP4,
                             open=110 + i * .02, close=110 + i * .02, low=109 + i * .02, high=111 + i * .02),
                       interval='HOUR_4') for i in range(200)]
    entries = [candle(SIGNAL - (400 - i) * STEP, open=100, close=100, low=99.8, high=100.2)
               for i in range(400)]
    entries = [replace(c, volume=10) for c in entries]
    entries[-40] = replace(entries[-40], high=105)
    entries.append(replace(candle(SIGNAL, open=99.9, close=100.1, low=99.85, high=100.15), volume=volume))
    if side == 'SHORT':
        mirror = lambda c: replace(c, open=210-c.open, close=210-c.close, low=210-c.high, high=210-c.low)
        monitor, entries = [mirror(c) for c in monitor], [mirror(c) for c in entries]
    return monitor, entries


def record(side='LONG'):
    m, e = market(side)
    r, _, reason = study.candidate(CONTRACT, m[-180:], e[-180:])
    assert r is not None, reason
    return r


def evaluate(r, cs, *, missing_state=False):
    def state(stamp):
        if missing_state:
            return None
        return dict(setup_id=r['setup_id'], confirmation_color_ok=True, confirmation_level_ok=True,
                    retest_touched=True, stop_valid=True)
    return study.evaluate(r, cs, state, end_ms=max(c.time_ms for c in cs) + STEP)


def fill(r, offset=0):
    p = r['trigger']
    return candle(SIGNAL + (1 + offset) * STEP, open=p, close=p, low=p-.01, high=p+.01)


def report(role='validation_1', *, count=50, avg=.3, pf=1.5, win=45, stress=.2,
           stress_pf=1.3, capped=20, roi=2, extra=1, cap_extra=1, failure=0):
    p = json.loads(study.PROTOCOL.read_text())
    period = p['development'] if role == 'development' else p['validation_periods'][int(role[-1])-1]
    return dict(role=role, start_ms=period['start_ms'], end_ms=period['end_ms'], period_complete=True,
                coverage=dict(failed_markets=failure),
                metrics={study.MODEL:dict(resolved=count, avg_net_r=avg, profit_factor=pf, win_rate=win)},
                stress_metrics={study.MODEL:dict(avg_net_r=stress, profit_factor=stress_pf)},
                comparison=dict(net_filled_count_difference=extra, capped_filled_count_difference=cap_extra),
                portfolios={study.MODEL:dict(resolved=capped, closed_portfolio_roi_pct=roi)})


class VwapReclaimTests(unittest.TestCase):
    def test_volume_vwap_and_structural_extension_exclude_signal_both_sides(self):
        for side in ('LONG', 'SHORT'):
            m, e = market(side)
            origin, level, rv = study.session_reference(e[-180:])
            self.assertEqual(origin, ORIGIN)
            self.assertAlmostEqual(level, 100 if side == 'LONG' else 110)
            self.assertEqual(rv, 2)
            r, _, _ = study.candidate(CONTRACT, m[-180:], e[-180:])
            self.assertLess(r['extension_target'], 105) if side == 'LONG' else self.assertGreater(r['extension_target'], 105)
            extreme = replace(e[-1], high=1000 if side == 'LONG' else e[-1].high,
                              low=1 if side == 'SHORT' else e[-1].low, volume=100000)
            ref = study.session_reference((e[:-1] + [extreme])[-180:])
            self.assertEqual(ref[1], level)  # Huge signal cannot move its own reclaimed VWAP.

    def test_utc_session_uses_all_prior_bars_without_previous_day_volume(self):
        _, e = market()
        a = study.session_reference(e[-180:])
        e[-40] = replace(e[-40], volume=100000, high=1000)
        self.assertEqual(study.session_reference(e[-180:]), a)

    def test_four_prior_session_bars_required_and_missing_session_not_rebuilt(self):
        _, e = market()
        self.assertIsNone(study.session_reference(e[:-1]))
        without_midnight = [c for c in e if c.time_ms != ORIGIN]
        self.assertIsNone(study.session_reference(without_midnight))

    def test_zero_or_invalid_volume_never_creates_a_signal(self):
        m, e = market()
        self.assertIsNone(study.session_reference([replace(c, volume=0) for c in e]))
        for v in (-1, float('nan'), True):
            bad = e[:-1] + [replace(e[-1], volume=v)]
            with self.assertRaises(ValueError):
                study.candidate(CONTRACT, m[-180:], bad[-180:])

    def test_closed_4h_trend_strict_color_and_close_all_required(self):
        for side in ('LONG', 'SHORT'):
            m, e = market(side)
            last = e[-1]
            for c in (replace(last, open=last.close),
                      replace(last, close=100 if side=='LONG' else 110),
                      replace(last, open=last.close+.01 if side=='LONG' else last.close-.01)):
                r, _, _ = study.candidate(CONTRACT, m[-180:], (e[:-1]+[c])[-180:])
                self.assertIsNone(r)
            flat = [replace(c, open=100, close=100) for c in m[-180:]]
            self.assertIsNone(study.candidate(CONTRACT, flat, e[-180:])[0])

    def test_first_low_volume_reclaim_consumed_before_later_higher_volume(self):
        m, e = market(volume=19.99)
        r, episode, reason = study.candidate(CONTRACT, m[-180:], e[-180:])
        self.assertIsNone(r)
        self.assertEqual(reason, 'FIRST_RECLAIM_LOW_VOLUME')
        self.assertEqual(episode, ('LONG', ORIGIN))
        later = [replace(candle(SIGNAL+STEP, open=100.1, close=99.95, low=99.8, high=100.2), volume=10),
                 replace(candle(SIGNAL+2*STEP, open=99.95, close=100.2, low=99.9, high=100.3), volume=100)]
        out = study.replay_market(CONTRACT, m, e+later, start_ms=SIGNAL+STEP, end_ms=SIGNAL+4*STEP)
        self.assertFalse(out['records'])
        self.assertEqual(out['exclusions']['FIRST_RECLAIM_LOW_VOLUME'], 1)
        self.assertGreater(out['exclusions'].get('ALREADY_CONSUMED_SESSION_RECLAIM', 0), 0)

    def test_frozen_stop_uses_observed_structure_and_fee_inclusive_target_both_sides(self):
        for side in ('LONG', 'SHORT'):
            r = record(side)
            m, e = market(side)
            atr = study.scanner._atr(e[-180:], 14)
            extreme = min(c.low for c in e[-4:]) if side=='LONG' else max(c.high for c in e[-4:])
            self.assertAlmostEqual(r['stop'], extreme-.5*atr if side=='LONG' else extreme+.5*atr)
            self.assertAlmostEqual(control.cost_levels(r['trigger'], r['stop'], r['target'], side)['net_rr'], 2)
            self.assertEqual(r['created_ms'], SIGNAL+STEP+1)
            self.assertEqual(r['expires_ms'], SIGNAL+5*STEP)

    def test_net_room_failure_does_not_resize_stop_or_reclaim_later_signal(self):
        m, e = market()
        e[-41] = replace(e[-41], high=100.2)
        before = copy.deepcopy(e)
        r, episode, reason = study.candidate(CONTRACT, m[-180:], e[-180:])
        self.assertIsNone(r)
        self.assertEqual(reason, 'NET_ROOM_BELOW_2R')
        self.assertIsNotNone(episode)
        self.assertEqual(e, before)

    def test_future_4h_bar_missing_indicator_bar_and_off_grid_rejected(self):
        m, e = market()
        for monitor, entries in ((m[-179:]+[replace(m[-1],time_ms=ORIGIN)], e[-180:]),
                                 (m[-180:], e[-179:]),
                                 (m[-180:], e[-180:-1]+[replace(e[-1], time_ms=SIGNAL+1)])):
            with self.assertRaises(ValueError):
                study.candidate(CONTRACT, monitor, entries)

    def test_signal_and_prior_bar_extremes_cannot_fill_resolve_or_add_excursions(self):
        r = record()
        cs = [candle(SIGNAL-STEP, low=1, high=1000), candle(SIGNAL, low=1, high=1000), fill(r)]
        out = evaluate(r, cs)
        self.assertEqual(out['status'], 'OPEN')
        self.assertEqual(out['filled_ms'], SIGNAL+STEP)
        self.assertEqual(out['mfe_r'], 0)
        self.assertEqual(out['mae_r'], 0)

    def test_unknown_fill_exit_order_and_both_later_touches_ambiguous_both_sides(self):
        for side in ('LONG', 'SHORT'):
            r = record(side)
            both = candle(SIGNAL+STEP, open=r['trigger'], low=1, high=1000)
            self.assertEqual(evaluate(r, [both])['status'], 'AMBIGUOUS')
            later = replace(both, time_ms=SIGNAL+2*STEP)
            out = evaluate(r, [fill(r), later])
            self.assertEqual(out['status'], 'AMBIGUOUS')
            self.assertIsNone(out['final_net_r'])
            self.assertIsNone(control.portfolio([out])['closed_portfolio_roi_pct'])

    def test_missing_first_bar_or_pre_fill_state_blocks_later_profit(self):
        r = record()
        winner = candle(SIGNAL+2*STEP, open=r['trigger'], close=r['target'], low=r['trigger'], high=r['target'])
        for cs, missing_state in (([winner], False), ([fill(r), winner], True)):
            out = evaluate(r, cs, missing_state=missing_state)
            self.assertEqual(out['status'], 'DATA_GAP')
            self.assertIsNone(out['final_net_r'])
            self.assertIsNone(control.portfolio([out])['closed_portfolio_roi_pct'])

    def test_missing_post_fill_bar_does_not_infer_a_later_tp(self):
        r = record()
        tp = candle(SIGNAL+3*STEP, open=r['trigger'], close=r['target'], low=r['trigger'], high=r['target'])
        out = evaluate(r, [fill(r), tp])
        self.assertEqual(out['status'], 'DATA_GAP')
        self.assertIsNotNone(out['filled_ms'])

    def test_only_closed_outcome_bars_used_and_expiry_never_extended(self):
        r = record()
        cs = [fill(r), candle(SIGNAL+2*STEP, low=1, high=1000)]
        state = lambda stamp: dict(setup_id=r['setup_id'], confirmation_color_ok=True,
                                  confirmation_level_ok=True, retest_touched=True, stop_valid=True)
        out = study.evaluate(r, cs, state, end_ms=SIGNAL+2*STEP)
        self.assertEqual(out['status'], 'OPEN')
        no_fill = [candle(SIGNAL+(i+1)*STEP, open=r['trigger']+.1, close=r['trigger']+.1,
                          low=r['trigger']+.05, high=r['trigger']+.2) for i in range(4)]
        out = evaluate(r, no_fill)
        self.assertEqual(out['status'], 'EXPIRED')
        self.assertEqual(out['outcome_ms'], r['expires_ms'])

    def test_stress_changes_costs_on_same_fills_not_sample_or_prices(self):
        for side in ('LONG', 'SHORT'):
            r = record(side)
            exit_bar = candle(SIGNAL+2*STEP, open=r['target'], close=r['target'],
                              low=r['target']-.01, high=r['target']+.01)
            out = evaluate(r, [fill(r), exit_bar])
            before = copy.deepcopy(out)
            stress = study.stressed_metrics([out])
            self.assertEqual(out['status'], 'TP')
            self.assertAlmostEqual(out['final_net_r'], 2)
            self.assertEqual(stress['resolved'], 1)
            self.assertLess(stress['avg_net_r'], 2)
            self.assertEqual(out, before)

    def test_adverse_sl_gap_charges_worse_open_and_keeps_original_stop(self):
        r = record()
        gap = candle(SIGNAL+2*STEP, open=90, close=90, low=89, high=91)
        out = evaluate(r, [fill(r), gap])
        self.assertEqual(out['status'], 'SL')
        self.assertLess(out['final_net_r'], -1)
        self.assertEqual(out['stop'], r['stop'])

    def test_pre_period_first_reclaim_and_missed_state_not_rearmed(self):
        m, e = market()
        later = [replace(candle(SIGNAL+STEP, open=100.1, close=99.95, low=99.8, high=100.2), volume=10),
                 replace(candle(SIGNAL+2*STEP, open=99.95, close=100.2, low=99.9, high=100.3), volume=100)]
        out = study.replay_market(CONTRACT, m, e+later, start_ms=SIGNAL+2*STEP, end_ms=SIGNAL+3*STEP)
        self.assertFalse(out['records'])
        self.assertGreater(out['exclusions']['WARMUP_FIRST_RECLAIM'], 0)
        missing = [c for c in e if c.time_ms != ORIGIN]
        out = study.replay_market(CONTRACT, m, missing+later, start_ms=SIGNAL+STEP, end_ms=SIGNAL+3*STEP)
        self.assertFalse(out['records'])
        self.assertGreater(out['exclusions']['INCOMPLETE_INDICATOR_WINDOW'], 0)

    def test_future_prices_and_volumes_cannot_change_prefix_replay(self):
        m, e = market()
        r = record()
        prefix = e+[replace(fill(r), volume=10)]
        first = study.replay_market(CONTRACT, m, prefix, start_ms=SIGNAL+STEP, end_ms=SIGNAL+2*STEP)
        future = replace(candle(SIGNAL+2*STEP, open=100, close=100, low=1, high=1000), volume=1000000)
        with_future = study.replay_market(CONTRACT, m, prefix+[future], start_ms=SIGNAL+STEP, end_ms=SIGNAL+2*STEP)
        self.assertEqual(first, with_future)

    def test_duplicate_identity_bad_ohlc_and_volume_fail_before_research(self):
        m, e = market()
        for cs in (e+[e[-1]], e[:-1]+[replace(e[-1], contract_id='bad')],
                   e[:-1]+[replace(e[-1], low=200)], e[:-1]+[replace(e[-1], volume=-1)]):
            with self.assertRaises(ValueError):
                study.replay_market(CONTRACT, m, cs, start_ms=SIGNAL+STEP, end_ms=SIGNAL+2*STEP)

    def test_pending_rechecks_frozen_level_strict_confirmation_and_trend(self):
        m, e = market()
        r = record()
        self.assertEqual(study.pending_state(r, m[-180:], e[-180:])['setup_id'], r['setup_id'])
        self.assertIsNone(study.pending_state(r, m[-180:], e[-180:], direction='SHORT')['setup_id'])
        lost = e[:-1]+[replace(e[-1], close=r['roll_level'])]
        self.assertFalse(study.pending_state(r, m[-180:], lost[-180:])['confirmation_level_ok'])

    def test_kill_sufficient_negative_not_few_losing_trades(self):
        self.assertEqual(study.decision([report(role='development', count=20, avg=-.1, pf=.9)]), 'KILL_NO_LIVE_PROMOTION')
        self.assertEqual(study.decision([report(role='development', count=19, avg=-1, pf=0)]), 'CONTINUE_INSUFFICIENT_SAMPLE')
        self.assertEqual(study.decision([report(role='development', count=20, avg=1e-15, pf=1+1e-15)]), 'KILL_NO_LIVE_PROMOTION')

    def test_development_or_duplicate_validation_cannot_satisfy_two_unused_periods(self):
        self.assertEqual(study.decision([report(role='development'), report()]), 'CONTINUE_INSUFFICIENT_SAMPLE')
        with self.assertRaises(ValueError):
            study.decision([report(), report()])

    def test_practicality_requires_win_margin_stress_pure_capital_increase_and_roi(self):
        good = report()
        self.assertEqual(study.decision([good, report('validation_2')]), 'LIVE_CAPTURE_SHADOW_REQUIRED')
        for bad in (report('validation_2', win=39), report('validation_2', pf=1.19),
                    report('validation_2', stress=-.1), report('validation_2', stress_pf=1),
                    report('validation_2', extra=0), report('validation_2', cap_extra=0)):
            self.assertEqual(study.decision([good, bad]), 'PIVOT_NO_PRACTICAL_MARGIN')
        self.assertEqual(study.decision([good, report('validation_2', count=49)]), 'CONTINUE_INSUFFICIENT_SAMPLE')
        self.assertEqual(study.decision([good, report('validation_2', capped=19)]), 'CONTINUE_INSUFFICIENT_CAPITAL_SAMPLE')
        self.assertEqual(study.decision([good, report('validation_2', roi=None)]), 'CONTINUE_ROI_UNVERIFIED')
        self.assertEqual(study.decision([good, report('validation_2', roi=-.1)]), 'PIVOT_NO_CAPITAL_PROFIT')
        self.assertEqual(study.decision([good, report('validation_2', failure=1)]), 'BLOCKED_DATA_QUALITY')

    def test_protocol_dependencies_and_future_periods_fixed(self):
        p = json.loads(study.PROTOCOL.read_text())
        for name, digest in p['frozen_dependencies_sha256'].items():
            self.assertEqual(hashlib.sha256(study.PROTOCOL.with_name(name).read_bytes()).hexdigest(), digest)
        a, b = p['validation_periods']
        self.assertEqual(a['end_ms'], b['start_ms'])
        self.assertEqual(a['end_ms']-a['start_ms'], 7*DAY)
        self.assertEqual(b['end_ms']-b['start_ms'], 7*DAY)
        self.assertFalse(p['real_orders_enabled'])
        self.assertFalse(p['changes_live_rules'])
        with self.assertRaises(ValueError):
            study.build(Path('/tmp/unused-vwap-source'), None, None, role='development')

    def test_validation_requires_original_development_and_never_opens_after_kill(self):
        p = json.loads(study.PROTOCOL.read_text())
        with self.assertRaises(ValueError):
            study.validation_gate(None, p)
        r = record()
        sl = evaluate(r, [fill(r), candle(SIGNAL+2*STEP, open=90, close=90, low=89, high=91)])
        records = [dict(sl, key=sl['key']+str(i), setup_id=sl['setup_id']+str(i)) for i in range(20)]
        d = report('development', count=20, avg=-1, pf=0)
        d.update(records=records, protocol_sha256=hashlib.sha256(study.PROTOCOL.read_bytes()).hexdigest(),
                 engine_sha256=hashlib.sha256(Path(study.__file__).read_bytes()).hexdigest(),
                 source_manifest_sha256=p['development']['source_manifest_sha256'])
        models = (*control.MODELS, study.MODEL)
        d['metrics'] = {m:control.metrics([r for r in records if r['model']==m]) for m in models}
        d['portfolios'] = {m:control.portfolio([r for r in records if r['model']==m]) for m in models}
        d['stress_metrics'] = {m:study.stressed_metrics([r for r in records if r['model']==m]) for m in models}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'development.json'
            path.write_text(json.dumps(d))
            with self.assertRaisesRegex(ValueError, 'stays sealed'):
                study.validation_gate(path, p)
            d['metrics'][study.MODEL]['avg_net_r'] = 1
            path.write_text(json.dumps(d))
            with self.assertRaisesRegex(ValueError, 'economics'):
                study.validation_gate(path, p)

    def test_partial_and_overlapping_reviews_cannot_qualify_as_two_complete_periods(self):
        partial = report('validation_2');partial['period_complete']=False
        self.assertEqual(study.decision([report(), partial]), 'CONTINUE_INSUFFICIENT_SAMPLE')
        overlap = report('validation_2');overlap['start_ms']=report()['start_ms']
        with self.assertRaises(ValueError):
            study.decision([report(), overlap])


if __name__ == '__main__':
    unittest.main()
