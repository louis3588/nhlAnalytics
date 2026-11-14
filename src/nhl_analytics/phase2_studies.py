"""Pre-registered Phase 2 baselines and one causal opponent-strength experiment."""
import argparse
import bisect
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from nhl_analytics.database import timestamp
from nhl_analytics.evaluation_support import save_csv
from nhl_analytics.features import write_document
from nhl_analytics.pregame import FEATURES, franchise
from nhl_analytics.phase2_common import inputs,load_data,evaluate,artifacts,score_table
from nhl_analytics.research_registry import register,finish,verify_registry,note
from nhl_analytics.team_ablation import DEVELOPMENT
from nhl_analytics.team_models import CONFIG

SIMPLE=['prior_goal_diff_per_game_diff','season_goal_diff_per_game_diff','season_points_pct_diff']
ADJUSTED='opponent_adjusted_xg_strength_diff'


def specs():
    common=dict(role='development_only_research',data_mode='reconstructed_latest_corrected',
        input_sha256=inputs(),training_seasons=list(DEVELOPMENT),evaluation_seasons=list(DEVELOPMENT[1:]),
        holdout_policy='Exclude 2025–26 and all later rows before feature/label conversion; no prospective outcomes for selection',
        metrics=['brier','log_loss','accuracy','calibration','paired_week_bootstrap'],model_parameters=CONFIG,
        production_changed=False,implementation_version='phase2-v1',source_availability='Frozen NHL next-day assumption; not verified historical publication times')
    return {
        'baselines':dict(common,hypothesis='A small points/scoring model may explain most value of the 22-feature context model',
            feature_columns=SIMPLE,home_advantage='Training-only logistic intercept',
            points='Cumulative known regular-season points / (2 * known games); win=2, OT/SO loss=1',
            home_rate='Completed prior-season home wins / games; reuse original value after checking completed-prior provenance',
            missing='Training-only mean imputation plus missing indicators; no current history remains missing',
            comparisons=['home_rate','simple_strength','frozen_elo','baseline_context']),
        'opponent':dict(common,hypothesis='Recent team performance confounds own strength with opposition faced',
            feature_columns=[*FEATURES,ADJUSTED],
            formula='Ridge SRS: y_g = home_offset + rating_home - rating_away; y is MoneyPuck 5on5 net xG per game',
            penalty=20.,season_rating_carry=.5,home_offset_prior='Previous season fitted home offset; neutral zero without prior',
            fitting='For each pregame cutoff solve only games with start and assumed availability strictly earlier; cache unchanged prefixes',
            missing='Fail before fitting if any known paired xG is absent or unequal; never silently drop known games',
            comparisons=['context_plus_opponent_xg versus frozen Elo and unchanged context']),
        'goalies':dict(common,hypothesis='Past-only selection distributions and shrunk save rates better represent goalie uncertainty',
            selection='Up to 3 most recent goalies from last 10 known team-game appearances; prior-season tail when current is empty; explicit OTHER',
            weights='Last-10 start counts + 1 per candidate; OTHER weight=1; previous-day appearance multiplier=.5, same-day=.25, otherwise1',
            shrinkage='500-shot league shrinkage for prior-season player rate, then 500-shot prior shrinkage for current player rate',
            league_fallback=.900,selection_metrics=['multiclass log loss','multiclass Brier (sum across categories)','top-label calibration','candidate coverage'],
            performance_metrics=['shot-weighted Bernoulli log loss','shot-weighted Bernoulli Brier'],
            joint_assessment='Calendar-week residual correlation interval and product top-pair calibration; no unassessed independence',
            mixture_policy='Mathematical helper requires explicit joint weights or explicit independence flag; do not invent conditional pairing win probabilities',
            historical_limitation='No historical observed roster/injury/confirmed-starter feed; feasibility gate before game-win mixture')}


