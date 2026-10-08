"""Read-only review of frozen weekly artifacts, never a strategy or notifier."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from analysis_terminal import pending_entry_replay as study, pending_followup as follow, execution_funnel as funnel
import capital_admission_audit

DAY=86400000


def read(path):
    return json.loads(path.read_text())


def summarize(directory, *, now_ms):
    state=read(directory/'run-status.json')
    protocol=read(study.PROTOCOL)
    origin=protocol['prospective_start_ms']
    out=dict(dataset='FROZEN_WEEKLY_RESEARCH_REVIEW',collection_status=state['status'],
             evaluated_at_ms=now_ms,first_completed_day_ms=origin+DAY,
             automatic_promotion=False,eligible_for_live_promotion=False,real_orders_enabled=False,
             changes_live_rules=False,independent_sample_counts_added=False,
             next_step='COLLECT_REGISTERED_WINDOW_WITHOUT_PARAMETER_CHANGES')
    if state['status']=='WAITING_FOR_FIRST_PROSPECTIVE_DAY':
        if now_ms//DAY*DAY>origin:
            out.update(collection_status='COLLECTION_NOT_CURRENT',decision='NO_NEW_EVIDENCE')
        else:
            out['decision']='WAITING_FOR_FIRST_COMPLETED_DAY'
        return out
    if state['status']!='READY':
        raise ValueError('Unknown collection status')
    start,end,days=state['start_ms'],state['end_ms'],state['days']
    if (any(not isinstance(v,int) or isinstance(v,bool) for v in (start,end,days))
            or start<origin or (start-origin)%(7*DAY) or end%DAY
            or not 1<=days<=7 or end-start!=days*DAY or end>now_ms//DAY*DAY):
        raise ValueError('Unregistered, unfinished or inconsistent UTC window')
    out.update(cohort_start_ms=start,cohort_end_ms=end,period_complete=days==7,
               cohort_key=f'{start}:{start+7*DAY}',sample_note='Daily reports update this cohort; never sum their signals.')
    rp=directory/'review/pending-entry-report.json';sp=directory/'source/replay-report.json'
    if not rp.exists() or not sp.exists():
        out.update(collection_status='COLLECTION_INCOMPLETE',decision='NO_VERIFIED_RESULTS',
                   missing_files=[str(p.relative_to(directory)) for p in (rp,sp) if not p.exists()])
        return out
    report,source=read(rp),read(sp)
    if (report.get('role')!='PROSPECTIVE_PARAMETERS_RETROSPECTIVE_DATA'
            or (report['start_ms'],report['end_ms'])!=(start,end)
            or report.get('period_complete') is not (days==7)):
        raise ValueError('Ledger does not describe the registered collection window')
    follow.validate_base(report,source)
    checked=funnel.summarize(report)
    c=checked['proposal_vs_current']
    if report.get('opportunities')!=dict(additional_filled_setups_vs_current=c['proposal_only_filled_setups'],
                                         shared_filled_setups=c['shared_filled_setups']):
        raise ValueError('Opportunity counts differ from verified ledger')
    universe=source['manifest']['universe'];sources=source['manifest']['sources'];failures=source['manifest']['failures']
    names=[c['contract_name'] for c in universe];observed=[c['ticker'] for c in sources]+[c['ticker'] for c in failures]
    coverage=source['coverage']
    expected=days*DAY//study.STEP*len(names)
    if (len(set(names))!=len(names) or len(set(observed))!=len(observed) or set(names)!=set(observed)
            or report['source_files_verified']!=len(sources) or report['coverage']!=coverage
            or coverage['requested_markets']!=len(names) or coverage['fetched_markets']!=len(sources)
            or coverage['failed_markets']!=len(failures) or coverage['expected_points_all_markets']!=expected
            or not isinstance(coverage['valid_points'],int) or isinstance(coverage['valid_points'],bool)
            or not 0<=coverage['valid_points']<=expected):
        raise ValueError('Coverage or universe accounting mismatch')
    raw=study.decision([report])
    admissions={model:capital_admission_audit.summarize(
        [r for r in report['records'] if r['model']==model],report['portfolios'][model])
        for model in study.MODELS}
    quality_blocked=bool(failures) or coverage['valid_points']==0
    out.update(collection_status='DATA_QUALITY_BLOCKED' if quality_blocked else 'RESULTS_VERIFIED',
               current_cohort_rule_decision=raw,
               decision='DATA_QUALITY_BLOCKED' if quality_blocked else raw if days==7 else 'PROVISIONAL_'+raw,
               coverage=coverage,metrics=report['metrics'],portfolios=report['portfolios'],
               comparison=checked['proposal_vs_current'],
               capital_admission_audit=admissions,
               capital_admission_audit_sha256=hashlib.sha256(Path(capital_admission_audit.__file__).read_bytes()).hexdigest(),
               report_sha256=hashlib.sha256(rp.read_bytes()).hexdigest(),
               source_manifest_sha256=hashlib.sha256(sp.read_bytes()).hexdigest(),
               protocol_sha256=report['protocol_sha256'],engine_sha256=report['engine_sha256'],
               limitations=['Historical OHLC replay with frozen parameters, not captured-live fills or actual ROI.',
                            'Partial periods are provisional; no new cohort is created by another daily report.',
                            'OPEN, ambiguous results and gaps are not wins or losses; unknown portfolio ROI stays unknown.',
                            'This review does not combine historical development, validation or other weekly samples.'])
    return out


def markdown(result):
    lines=['## 固定条件の週次研究',f"状態: **{result['collection_status']}** / 判定: **{result['decision']}**",'',
           '本番昇格・実注文・通知なし。期間と条件を変更しない。']
    if 'metrics' not in result:
        first=datetime.fromtimestamp(result['first_completed_day_ms']/1000,timezone.utc).isoformat()
        return '\n'.join(lines+['',f"初回の完了日: {first}"])+ '\n'
    def show(v):
        return '不明' if v is None else f'{v:.4f}' if isinstance(v,float) else str(v)
    lines += ['',f"cohort: `{result['cohort_key']}` / 7日完了: {result['period_complete']}",
              '同じ週の日次更新を合算しない。MFE/MAEは足内のexitとの順序を保証しない。','',
              '| モデル | 約定 | 確定 | TP/SL | 勝率% | net平均R | PF | MFE/MAE R | 最大連敗 | サンプル |',
              '|---|---:|---:|---:|---:|---:|---:|---:|---:|---|']
    for model,m in result['metrics'].items():
        lines.append(f"| {model} | {m['filled']} | {m['resolved']} | {m['tp']}/{m['sl']} | {show(m['win_rate'])} | {show(m['avg_net_r'])} | {show(m['profit_factor'])} | {show(m['avg_mfe_r'])}/{show(m['avg_mae_r'])} | {show(m['max_consecutive_losses'])} | {m['sample_status']} |")
    lines += ['', '| モデル | 資金制限後約定 | 確定 | 実現損益 USDC | 最終ROI% | 保有/不確定 |', '|---|---:|---:|---:|---:|---:|']
    for model,p in result['portfolios'].items():
        lines.append(f"| {model} | {p['filled']} | {p['resolved']} | {show(p['realized_net_pnl_usdc'])} | {show(p['closed_portfolio_roi_pct'])} | {p['active']}/{p['uncertain']} |")
    lines += ['', '| モデル | 制約で失われた仮約定 | 除外理由 | 約定あり/なしの除外候補 |',
              '|---|---:|---|---:|']
    for model,audit in result['capital_admission_audit'].items():
        if not audit['reasons']:
            lines.append(f"| {model} | {audit['filled_omitted']} | なし | 0/0 |")
        for reason,counts in audit['reasons'].items():
            lines.append(f"| {model} | {counts['excluded_with_uncapped_fill']} | {reason} | {counts['excluded_with_uncapped_fill']}/{counts['excluded_without_uncapped_fill']} |")
    lines += ['', '元の固定研究口座の判断を観察。未約定候補の除外を失われた約定に数えない。',
              '予約・最小数量の診断は重複する。実運用口座の評価や予約解除の提案ではない。']
    c=result['comparison'];v=result['coverage']
    lines += ['',f"現行に対する仮想約定純増: {c['net_filled_count_difference']} / 資金制限後: {c['capped_filled_count_difference']}",
              f"有効市場時点: {v['valid_points']}/{v['expected_points_all_markets']} / 取得失敗: {v['failed_markets']}",
              '', 'net平均R = 候補単位のexpectancy。資金のROIとは異なる。',
              '判定は単独週の固定条件検査。過去・別週との合算や本番昇格の承認ではない。',
              f"ledger SHA256: `{result['report_sha256']}`"]
    return '\n'.join(lines)+'\n'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--research-dir',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--markdown',type=Path,required=True)
    args=parser.parse_args()
    if args.output.resolve()==args.markdown.resolve():
        raise ValueError('JSON and Markdown destinations must differ')
    result=summarize(args.research_dir,now_ms=int(time.time()*1000))
    for p in (args.output,args.markdown):
        if p.resolve().is_relative_to((args.research_dir/'source').resolve()) or p.resolve()==(args.research_dir/'review/pending-entry-report.json').resolve() or p.resolve()==(args.research_dir/'run-status.json').resolve():
            raise ValueError('Do not overwrite frozen inputs')
        p.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    args.markdown.write_text(markdown(result))


if __name__=='__main__':main()
