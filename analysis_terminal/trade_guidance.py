"""Presentation of frozen analysis; never used for signal/order eligibility."""
from analysis_terminal.entry_band import diagnose, positive


def valid_first_target(row):
    entry, stop, target, first = (row.get(k) for k in ('entry_reference', 'stop_loss', 'take_profit', 'tp1_2r'))
    if not all(positive(v) for v in (entry, stop, target, first)):
        return None
    if row.get('direction') == 'LONG' and stop < entry < first <= target:
        return first
    if row.get('direction') == 'SHORT' and target <= first < entry < stop:
        return first
    return None


def describe(row, default_action):
    result = dict(version=1, changes_ready_rules=False, action=default_action,
                  status='STAGE_ONLY', first_target=valid_first_target(row))
    band = row.get('entry_band')
    if not isinstance(band, dict):
        return result
    if (row.get('direction') != band.get('direction') or
            band != diagnose(band.get('direction'), band.get('structural_stop'),
                             band.get('structural_target'), band.get('confirmation_level'),
                             band.get('min_rr'))):
        result.update(action='SKIP', status='INVALID_DIAGNOSTIC', first_target=None)
    elif band['status'] == 'NO_OVERLAP':
        result.update(action='WAIT FOR STRUCTURE', status='STRUCTURE_CONFLICT')
    elif (band['status'] == 'COMPATIBLE' and row.get('stage') in
          {'CONFIRMATION_WAIT', 'RETEST_WAIT', 'RR_WAIT'} and
          (not positive(row.get('rr')) or row['rr'] < band['min_rr'])):
        result.update(action='WAIT FOR RR', status='PRICE_OUTSIDE_RR_BAND')
    return result
