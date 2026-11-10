"""Player pace projections and sequential Elo, using labelled reconstructed history."""

import argparse
import csv
import heapq
import json
import math
from collections import Counter,defaultdict
from datetime import date,datetime,time,timedelta,timezone
from pathlib import Path

from nhl_analytics.asof import LEAGUE_TZ
from nhl_analytics.database import timestamp
from nhl_analytics.features import write_document
from nhl_analytics.reconstructed import MODE,load_history,file_hash

PARAMETERS=dict(elo_k=20.0,elo_home_advantage=55.0,elo_season_retention=0.75,
    prior_pseudo_games=10,min_player_games=5)
TARGETS=('goals','assists','points')
FRANCHISE_MAP={59:53,68:53}  # Arizona -> Utah Hockey Club -> Utah Mammoth


def regression_metrics(pairs):
    pairs=list(pairs)
    if not pairs:
        return dict(n=0,mae=None,rmse=None,bias=None)
    errors=[prediction-actual for prediction,actual in pairs]
    return dict(n=len(errors),mae=sum(map(abs,errors))/len(errors),
        rmse=math.sqrt(sum(e*e for e in errors)/len(errors)),bias=sum(errors)/len(errors))


def probability_metrics(pairs):
    pairs=list(pairs)
    if not pairs:
        return dict(n=0,brier=None,log_loss=None,accuracy=None)
    return dict(n=len(pairs),brier=sum((p-y)**2 for p,y in pairs)/len(pairs),
        log_loss=-sum(y*math.log(max(p,1e-15))+(1-y)*math.log(max(1-p,1e-15)) for p,y in pairs)/len(pairs),
        accuracy=sum((p>=0.5)==bool(y) for p,y in pairs)/len(pairs))


def validate_history(data,seasons):
    """Fail closed on incomplete season schedules and obvious partial backfills."""
    games={g['game_id']:g for g in data['games']}
    counts={kind:Counter(r['game_id'] for r in data[kind]) for kind in ('skaters','goalies')}
    completed=[]
    for season in seasons:
        rows=[g for g in games.values() if g['season_id']==season]
        if not rows or any(g['retrieved_at'] is None or g['start_time_utc'] is None for g in rows):
            raise ValueError(f'Missing schedules/results for {season}')
        if all(g['is_final'] for g in rows):
            # These requested completed seasons all have 32 teams x 82 games.
            totals=Counter(t for g in rows for t in (g['home_team_id'],g['away_team_id']))
            if season in (20212022,20222023,20232024,20242025,20252026) and (len(totals)!=32 or set(totals.values())!={82}):
                raise ValueError(f'Incomplete completed-season schedule: {season}')
            completed.append(season)
        for game in rows:
            if game['is_final'] and (game['winner_team_id'] not in (game['home_team_id'],game['away_team_id'])
                or game['home_score'] is None or game['away_score'] is None or game['home_score']==game['away_score']):
                raise ValueError(f"Invalid final result for game {game['game_id']}")
            if game['is_final'] and (counts['skaters'][game['game_id']]<30 or counts['goalies'][game['game_id']]<2):
                raise ValueError(f"Player backfill incomplete for game {game['game_id']}")
    for kind in ('skaters','goalies'):
        seen=set()
        for row in data[kind]:
            key=(row['player_id'],row['game_id'])
            game=games.get(row['game_id'])
            if key in seen or not game or not game['is_final'] or row['season_id']!=game['season_id']:
                raise ValueError(f'Invalid/duplicate {kind} player-game link: {key}')
            seen.add(key)
            expected=game['home_team_abbrev'] if row['home_road']=='H' else game['away_team_abbrev']
            if row['team_abbrev']!=expected:
                raise ValueError(f'Player/game team mismatch: {key}')
            if kind=='skaters' and any(row[k] is None for k in TARGETS):
                raise ValueError(f'Missing scoring targets: {key}')
            if kind=='skaters' and row['points']!=row['goals']+row['assists']:
                raise ValueError(f'Invalid scoring arithmetic: {key}')
    return completed


def player_projection(current,prior,team_played,team_remaining,pseudo_games=10):
    """No future appearances enter either rate or playing-time estimate."""
    n=len(current)
    if n==0 or team_played<n or team_remaining<0:
        raise ValueError('Invalid player projection opportunity counts')
    remaining=team_remaining*min(1.0,n/team_played)
    totals={k:sum(r[k] for r in current) for k in TARGETS}
    output=dict(projected_games=n+remaining)
    for key in TARGETS:
        pace=totals[key]/n
        prior_rate=sum(r[key] for r in prior)/len(prior) if prior else pace
        shrunk=(totals[key]+pseudo_games*prior_rate)/(n+pseudo_games)
        output[f'pace_{key}']=totals[key]+remaining*pace
        output[f'prior_shrinkage_{key}']=totals[key]+remaining*shrunk
    return output


