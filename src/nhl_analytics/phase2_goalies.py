"""Past-only goalie selection/shrinkage research and explicit mixture feasibility."""
import argparse
import json
import math
import random
from collections import defaultdict
from datetime import datetime,timedelta
from pathlib import Path

import pyarrow.parquet as pq

from nhl_analytics.database import timestamp
from nhl_analytics.evaluation_support import save_csv
from nhl_analytics.features import write_document
from nhl_analytics.pregame import franchise
from nhl_analytics.phase2_common import load_data,artifacts,freeze_implementation
from nhl_analytics.research_registry import finish,verify_registry,note
from nhl_analytics.team_ablation import DEVELOPMENT

OTHER='OTHER'


def normalise(weights):
    if not weights or any(not math.isfinite(v) or v<0 for v in weights.values()) or sum(weights.values())<=0:
        raise ValueError('Invalid goalie probability weights')
    total=sum(weights.values());return {k:v/total for k,v in weights.items()}


def shrunk_save_rate(saves,shots,prior_saves,prior_shots,league=.900,pseudo=500):
    if not (0<=saves<=shots and 0<=prior_saves<=prior_shots and 0<league<1 and pseudo>0):raise ValueError('Invalid save/shrinkage inputs')
    prior=(prior_saves+pseudo*league)/(prior_shots+pseudo)
    return (saves+pseudo*prior)/(shots+pseudo)


def mixture(home,away,conditional,joint=None,assume_independence=False):
    """No implicit independence and no missing pairing/OTHER probabilities."""
    if abs(sum(home.values())-1)>1e-10 or abs(sum(away.values())-1)>1e-10:raise ValueError('Marginals must be normalised')
    if any(not math.isfinite(v) or v<0 for v in [*home.values(),*away.values()]):raise ValueError('Invalid marginal')
    if joint is None:
        if not assume_independence:raise ValueError('Joint distribution or explicit independence assumption required')
        joint={(h,a):ph*pa for h,ph in home.items() for a,pa in away.items()}
    if set(joint)!={(h,a) for h in home for a in away}:raise ValueError('Every goalie pairing must be represented')
    if any(not math.isfinite(v) or v<0 for v in joint.values()) or abs(sum(joint.values())-1)>1e-10:raise ValueError('Invalid joint probability')
    for h,p in home.items():
        if abs(sum(v for (i,j),v in joint.items() if i==h)-p)>1e-8:raise ValueError('Home marginal mismatch')
    for a,p in away.items():
        if abs(sum(v for (i,j),v in joint.items() if j==a)-p)>1e-8:raise ValueError('Away marginal mismatch')
    if any(pair not in conditional or not math.isfinite(conditional[pair]) or not 0<=conditional[pair]<=1 for pair in joint):
        raise ValueError('Missing/invalid conditional game probability')
    return sum(p*conditional[pair] for pair,p in joint.items())


