"""Conservative player-cluster residual prediction bands, fitted before evaluation."""

import math
from collections import defaultdict

from nhl_analytics.evaluation_support import horizon,player_values

MODELS=('pace','prior_shrinkage')
TARGETS=('goals','assists','points','games')
NOMINALS=(0.80,0.95)


def finite_quantile(scores,nominal):
    scores=sorted(scores)
    rank=math.ceil((len(scores)+1)*nominal)
    return scores[rank-1] if scores and rank<=len(scores) else None


def nodes(input_row):
    bucket=horizon(input_row['team_games_remaining'])
    prior=int(input_row['prior_games'])>0
    return [(bucket,prior),(bucket,None),(None,prior),(None,None)]


def fit_pools(artifacts,test_season,period_start,label_available,holdout,min_players=40):
    """No holdout labels, same-season errors or repeated-player pseudoreplicates."""
    pools=defaultdict(dict)
    provenance=defaultdict(lambda:dict(seasons=set(),latest=None))
    for key,label in artifacts['player_labels'].items():
        player,season,_=key
        available=label_available.get(season)
        if season>=test_season or season==holdout or available is None or available>=period_start:
            continue
        row=artifacts['player_inputs'][key]
        prediction=artifacts['player_predictions'][key]
        for model in MODELS:
            for target in TARGETS:
                _,p=player_values(row,prediction,target,model)
                error=abs(p-float(label[f'final_{target}']))
                for node in nodes(row):
                    group=(model,target,*node)
                    pools[group][player]=max(error,pools[group].get(player,0))
                    provenance[group]['seasons'].add(season)
                    provenance[group]['latest']=max(available,provenance[group]['latest'] or available)
    result={}
    for group,players in pools.items():
        result[group]=dict(n_players=len(players),radii={n:finite_quantile(players.values(),n) for n in NOMINALS},
            seasons=sorted(provenance[group]['seasons']),latest=provenance[group]['latest'],eligible=len(players)>=min_players)
    return result


def interval(input_row,prediction,target,model,nominal,pools):
    observed,point=player_values(input_row,prediction,target,model)
    for bucket,prior in nodes(input_row):
        pool=pools.get((model,target,bucket,prior))
        if not pool or not pool['eligible'] or pool['radii'][nominal] is None:
            continue
        q=pool['radii'][nominal]
        # Integer outward rounding and the observed-total floor cannot remove
        # a feasible nonnegative future total from the original residual band.
        lower=max(observed,math.floor(point-q))
        upper=max(lower,math.ceil(point+q))
        return dict(available=True,lower=lower,upper=upper,calibration_players=pool['n_players'],
            calibration_seasons=','.join(map(str,pool['seasons'])),max_calibration_label_available_at=pool['latest'],
            calibration_scope=f'remaining={bucket};prior={prior}',radius=q)
    return dict(available=False,lower=None,upper=None,calibration_players=0,
        calibration_seasons='',max_calibration_label_available_at=None,
        calibration_scope='insufficient earlier non-holdout player clusters',radius=None)


class Coverage:
    def __init__(self):
        self.n=self.covered=0
        self.width=0
        self.clusters=defaultdict(lambda:[0,0,0])

    def add(self,player,actual,lower,upper):
        covered=int(lower<=actual<=upper)
        width=upper-lower
        self.n+=1
        self.covered+=covered
        self.width+=width
        self.clusters[player][0]+=1
        self.clusters[player][1]+=covered
        self.clusters[player][2]+=width

    def result(self):
        return dict(n=self.n,unique_players=len(self.clusters),coverage=self.covered/self.n,
            average_width=self.width/self.n,
            player_weighted_coverage=sum(c/n for n,c,w in self.clusters.values())/len(self.clusters),
            player_weighted_average_width=sum(w/n for n,c,w in self.clusters.values())/len(self.clusters))