def player_datasets(data,completed,now):
    games={g['game_id']:g for g in data['games']}
    season_games=defaultdict(list)
    players=defaultdict(list)
    for game in games.values():
        season_games[game['season_id']].append(game)
    for row in data['skaters']:
        players[(row['season_id'],row['player_id'])].append(row)
    inputs,labels,predictions=[],[],[]
    for season,fixtures in sorted(season_games.items()):
        if season in completed:
            first=min(g['game_date'] for g in fixtures)
            last=max(g['game_date'] for g in fixtures)
            # Fixed calendar sampling avoids conditioning samples on future
            # player appearances or their eventual season totals.
            cutoffs=[]
            month=date(first.year,first.month,1)
            while month<=last:
                cutoff=datetime.combine(month,time(12),LEAGUE_TZ).astimezone(timezone.utc)
                if first<month<=last:
                    cutoffs.append(cutoff)
                month=date(month.year+month.month//12,month.month%12+1,1)
        else:
            cutoffs=[now]
        season_players={p:rows for (s,p),rows in players.items() if s==season}
        for cutoff in cutoffs:
            played=Counter()
            remaining=Counter()
            for game in fixtures:
                if game['is_final'] and game['assumed_available_at']<cutoff:
                    played.update((game['home_team_id'],game['away_team_id']))
                else:
                    remaining.update((game['home_team_id'],game['away_team_id']))
            for player,all_rows in sorted(season_players.items()):
                current=sorted((r for r in all_rows if r['assumed_available_at']<cutoff),key=lambda r:(r['game_date'],r['game_id']))
                if len(current)<PARAMETERS['min_player_games']:
                    continue
                last=current[-1]
                game=games[last['game_id']]
                team=game['home_team_id'] if last['home_road']=='H' else game['away_team_id']
                # A traded player can have more appearances than his current
                # team's games. Clamp attendance, never inspect future trades.
                team_played=max(played[team],len(current))
                prior_season=(season//10000-1)*10000+season//10000
                prior=[r for r in players.get((prior_season,player),[]) if r['assumed_available_at']<cutoff] if prior_season in completed else []
                key=dict(player_id=player,season_id=season,prediction_at=cutoff)
                totals={k:sum(r[k] for r in current) for k in TARGETS}
                prior_totals={f'prior_{k}':sum(r[k] for r in prior) if prior else None for k in TARGETS}
                inputs.append(dict(**key,data_mode=MODE,games_played=len(current),**totals,
                    team_games_played=team_played,team_games_remaining=remaining[team],last_played_team_id=team,
                    prior_games=len(prior),**prior_totals,
                    max_assumed_available_at=max(r['assumed_available_at'] for r in current+prior)))
                prediction=player_projection(current,prior,team_played,remaining[team],PARAMETERS['prior_pseudo_games'])
                predictions.append(dict(**key,data_mode=MODE,**prediction))
                if season in completed:
                    labels.append(dict(**key,data_mode=MODE,final_games=len(all_rows),**{f'final_{k}':sum(r[k] for r in all_rows) for k in TARGETS}))
    return inputs,labels,predictions


def player_scores(labels,predictions):
    truth={(r['player_id'],r['season_id'],r['prediction_at']):r for r in labels}
    groups=defaultdict(list)
    for row in predictions:
        label=truth.get((row['player_id'],row['season_id'],row['prediction_at']))
        if label:
            groups[row['season_id']].append((row,label))
    return {str(season):dict(projected_games=regression_metrics((p['projected_games'],y['final_games']) for p,y in rows),
        **{model:{target:regression_metrics((p[f'{model}_{target}'],y[f'final_{target}']) for p,y in rows) for target in TARGETS}
           for model in ('pace','prior_shrinkage')}) for season,rows in sorted(groups.items())}


def elo_probability(home,away,advantage=55):
    return 1/(1+10**((away-home-advantage)/400))


def elo_predictions(games,now,parameters=None):
    parameters=parameters or PARAMETERS
    ratings=defaultdict(lambda:1500.0)
    pending=[]
    rows=[]
    season=None
    updated=0
    max_available=None
    past_wins=Counter()
    past_counts=Counter()
    # Preserve roster continuity across Arizona -> Utah, while retaining NHL
    # team IDs in every export. Seattle begins at the neutral rating.
    franchise=lambda team:FRANCHISE_MAP.get(team,team)
    events=[]
    for game in games:
        cutoff=game['start_time_utc']-timedelta(hours=1)
        # Unknown future results are not used. Upcoming probabilities use the
        # ratings known now; there is no speculative intervening-game update.
        if cutoff>now:
            cutoff=now
        events.append((cutoff,game['start_time_utc'],game['game_id'],game))
    for cutoff,_,_,game in sorted(events):
        while pending and pending[0][0]<cutoff:
            available,_,home,away,delta,played_season,won=heapq.heappop(pending)
            ratings[home]+=delta
            ratings[away]-=delta
            updated+=1
            max_available=available
            past_wins[played_season]+=won
            past_counts[played_season]+=1
        if season!=game['season_id']:
            season=game['season_id']
            for team in list(ratings):
                ratings[team]=1500+parameters['elo_season_retention']*(ratings[team]-1500)
        home,away=map(franchise,(game['home_team_id'],game['away_team_id']))
        probability=elo_probability(ratings[home],ratings[away],parameters['elo_home_advantage'])
        training_n=sum(n for s,n in past_counts.items() if s<season)
        home_baseline=sum(n for s,n in past_wins.items() if s<season)/training_n if training_n else 0.5
        rows.append(dict(game_id=game['game_id'],season_id=season,prediction_at=cutoff,data_mode=MODE,
            home_team_id=game['home_team_id'],away_team_id=game['away_team_id'],
            home_rating=ratings[home],away_rating=ratings[away],home_win_probability=probability,
            historical_home_win_probability=home_baseline,baseline_training_games=training_n,
            games_updated_before_prediction=updated,max_assumed_available_at=max_available))
        if game['is_final'] and game['assumed_available_at']<now:
            if game['assumed_available_at']<=game['start_time_utc']:
                raise ValueError('Assumed result availability precedes game start')
            won=int(game['winner_team_id']==game['home_team_id'])
            # Elo innovation uses the pregame probability. No goal margin,
            # shots or score from the target game enters its forecast.
            delta=parameters['elo_k']*(won-probability)
            heapq.heappush(pending,(game['assumed_available_at'],game['game_id'],home,away,delta,season,won))
    while pending and pending[0][0]<now:
        _,_,home,away,delta,_,_=heapq.heappop(pending)
        ratings[home]+=delta
        ratings[away]-=delta
    return rows,dict(sorted(ratings.items()))


def elo_scores(predictions,games,completed):
    truth={g['game_id']:int(g['winner_team_id']==g['home_team_id']) for g in games if g['is_final']}
    return {str(season):{model:probability_metrics((r[column],truth[r['game_id']]) for r in predictions
        if r['season_id']==season and r['game_id'] in truth)
        for model,column in [('elo','home_win_probability'),('historical_home_rate','historical_home_win_probability')]}
        for season in completed}


def write_csv(path,rows,fields=None):
    fields=fields or (list(rows[0]) if rows else [])
    temporary=path.with_suffix('.csv.tmp')
    with temporary.open('w',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def run_baselines(history,output,now):
    data,source=load_history(history,columns={
        'skaters':['player_id','game_id','season_id','game_date','home_road','team_abbrev',
                   'goals','assists','points','assumed_available_at'],
        'goalies':['player_id','game_id','season_id','home_road','team_abbrev']})
    completed=validate_history(data,source['seasons'])
    coverage={str(season):dict(games=sum(g['season_id']==season for g in data['games']),
        final_games=sum(g['season_id']==season and g['is_final'] for g in data['games']),
        **{kind:dict(rows=sum(r['season_id']==season for r in data[kind]),
                    players=len({r['player_id'] for r in data[kind] if r['season_id']==season}))
           for kind in ('skaters','goalies')}) for season in source['seasons']}
    if len(completed)<2:
        raise ValueError('At least two complete seasons are required for chronological evaluation')
    starts=sorted({s//10000 for s in source['seasons']})
    if starts!=list(range(starts[0],starts[-1]+1)):
        raise ValueError('Elo history must include consecutive seasons; do not skip intervening results')
    # Freeze evaluation at the reconstruction's retrieval time. A supplied
    # earlier cutoff cannot silently claim knowledge of later snapshots.
    if now<timestamp(source['generated_at']):
        raise ValueError('Evaluation clock must be at or after reconstruction creation')
    output.mkdir(parents=True,exist_ok=True)
    if (output/'manifest.json').exists():
        raise ValueError('Use a new baseline output directory')
    inputs,labels,projections=player_datasets(data,completed,now)
    elo,ratings=elo_predictions(data['games'],now)
    for row in inputs+elo:
        if row['max_assumed_available_at'] is not None and row['max_assumed_available_at']>=row['prediction_at']:
            raise ValueError('Reconstructed feature availability violates prediction cutoff')
    future_labels=[dict(game_id=g['game_id'],season_id=g['season_id'],data_mode=MODE,home_win=int(g['winner_team_id']==g['home_team_id']))
                   for g in data['games'] if g['is_final']]
    artifacts={}
    for name,rows in [('player_inputs',inputs),('player_labels',labels),('player_predictions',projections),
                      ('elo_inputs_predictions',elo),('elo_labels',future_labels)]:
        write_csv(output/f'{name}.csv',rows)
        artifacts[name]=dict(rows=len(rows),sha256=file_hash(output/f'{name}.csv'))
    evaluation=dict(data_mode=MODE,player=player_scores(labels,projections),elo=elo_scores(elo,data['games'],completed),
        evaluation_policy='Forward chronological seasons; parameters fixed in advance, no random split or tuning on held-out seasons.',
        warmup_season=min(completed),final_holdout_season=max(completed),
        player_weighting='Each monthly player snapshot has equal weight; repeated snapshots are correlated, not independent trials.')
    write_document(output/'coverage.json',dict(status='pass',data_mode=MODE,seasons=coverage,
        checks=['Unique linked player/game keys','Complete 82-game schedules for requested historical seasons',
                'At least 30 skater and two goalie appearances per final game','Matching historical player/game teams',
                'Non-null scoring targets and points = goals + assists']))
    write_document(output/'evaluation.json',evaluation)
    implementation_sha256=file_hash(Path(__file__))
    write_document(output/'model.json',dict(version=1,data_mode=MODE,parameters=PARAMETERS,implementation_sha256=implementation_sha256,
        fixed_parameters=True,ratings_as_of=now,franchise_ratings=ratings,
        franchise_mapping=FRANCHISE_MAP,reconstruction_manifest_sha256=file_hash(history/'manifest.json')))
    manifest=dict(status='complete',version=1,data_mode=MODE,history=str(history.resolve()),
        implementation_sha256=implementation_sha256,
        history_manifest_sha256=file_hash(history/'manifest.json'),as_of=now,artifacts=artifacts,
        availability_policy=source['availability_policy'],completed_seasons=completed,
        limitations=source['limitations']+[
            'Player games remaining assumes attendance rate continues on last played team; injuries, future trades and demotions are unknown.',
            'Historical fixture counts use the latest full schedule, including later rescheduling; schedule availability is reconstructed.',
            'Prior season rates use only the immediately preceding complete season; missing priors fall back to current pace.',
            'Elo predicts any win including overtime/shootout; playoffs and playoff simulation are excluded.',
            'Upcoming-game probabilities hold current ratings fixed; they are not simulated future rating paths.',
            'No prediction intervals or claimed betting edge; these are benchmark models.'])
    report=['# Reconstructed baseline evaluation','',
        'These retrospective benchmarks use latest corrected history and assumed next-day publication. They are separate from strict point-in-time exports.','',
        f'Final chronological holdout: {max(completed)}. Parameters fixed before evaluation.','',
        '| Season | Elo games | Elo Brier | Home-rate Brier | Pace points MAE | Shrunk points MAE |',
        '| --- | ---: | ---: | ---: | ---: | ---: |']
    for season in completed:
        e=evaluation['elo'][str(season)]
        p=evaluation['player'].get(str(season),{})
        report.append(f"| {season} | {e['elo']['n']} | {e['elo']['brier']:.4f} | {e['historical_home_rate']['brier']:.4f} | {p.get('pace',{}).get('points',{}).get('mae',float('nan')):.2f} | {p.get('prior_shrinkage',{}).get('points',{}).get('mae',float('nan')):.2f} |")
    (output/'evaluation.md').write_text('\n'.join(report)+'\n',encoding='utf-8')
    for name in ('model.json','evaluation.json','evaluation.md','coverage.json'):
        artifacts[name]=dict(sha256=file_hash(output/name))
    # Publish completion only after every artifact has been written.
    write_document(output/'manifest.json',manifest)
    return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history',type=Path,required=True,help='Complete nhl-reconstruct output directory')
    parser.add_argument('--output',type=Path,default=Path('data/baselines/reconstructed'))
    parser.add_argument('--as-of',type=timestamp,default=None)
    args=parser.parse_args()
    now=args.as_of or datetime.now(timezone.utc)
    output=args.output/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    manifest=run_baselines(args.history,output,now)
    print(json.dumps(dict(output=str(output),status=manifest['status'],completed_seasons=manifest['completed_seasons']),indent=2))


if __name__=='__main__':
    main()