def prepare(root):
    if root.exists() and any(root.iterdir()):raise ValueError('Use a new empty Phase 2 run root')
    root.mkdir(parents=True,exist_ok=True);stamp=root.name
    identifiers={}
    for key,spec in specs().items():
        identifier=f'phase2_{key}_{stamp}'
        register(identifier,spec);identifiers[key]=identifier
    write_document(root/'plans.json',identifiers);return identifiers


def simple_features(rows,games):
    output=[]
    for row in rows:
        if row['season_id'] not in DEVELOPMENT:raise ValueError('Nondevelopment row')
        cutoff=timestamp(row['prediction_at']);season=row['season_id'];by_team=defaultdict(list)
        for game in games.values():
            if game['season_id']!=season or game['game_id']==row['game_id'] or not game['is_final'] or game['assumed_available_at']>=cutoff or game['start_time_utc']>=cutoff:continue
            for side in ('home','away'):
                team=franchise(game[side+'_team_id']);won=game['winner_team_id']==game[side+'_team_id']
                by_team[team].append(2 if won else int(game['result_type'] in ('OT','SO')))
        values=[]
        for side in ('home','away'):
            known=by_team[franchise(row[side+'_team_id'])]
            values.append(sum(known)/(2*len(known)) if known else None)
        prior=[g for g in games.values() if g['season_id']<season]
        completed=[s for s in {g['season_id'] for g in prior} if all(g['is_final'] and g['assumed_available_at']<cutoff for g in prior if g['season_id']==s)]
        known=[g for g in prior if g['season_id'] in completed]
        p=sum(g['winner_team_id']==g['home_team_id'] for g in known)/len(known) if known else .5
        if abs(p-float(row['historical_home_probability']))>1e-12:raise ValueError('Historical home comparator includes noncompleted/later data')
        result=dict(row);result[SIMPLE[-1]]=values[0]-values[1] if None not in values else None
        output.append(result)
    return output


