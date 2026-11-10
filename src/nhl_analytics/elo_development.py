"""Development-only Elo selection; reserved holdout outcomes cannot enter selection."""

from collections import Counter
from itertools import product

from nhl_analytics.baselines import PARAMETERS,elo_predictions,probability_metrics
from nhl_analytics.asof import LEAGUE_TZ


def candidate_grid():
    return [dict(PARAMETERS,elo_k=k,elo_home_advantage=h,elo_season_retention=r)
            for k,h,r in product((10.0,20.0,40.0),(0.0,25.0,55.0),(0.0,0.5,0.75,1.0))]


def select_elo(games,now,holdout):
    # Restrict the data before constructing probabilities, scores or rankings.
    development=[g for g in games if g['season_id']<holdout and g['is_final']]
    seasons=sorted({g['season_id'] for g in development})
    if len(seasons)<3:
        raise ValueError('Need warmup and at least two completed development seasons')
    truth={g['game_id']:int(g['winner_team_id']==g['home_team_id']) for g in development}
    rows=[]
    for number,parameters in enumerate(candidate_grid()):
        forecasts,_=elo_predictions(development,now,parameters)
        for season in seasons[1:]:
            metrics=probability_metrics((r['home_win_probability'],truth[r['game_id']]) for r in forecasts if r['season_id']==season)
            rows.append(dict(candidate=number,season_id=season,**{k:v for k,v in parameters.items() if k.startswith('elo_')},**metrics))
    def best(earlier):
        ranked=[]
        for number,p in enumerate(candidate_grid()):
            scores=[r for r in rows if r['candidate']==number and r['season_id'] in earlier]
            ranked.append((sum(r['brier'] for r in scores)/len(scores),
                           sum(r['log_loss'] for r in scores)/len(scores),number))
        return min(ranked)[2]
    winner=best(seasons[1:])
    folds=[]
    for season in seasons[2:]:
        earlier=[s for s in seasons[1:] if s<season]
        choice=best(earlier)
        score=next(r for r in rows if r['candidate']==choice and r['season_id']==season)
        folds.append(dict(evaluation_season=season,selection_seasons=','.join(map(str,earlier)),**score))
    selection=dict(parameters=candidate_grid()[winner],candidate=winner,selection_seasons=seasons[1:],
        warmup_season=seasons[0],reserved_holdout=holdout,criterion='Mean development-season Brier, log-loss tie-break, then deterministic grid order',
        excluded_holdout_from_selection=True,
        warning='In-sample candidate ranking is optimistic; forward development folds are reported separately. Holdout was previously inspected, not pristine unseen data.')
    return selection,rows,folds


def contextualize(predictions,games):
    available=sorted((g['assumed_available_at'],g['game_id'],g) for g in games if g['is_final'])
    position=0
    played=Counter()
    outputs=[]
    for row in sorted(predictions,key=lambda r:(r['prediction_at'],r['game_id'])):
        cutoff=row['prediction_at']
        while position<len(available) and available[position][0]<cutoff:
            g=available[position][2]
            played[g['season_id'],g['home_team_id']]+=1
            played[g['season_id'],g['away_team_id']]+=1
            position+=1
        progress=(played[row['season_id'],row['home_team_id']]+played[row['season_id'],row['away_team_id']])/2
        diff=row['home_rating']-row['away_rating']
        bucket='<-150' if diff< -150 else '-150:-50' if diff< -50 else '-50:50' if diff<50 else '50:150' if diff<150 else '150+'
        outputs.append(dict(row,month=cutoff.astimezone(LEAGUE_TZ).strftime('%Y-%m'),
            rating_difference=diff,rating_bucket=bucket,stage='early_0-20' if progress<=20 else 'middle_21-60' if progress<=60 else 'late_61+',
            team_games_known_before_prediction=progress))
    return outputs
