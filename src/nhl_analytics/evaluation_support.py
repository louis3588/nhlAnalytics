"""Small bounded accumulators and validated readers for reconstructed evaluations."""

import csv
import json
import math
from collections import defaultdict
from pathlib import Path

from nhl_analytics.database import timestamp
from nhl_analytics.reconstructed import MODE,file_hash


def sample_key(row):
    return int(row['player_id']),int(row['season_id']),timestamp(row['prediction_at'])


def horizon(value):
    value=int(value)
    return '00-20' if value<=20 else '21-40' if value<=40 else '41-60' if value<=60 else '61+'


def read_artifacts(root):
    manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('status')!='complete' or manifest.get('data_mode')!=MODE:
        raise ValueError('A complete reconstructed baseline run is required')
    outputs={}
    for name in ('player_inputs','player_labels','player_predictions','elo_inputs_predictions','elo_labels'):
        path=root/f'{name}.csv'
        if file_hash(path)!=manifest['artifacts'][name]['sha256']:
            raise ValueError(f'Baseline artifact changed: {name}')
        with path.open(encoding='utf-8',newline='') as stream:
            rows=list(csv.DictReader(stream))
        if len(rows)!=manifest['artifacts'][name]['rows'] or any(r['data_mode']!=MODE for r in rows):
            raise ValueError(f'Baseline artifact row count or provenance mismatch: {name}')
        keys=[sample_key(r) if name.startswith('player_') else int(r['game_id']) for r in rows]
        if len(set(keys))!=len(keys):
            raise ValueError(f'Duplicate keys: {name}')
        outputs[name]=dict(zip(keys,rows))
    if set(outputs['player_inputs'])!=set(outputs['player_predictions']):
        raise ValueError('Player input/prediction keys differ')
    if not set(outputs['player_labels'])<=set(outputs['player_inputs']):
        raise ValueError('Unmatched player labels')
    for key,row in outputs['player_inputs'].items():
        if timestamp(row['max_assumed_available_at'])>=key[2]:
            raise ValueError('Player feature availability reaches prediction instant')
        prediction=outputs['player_predictions'][key]
        if int(row['goals'])+int(row['assists'])!=int(row['points']):
            raise ValueError('Recorded goals plus assists differ from points')
        for model in ('pace','prior_shrinkage'):
            if abs(float(prediction[f'{model}_goals'])+float(prediction[f'{model}_assists'])-float(prediction[f'{model}_points']))>1e-8:
                raise ValueError('Projected goals plus assists differ from points')
            for target in ('goals','assists','points','games'):
                player_values(row,prediction,target,model)
    return outputs,manifest


class Metrics:
    def __init__(self):
        self.n=0
        self.a=self.b=self.c=self.d=0.0
        self.y=self.confidence=0.0
        self.players=set()

    def regression(self,predicted,actual,player):
        error=predicted-actual
        self.n+=1
        self.a+=abs(error)
        self.b+=error*error
        self.c+=error
        self.players.add(player)

    def probability(self,p,y):
        self.n+=1
        self.a+=(p-y)**2
        self.b-=y*math.log(max(p,1e-15))+(1-y)*math.log(max(1-p,1e-15))
        self.c+=int((p>=0.5)==bool(y))
        self.d+=p
        self.y+=y
        self.confidence+=max(p,1-p)

    def regression_result(self):
        return dict(n=self.n,unique_players=len(self.players),mae=self.a/self.n,
                    rmse=math.sqrt(self.b/self.n),bias=self.c/self.n)

    def probability_result(self):
        return dict(n=self.n,brier=self.a/self.n,log_loss=self.b/self.n,accuracy=self.c/self.n,mean_probability=self.d/self.n,
                    observed_home_win_rate=self.y/self.n,home_probability_bias=(self.d-self.y)/self.n,
                    mean_favorite_confidence=self.confidence/self.n,favorite_confidence_gap=(self.confidence-self.c)/self.n)


def player_values(input_row,prediction,target,model):
    observed=int(input_row['games_played' if target=='games' else target])
    projected=float(prediction['projected_games' if target=='games' else f'{model}_{target}'])
    if projected<observed-1e-9:
        raise ValueError('Projection is below already recorded statistics')
    return observed,projected


def slices(row):
    return [('all','all'),('month',row['prediction_at'][:7]),
            ('remaining_games',horizon(row['team_games_remaining'])),
            ('prior_records','yes' if int(row['prior_games'])>0 else 'no')]


def save_csv(path,rows,fields=None):
    iterator=iter(rows)
    first=next(iterator,None)
    fields=fields or list(first or {})
    temporary=path.with_suffix('.csv.tmp')
    count=0
    with temporary.open('w',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields)
        writer.writeheader()
        if first is not None:
            writer.writerow(first)
            count+=1
        for row in iterator:
            writer.writerow(row)
            count+=1
    temporary.replace(path)
    return dict(rows=count,sha256=file_hash(path))
