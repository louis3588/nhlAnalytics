"""Read-only prospective review, isolated from development model selection."""
import argparse
from pathlib import Path

from nhl_analytics.database import connect
from nhl_analytics.prospective_store import ProspectiveRepository
from nhl_analytics.prospective_reports import performance,game_item,save_report
from nhl_analytics.research_registry import note


def run(root):
    with connect() as connection,connection.transaction():
        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        repo=ProspectiveRepository(connection);campaign=repo.campaign('elo_vs_logistic_v1')
        if campaign is None:raise ValueError('Existing frozen campaign unavailable')
        now=repo.clock();rows=repo.snapshot(campaign)
        report=performance(rows,campaign,now)
        items=[game_item(row,campaign,now) for row in rows]
    folder=save_report(report,items,Path(root)/'prospective')
    lines=['# Priority 1 — Prospective evaluation','',
        'The tracker, campaign and sealing policy remain unchanged. This review reads sealed forecasts and validated outcomes only; no missed predictions are reconstructed.',
        f"Campaign `{report['campaign']}`; checked {now.isoformat()}. Report data: `{folder}`.",'',
        f"Sealed pairs: {report['coverage']['sealed_pairs']}; validated pairs: {report['coverage']['validated_paired']}; missing: {report['coverage']['missing']}; awaiting cutoff/data: {report['coverage']['pending']}.",'',
        '| Model | Matched games | Brier ↓ | Log loss ↓ | Accuracy |','| --- | ---: | ---: | ---: | ---: |']
    for r in report['metrics']:
        if r['timing']=='all' and r['dimension']=='all':
            fmt=lambda v:'unavailable' if v is None else f'{v:.5f}'
            lines.append(f"| {r['model']} | {r['n']} | {fmt(r['brier'])} | {fmt(r['log_loss'])} | {fmt(r['accuracy'])} |")
    d=next(r for r in report['model_disagreement'] if r['timing']=='all' and r['window_games']==0)
    lines.extend(['',f"The models disagree on the favourite in {d['disagreements']}/{d['n']} validated games.",
        '', 'Rolling 20/50/100-game tables report actual sample sizes, all three scoring metrics and separate calibration bins. If fewer games exist, the sample is explicitly smaller than the requested window.',
        'Cumulative, season/month and rolling cohorts are matched across both models. Early bootstrap forecasts remain separately filterable. Paired intervals remain unavailable below 100 games and ten calendar weeks.',
        'Freshness records the actual latest seal/result times and source age at seal. A newly generated report does not make old observations fresh.',
        'Existing final-state validation, revisions, postponed schedule matching and ledger immutability are unchanged. Corrections update derived scores, never forecasts.',
        'The React performance page supports these additive fields with backward-compatible empty states. It uses the existing API/static JSON adapter; no hosted service or deployment-policy change is introduced.',
        'Prospective outcomes in this report are operational evaluation only and are not used by the independent research runners to select features or models.',''])
    note('priority-1-prospective','\n'.join(lines));return report


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,required=True)
    args=parser.parse_args();report=run(args.root);print(report['coverage'])


if __name__=='__main__':main()