class GoalieHistory:
    def __init__(self,records):
        self.teams=defaultdict(list);self.players=defaultdict(list);self.league=defaultdict(list);self.actual=defaultdict(list)
        for r in records:
            if r['season_id'] not in DEVELOPMENT:raise ValueError('Nondevelopment goalie record')
            if r['available']<=r['start']:raise ValueError('Invalid goalie chronology')
            self.teams[r['season_id'],r['team']].append(r)
            self.players[r['season_id'],r['player_id']].append(r)
            self.league[r['season_id']].append(r)
            if r['started']==1:self.actual[r['game_id'],r['team']].append(r)
        self.league_cache={}

    def known(self,records,cutoff,target):return [r for r in records if r['game_id']!=target and r['available']<cutoff and r['start']<cutoff]

    def selection(self,season,team,cutoff,target,game_date):
        records=self.known(self.teams[season,team],cutoff,target);origin='current_team_history'
        if not records:
            previous=(season//10000-1)*10000+season//10000
            records=self.known(self.teams[previous,team],cutoff,target);origin='prior_team_history'
        games=sorted({(r['start'],r['game_id']) for r in records})[-10:];ids={gid for _,gid in games}
        recent=[r for r in records if r['game_id'] in ids]
        latest={}
        for r in recent:latest[r['player_id']]=max(latest.get(r['player_id'],r['start']),r['start'])
        candidates=sorted(latest,key=lambda p:(latest[p],p),reverse=True)[:3]
        weights={OTHER:1.}
        for player in candidates:
            appearances=[r for r in recent if r['player_id']==player]
            gap=(game_date-max(r['game_date'] for r in appearances)).days
            rest=.25 if gap==0 else .5 if gap==1 else 1.
            weights[player]=(1+sum(r['started']==1 for r in appearances))*rest
        probabilities=normalise(weights)
        uniform=normalise({k:1. for k in weights})
        starters=sorted((r for r in recent if r['started']==1),key=lambda r:(r['start'],r['player_id']))
        last=starters[-1]['player_id'] if starters and starters[-1]['player_id'] in weights else OTHER
        last_proxy={k:.1/(len(weights)-1) for k in weights if k!=last} if len(weights)>1 else {}
        last_proxy[last]=.9 if len(weights)>1 else 1.
        return probabilities,uniform,last_proxy,dict(origin=origin,candidates=len(candidates),history_games=len(ids),
            max_available=max((r['available'] for r in recent),default=None))

    def rates(self,player,season,cutoff,target):
        previous=(season//10000-1)*10000+season//10000
        valid=lambda rows:[r for r in self.known(rows,cutoff,target) if r['shots'] is not None and r['saves'] is not None and 0<=r['saves']<=r['shots']]
        current=valid(self.players[season,player]);prior=valid(self.players[previous,player])
        key=previous,cutoff
        if key not in self.league_cache:
            league_rows=valid(self.league[previous]);sa=sum(r['shots'] for r in league_rows)
            self.league_cache[key]=sum(r['saves'] for r in league_rows)/sa if sa else .900
        league=self.league_cache[key]
        s,sa=sum(r['saves'] for r in current),sum(r['shots'] for r in current)
        ps,pa=sum(r['saves'] for r in prior),sum(r['shots'] for r in prior)
        return dict(league=league,unshrunk=s/sa if sa else league,shrunk=shrunk_save_rate(s,sa,ps,pa,league),
            current_shots=sa,prior_shots=pa)


def source_records(history,games):
    records=[]
    columns=['player_id','game_id','season_id','home_road','team_abbrev','games_started','shots_against','saves','toi_seconds','assumed_available_at']
    for batch in pq.ParquetFile(history/'goalies.parquet').iter_batches(batch_size=1000,columns=columns,use_threads=False):
        for r in batch.to_pylist():
            if r['season_id'] not in DEVELOPMENT:continue
            game=games[r['game_id']];side='home' if r['home_road']=='H' else 'away'
            if r['team_abbrev']!=game[side+'_team_abbrev']:raise ValueError('Goalie team/game mismatch')
            records.append(dict(player_id=r['player_id'],game_id=r['game_id'],season_id=r['season_id'],
                team=franchise(game[side+'_team_id']),game_date=game['game_date'],start=game['start_time_utc'],
                available=max(r['assumed_available_at'],game['assumed_available_at']),started=r['games_started'],
                shots=r['shots_against'],saves=r['saves'],minutes=r['toi_seconds']/60 if r['toi_seconds'] is not None else None))
    return records


def loss(probabilities,label):
    return -math.log(max(probabilities[label],1e-15)),sum((p-int(k==label))**2 for k,p in probabilities.items())


def residual_interval(rows):
    import numpy as np
    blocks=defaultdict(list)
    for r in rows:
        day=r['game_date'];blocks[day-timedelta(days=day.weekday())].append((r['home_residual'],r['away_residual']))
    def corr(values):
        n,sx,sy,sxy,sx2,sy2=values
        denominator=math.sqrt(max(0,sx2-sx*sx/n)*max(0,sy2-sy*sy/n))
        return (sxy-sx*sy/n)/denominator if denominator else None
    stats=np.asarray([[len(v),sum(x for x,y in v),sum(y for x,y in v),sum(x*y for x,y in v),sum(x*x for x,y in v),sum(y*y for x,y in v)] for v in blocks.values()])
    if not len(stats):return dict(n=0,weeks=0,correlation=None,lower=None,upper=None)
    result=dict(n=len(rows),weeks=len(blocks),correlation=corr(stats.sum(axis=0)),lower=None,upper=None)
    if len(blocks)>=10:
        rng=np.random.default_rng(42);values=[]
        for _ in range(1000):
            value=corr(stats[rng.integers(0,len(stats),size=len(stats))].sum(axis=0))
            if value is not None:values.append(value)
        if values:result.update(lower=float(np.quantile(values,.025)),upper=float(np.quantile(values,.975)))
    return result


def run(root):
    identifier=json.loads((root/'plans.json').read_text())['goalies'];verify_registry(identifier)
    output=root/'goalies'
    if output.exists() and any(output.iterdir()):raise ValueError('Goalie output already exists')
    output.mkdir(exist_ok=True)
    import sys
    from nhl_analytics import phase2_common
    freeze_implementation(output,sys.modules[__name__],phase2_common)
    rows,_,games,_,history,_,_=load_data();model=GoalieHistory(source_records(history,games))
    forecasts=[];form=[];joint=[];excluded=0
    for feature in rows:
        if feature['season_id'] not in DEVELOPMENT[1:]:continue
        game=games[feature['game_id']];cutoff=timestamp(feature['prediction_at']);pair={}
        for side in ('home','away'):
            team=franchise(game[side+'_team_id']);actual=model.actual[game['game_id'],team]
            p,uniform,last,meta=model.selection(game['season_id'],team,cutoff,game['game_id'],game['game_date'])
            if meta['max_available'] and meta['max_available']>=cutoff:raise ValueError('Late goalie source')
            if len(actual)!=1:excluded+=1;continue
            actual=actual[0];label=actual['player_id'] if actual['player_id'] in p else OTHER
            top=max(p,key=p.get)
            pair[side]=dict(probability=p,top_probability=p[top],top_correct=top==label,
                actual_probability=p[label],actual_label=label,home_residual=float(top==label)-p[top])
            for name,probabilities in [('selection_counts_rest',p),('uniform_candidates',uniform),('smoothed_last_starter',last)]:
                ll,brier=loss(probabilities,label);best=max(probabilities,key=probabilities.get)
                forecasts.append(dict(game_id=game['game_id'],season_id=game['season_id'],game_date=game['game_date'],side=side,model=name,
                    log_loss=ll,brier=brier,top_probability=probabilities[best],top_category_correct=best==label,
                    top_named_correct=best==actual['player_id'],actual_is_other=label==OTHER,other_probability=probabilities[OTHER],
                    probabilities=json.dumps(probabilities),candidates=meta['candidates'],source_origin=meta['origin']))
            if actual['shots'] is None or actual['saves'] is None or not 0<=actual['saves']<=actual['shots'] or not actual['shots']:continue
            rates=model.rates(actual['player_id'],game['season_id'],cutoff,game['game_id'])
            for name in ('league','unshrunk','shrunk'):
                probability=max(1e-6,min(1-1e-6,rates[name]));s,sa=actual['saves'],actual['shots']
                form.append(dict(game_id=game['game_id'],season_id=game['season_id'],side=side,model=name,shots=sa,
                    log_loss_sum=-s*math.log(probability)-(sa-s)*math.log(1-probability),
                    brier_sum=s*(1-probability)**2+(sa-s)*probability**2,current_shots=rates['current_shots'],prior_shots=rates['prior_shots']))
        if len(pair)==2:
            h,a=pair['home'],pair['away']
            joint.append(dict(game_id=game['game_id'],game_date=game['game_date'],home_residual=h['home_residual'],away_residual=a['home_residual'],
                product_top_pair_probability=h['top_probability']*a['top_probability'],both_top_correct=h['top_correct'] and a['top_correct'],
                joint_log_loss=-math.log(max(1e-15,h['actual_probability']*a['actual_probability']))))
    selection=[];calibration=[];performance=[]
    for season in [*DEVELOPMENT[1:],0]:
        for name in ('selection_counts_rest','uniform_candidates','smoothed_last_starter'):
            cohort=[r for r in forecasts if r['model']==name and (not season or r['season_id']==season)]
            if not cohort:continue
            n=len(cohort)
            selection.append(dict(season_id=season,model=name,n=n,log_loss=sum(r['log_loss'] for r in cohort)/n,
                multiclass_brier=sum(r['brier'] for r in cohort)/n,top_category_accuracy=sum(r['top_category_correct'] for r in cohort)/n,
                named_starter_accuracy=sum(r['top_named_correct'] for r in cohort)/n,other_frequency=sum(r['actual_is_other'] for r in cohort)/n,
                mean_other_probability=sum(r['other_probability'] for r in cohort)/n))
            for bucket in range(10):
                values=[r for r in cohort if min(9,int(r['top_probability']*10))==bucket]
                calibration.append(dict(season_id=season,model=name,bin_lower=bucket/10,bin_upper=(bucket+1)/10,n=len(values),
                    mean_top_probability=sum(r['top_probability'] for r in values)/len(values) if values else None,
                    observed_top_correct=sum(r['top_category_correct'] for r in values)/len(values) if values else None))
        for name in ('league','unshrunk','shrunk'):
            cohort=[r for r in form if r['model']==name and (not season or r['season_id']==season)];shots=sum(r['shots'] for r in cohort)
            if shots:performance.append(dict(season_id=season,model=name,team_games=len(cohort),shots=shots,
                shot_log_loss=sum(r['log_loss_sum'] for r in cohort)/shots,shot_brier=sum(r['brier_sum'] for r in cohort)/shots))
    assessment=residual_interval(joint)
    assessment.update(mean_product_top_pair=sum(r['product_top_pair_probability'] for r in joint)/len(joint) if joint else None,
        observed_both_top_correct=sum(r['both_top_correct'] for r in joint)/len(joint) if joint else None,
        interpretation='Residual correlation and coarse joint calibration assess, but cannot establish, conditional independence.')
    for name,values in [('selection_predictions',forecasts),('selection_metrics',selection),('selection_calibration',calibration),
                        ('performance_predictions',form),('performance_metrics',performance),('joint_selection_diagnostics',joint)]:save_csv(output/(name+'.csv'),values)
    write_document(output/'joint_assessment.json',assessment)
    result=finish(identifier,dict(decision='inconclusive',selection_metrics=selection,performance_metrics=performance,
        joint_assessment=assessment,excluded_ambiguous_starter_team_games=excluded,
        game_mixture_evaluated=False,production_changed=False,
        limitations=['Historical active rosters/injuries unavailable','OTHER is an aggregate category, not a named goalie forecast',
            'Save rates confound goalie ability and team shot defence','No validated conditional goalie-pair win model; independence not established'],
        artifact_sha256=artifacts(output)))
    verify_registry(identifier)
    lines=['# Priority 4 — Probabilistic goalie research','',
        f'Experiment `{identifier}`. Development seasons only; no holdout/prospective outcomes are used for selection. Production is unchanged.',
        '', '## A. Starter probabilities','',
        'Candidates come only from earlier known team appearances. Up to three recent candidates plus OTHER receive normalised count/rest probabilities. No historical roster membership is invented.',
        'Actual target-game starters are evaluation labels only. Category accuracy can count OTHER as correct; named-starter accuracy does not.',
        '', '| Model | Team-games | Log loss ↓ | Multiclass Brier ↓ | Named accuracy | Other frequency |','| --- | ---: | ---: | ---: | ---: | ---: |']
    for r in selection:
        if not r['season_id']:
            accuracy='undefined (uniform ties)' if r['model']=='uniform' else f"{r['named_starter_accuracy']:.2%}"
            lines.append(f"| {r['model']} | {r['n']} | {r['log_loss']:.5f} | {r['multiclass_brier']:.5f} | {accuracy} | {r['other_frequency']:.2%} |")
    lines.extend(['','Multiclass Brier sums errors across the candidate partition and OTHER; it is not numerically comparable to binary home-win Brier.',
        '', '## B. Shrunk save rates','',
        'A 500-shot league prior shrinks previous-season player save rates; another 500-shot prior shrinks current rates. Only earlier observations enter. All-strength NHL saves/shots are kept together; 5v5 team xG is not substituted for goalie GSAx.',
        'These are historical save-rate estimates, not isolated causal goalie ability. Target shot counts are scoring exposure only, never a forecast input.',
        '', '| Estimate | Shots | Shot log loss ↓ | Shot Brier ↓ |','| --- | ---: | ---: | ---: |'])
    for r in performance:
        if not r['season_id']:lines.append(f"| {r['model']} | {r['shots']} | {r['shot_log_loss']:.6f} | {r['shot_brier']:.6f} |")
    lines.extend(['','## C. Mixture feasibility','',f'Joint-selection diagnostics: `{assessment}`.',
        'A tested mixture helper requires a complete conditional-pair win matrix, normalised marginals and either matching joint weights or an explicit independence flag. OTHER pairings cannot be silently dropped.',
        '**No end-to-end game-win mixture was scored.** The frozen context model supplies team probabilities, not identified goalie-pair conditional probabilities. Supplying arbitrary ability-to-win coefficients would invent a model after inspecting results.',
        'Historical active rosters, injuries and pre-cutoff confirmed starts are unavailable; observed roster coverage begins in November 2026. Selection independence remains an approximation, not a conclusion from a small residual statistic.',
        'A defensible next step is a separately specified conditional/mixture-likelihood model trained only on earlier development data, with out-of-fold context offsets, explicit OTHER/fallback handling and joint-selection validation. Alternatively collect timestamped prospective roster/selection observations first. Neither requires changing the existing campaign.',
        'Until that work is specified and validated, improved selection/save-rate scores do not demonstrate improved game-win probabilities. This study is inconclusive for game-win promotion.',''])
    note('priority-4-goalies','\n'.join(lines));return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,required=True)
    args=parser.parse_args();print(json.dumps({'decision':run(args.root)['decision']}))


if __name__=='__main__':main()