class OpponentRatings:
    def __init__(self,records,teams,penalty=20.,carry=.5):
        self.records=defaultdict(list);self.teams=sorted(teams);self.index={t:i for i,t in enumerate(self.teams)}
        self.penalty=penalty;self.carry=carry;self.cache={};self.states={}
        for row in records:
            if row['season_id'] not in DEVELOPMENT:raise ValueError('Nondevelopment source')
            if row['available']<=row['start']:raise ValueError('Impossible source chronology')
            self.records[row['season_id']].append(row)
        for season in self.records:self.records[season].sort(key=lambda r:(r['available'],r['game_id']))
        self.times={s:[r['available'] for r in values] for s,values in self.records.items()}

    def at(self,season,cutoff):
        import numpy as np
        from threadpoolctl import threadpool_limits
        n=len(self.teams);zero=np.zeros(n+1)
        if season not in DEVELOPMENT:return zero,0
        count=bisect.bisect_left(self.times.get(season,[]),cutoff)
        previous=(season//10000-1)*10000+season//10000
        prior,_=self.at(previous,cutoff);centre=prior.copy();centre[:n]*=self.carry
        key=season,count,prior.tobytes()
        if key in self.cache:return self.cache[key]
        last,A,b=self.states.get(season,(0,np.zeros((n+1,n+1)),np.zeros(n+1)))
        if count<last:last,A,b=0,np.zeros((n+1,n+1)),np.zeros(n+1)
        records=self.records[season][:count]
        for r in records[last:]:
            if r['start']>=cutoff:raise ValueError('Future source start')
            x=np.zeros(n+1);x[self.index[r['home']]]=1;x[self.index[r['away']]]=-1;x[-1]=1
            A+=np.outer(x,x);b+=x*r['net']
        self.states[season]=(count,A,b)
        with threadpool_limits(limits=1):rating=np.linalg.solve(A+self.penalty*np.eye(n+1),b+self.penalty*centre)
        self.cache[key]=(rating,count);return rating,count

    def enrich(self,rows):
        output=[]
        for row in rows:
            cutoff=timestamp(row['prediction_at']);rating,n=self.at(row['season_id'],cutoff)
            result=dict(row);result[ADJUSTED]=float(rating[self.index[franchise(row['home_team_id'])]]-rating[self.index[franchise(row['away_team_id'])]])
            result['opponent_adjustment_games']=n;output.append(result)
        return output


def run_study(root,study):
    plans=json.loads((root/'plans.json').read_text());identifier=plans[study];verify_registry(identifier)
    output=root/study
    if output.exists() and any(output.iterdir()):raise ValueError('Study output already exists')
    output.mkdir(exist_ok=True)
    import sys
    from nhl_analytics import phase2_common,team_models
    phase2_common.freeze_implementation(output,sys.modules[__name__],phase2_common,team_models)
    rows,labels,games,xg,history,source,original=load_data()
    if study=='baselines':data=simple_features(rows,games);candidates={'simple_strength':SIMPLE};priority='priority-2-baselines'
    elif study=='opponent':
        teams={franchise(g[s+'_team_id']) for g in games.values() for s in ('home','away')}
        data=OpponentRatings(xg,teams).enrich(rows);candidates={'context_plus_opponent_xg':[*FEATURES,ADJUSTED]};priority='priority-3-opponent'
    else:raise ValueError('Use goalie research runner for goalie study')
    save_csv(output/'feature_dataset.csv',data)
    metrics,paired,predictions=evaluate(data,labels,source,original,candidates,output)
    coverage=[dict(feature=k,missing=sum(r.get(k) is None for r in data),rows=len(data)) for k in {k for v in candidates.values() for k in v}]
    save_csv(output/'missingness.csv',coverage)
    primary=[r for r in paired if r['season_id']==0 and r['reference_model']=='baseline_context' and r['model'] in candidates]
    decision='inconclusive'
    if study=='opponent' and primary:
        r=primary[0]
        decision='promising' if r['brier_upper'] is not None and r['brier_upper']<0 else 'rejected' if r['brier_lower'] is not None and r['brier_lower']>0 else 'inconclusive'
    result=finish(identifier,dict(decision=decision,metrics=metrics,paired_comparisons=paired,
        limitations=['Reconstructed publication times','Development-selected Elo parameters','Repeated development experiments; no automatic promotion'],
        artifact_sha256=artifacts(output),production_changed=False))
    verify_registry(identifier)
    text=f'# {"Independent baseline comparisons" if study=="baselines" else "Opponent-adjusted xG strength"}\n\nExperiment `{identifier}`. Production models are unchanged.\n\n'+score_table(metrics)
    text+='\n\nAll models use identical development games and one-hour cutoffs. Earlier seasons alone fit preprocessing and models. 2025–26 and later outcomes are excluded.\n\n'
    text+='Paired calendar-week bootstrap comparisons against Elo and context are in `paired.csv`; monthly metrics, calibration, feature coverage and portable research models are saved beside them.\n\n'
    for r in primary:text+=f"Pooled difference versus context for {r['model']}: {r['brier_delta']:+.6f}, exploratory 95% interval [{r['brier_lower']:.6f}, {r['brier_upper']:.6f}].\n\n"
    text+=f'Registry decision: **{decision}**. This is a research classification, not permission to replace production.\n\n'
    if study=='baselines':text+='The simple model uses prior goal differential, current goal differential and current points percentage differences. Home advantage is the training-only intercept. The home-rate comparator was verified against completed prior seasons only.\n'
    else:text+='One feature is the difference between ridge opponent-adjusted team ratings. The per-game 5on5 xG response, fixed penalty 20, rating carry 0.5 and historical franchise mapping were registered before scoring. The home-effect nuisance parameter is fitted only from earlier games. No standings or opponent results from the future enter the solution.\n'
    note(priority,text);return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--prepare',action='store_true');parser.add_argument('--study',choices=['baselines','opponent'])
    args=parser.parse_args()
    if args.prepare:print(json.dumps(prepare(args.root),indent=2))
    elif args.study:print(json.dumps({'decision':run_study(args.root,args.study)['decision']}))
    else:parser.error('Choose --prepare or --study')


if __name__=='__main__':main()
