"""Local read-only HTTP API for a pinned saved prediction bundle."""

import argparse
import os
from contextlib import asynccontextmanager
from datetime import date,datetime,timezone
from pathlib import Path
from typing import Annotated,Generic,Literal,TypeVar

from dotenv import load_dotenv
from fastapi import Depends,FastAPI,HTTPException,Query,Request
from fastapi import Path as APIPath
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from pydantic import AwareDatetime,BaseModel,BeforeValidator

from nhl_analytics.prediction_store import PredictionStore,discover_run,instant

Model=Literal['pace','prior_shrinkage']
Target=Literal['goals','assists','points','games']
Nominal=Literal[0.8,0.95]
NominalQuery=Annotated[Nominal,BeforeValidator(float)]
Limit=Annotated[int,Query(ge=1,le=200)]
Offset=Annotated[int,Query(ge=0,le=2**31-1)]
Positive=Annotated[int,Query(gt=0,le=2**63-1)]
Identifier=Annotated[int,APIPath(gt=0,le=2**63-1)]


class Provenance(BaseModel):
    bundle_id:str
    bundle_sha256:str
    baseline_id:str
    evaluation_version:str
    data_mode:Literal['reconstructed_latest_corrected']
    as_of:AwareDatetime
    availability_policy:str
    age_hours:float
    stale:bool
    stale_after_hours:int
    limitations:list[str]


class Snapshot(BaseModel):
    season_id:int
    prediction_at:AwareDatetime
    players:int


class BundleInfo(Provenance):
    seasons:list[int]
    snapshots:list[Snapshot]
    row_counts:dict[str,int]
    elo_model:str
    elo_parameters:dict[str,float]
    franchise_mapping:dict[str,int]


class Totals(BaseModel):
    goals:float
    assists:float
    points:float
    games:float


class ObservedTotals(BaseModel):
    goals:int
    assists:int
    points:int
    games:int


class PlayerProjection(BaseModel):
    player_id:int
    season_id:int
    prediction_at:AwareDatetime
    model:Model
    last_played_team_id:int
    observed:ObservedTotals
    projected:Totals
    team_games_remaining:int
    prior_games:int
    max_assumed_available_at:AwareDatetime


class PlayerInterval(BaseModel):
    player_id:int
    season_id:int
    prediction_at:AwareDatetime
    model:Model
    target:Target
    nominal:Nominal
    observed:int
    prediction:float
    available:bool
    lower:int|None
    upper:int|None
    calibration_players:int
    calibration_seasons:list[int]
    max_calibration_label_available_at:AwareDatetime|None
    calibration_scope:str
    radius:float|None


class GamePrediction(BaseModel):
    game_id:int
    season_id:int
    game_date:date
    start_time_utc:AwareDatetime
    prediction_at:AwareDatetime
    model:Literal['development_selected']
    home_team_id:int
    away_team_id:int
    home_team_abbrev:str
    away_team_abbrev:str
    home_rating:float
    away_rating:float
    home_win_probability:float
    away_win_probability:float
    historical_home_win_probability:float
    snapshot_status:Literal['final','scheduled','unresolved']
    max_assumed_available_at:AwareDatetime|None


T=TypeVar('T')


class Page(BaseModel,Generic[T]):
    metadata:Provenance
    snapshot_at:AwareDatetime|None=None
    total:int
    limit:int
    offset:int
    items:list[T]


class Detail(BaseModel,Generic[T]):
    metadata:Provenance
    item:T


def get_store(request:Request):
    store=getattr(request.app.state,'predictions',None)
    if store is None:raise HTTPException(503,'Saved predictions are not loaded')
    return store


Store=Annotated[PredictionStore,Depends(get_store)]


def provenance(store):
    age=max(0,(datetime.now(timezone.utc)-datetime.fromisoformat(store.metadata['as_of'])).total_seconds()/3600)
    return dict(store.metadata,age_hours=round(age,3),stale=age>24,stale_after_hours=24)


def snapshot(store,season,prediction_at):
    if season is not None and (season//10000+1!=season%10000 or season<19001901):
        raise HTTPException(422,'season must be consecutive NHL years, e.g. 20262027')
    if prediction_at is not None and prediction_at.tzinfo is None:
        raise HTTPException(422,'prediction_at must include a timezone')
    try:return store.snapshot(season,prediction_at)
    except LookupError as error:raise HTTPException(404,str(error)) from error


PROJECTIONS='''(SELECT p.*,i.team,i.games AS observed_games,i.goals AS observed_goals,i.assists AS observed_assists,
 i.points AS observed_points,i.remaining,i.prior_games,i.available_at
 FROM projections p JOIN inputs i USING(season,prediction_at,player))'''
INTERVALS='(SELECT b.*,i.team FROM intervals b JOIN inputs i USING(season,prediction_at,player))'
GAMES='(SELECT * FROM games JOIN fixtures USING(game))'


def player_item(row):
    return dict(player_id=row['player'],season_id=row['season'],prediction_at=row['prediction_at'],model=row['model'],
        last_played_team_id=row['team'],observed={t:row['observed_'+t] for t in ('goals','assists','points','games')},
        projected={t:row[t] for t in ('goals','assists','points','games')},team_games_remaining=row['remaining'],
        prior_games=row['prior_games'],max_assumed_available_at=row['available_at'])


def interval_item(row):
    return dict(player_id=row['player'],season_id=row['season'],prediction_at=row['prediction_at'],model=row['model'],
        target=row['target'],nominal=row['nominal']/100,observed=row['observed'],prediction=row['prediction'],
        available=bool(row['available']),lower=row['lower'],upper=row['upper'],calibration_players=row['calibration_players'],
        calibration_seasons=[int(s) for s in row['calibration_seasons'].split(',') if s],
        max_calibration_label_available_at=row['calibration_available_at'],calibration_scope=row['scope'],radius=row['radius'])


def game_item(row,as_of):
    return dict(game_id=row['game'],season_id=row['season'],game_date=row['game_date'],start_time_utc=row['start'],
        prediction_at=row['prediction_at'],model='development_selected',home_team_id=row['home'],away_team_id=row['away'],
        home_team_abbrev=row['home_abbrev'],away_team_abbrev=row['away_abbrev'],home_rating=row['home_rating'],away_rating=row['away_rating'],
        home_win_probability=row['home_probability'],away_win_probability=1-row['home_probability'],
        historical_home_win_probability=row['comparator'],
        snapshot_status='final' if row['is_final'] else 'scheduled' if row['start']>=as_of else 'unresolved',
        max_assumed_available_at=row['available_at'])


def player_page(store,season,model,prediction_at,team_id,player_id,limit,offset):
    season,cutoff=snapshot(store,season,prediction_at)
    where='season=? AND prediction_at=? AND model=?';params=[season,cutoff,model]
    if team_id is not None:where+=' AND team=?';params.append(team_id)
    if player_id is not None:where+=' AND player=?';params.append(player_id)
    total,rows=store.page(PROJECTIONS,where,params,'player',limit,offset)
    return dict(metadata=provenance(store),snapshot_at=cutoff,total=total,limit=limit,offset=offset,items=[player_item(r) for r in rows])


def interval_page(store,season,model,prediction_at,team_id,player_id,target,nominal,limit,offset):
    season,cutoff=snapshot(store,season,prediction_at)
    where='season=? AND prediction_at=? AND model=?';params=[season,cutoff,model]
    for column,value in [('team',team_id),('player',player_id),('target',target),('nominal',round(nominal*100) if nominal else None)]:
        if value is not None:where+=f' AND {column}=?';params.append(value)
    total,rows=store.page(INTERVALS,where,params,'player,target,nominal',limit,offset)
    return dict(metadata=provenance(store),snapshot_at=cutoff,total=total,limit=limit,offset=offset,items=[interval_item(r) for r in rows])


def create_app(run_dir:Path|None=None,cache_dir:Path|None=None):
    """App factory. Artifact loading happens once in lifespan, never in a route."""
    load_dotenv(override=False)
    @asynccontextmanager
    async def lifespan(app):
        load_dotenv(override=False)
        configured=run_dir or os.environ.get('NHL_PREDICTIONS_RUN')
        run=Path(configured) if configured else discover_run()
        cache=Path(cache_dir or os.environ.get('NHL_API_CACHE_DIR','.work/api-cache'))
        app.state.predictions=await run_in_threadpool(PredictionStore.load,run,cache)
        try:yield
        finally:app.state.predictions=None
    app=FastAPI(title='NHL Prediction API',version='1.0.0',lifespan=lifespan,
        description='Saved player and improved Elo forecasts. Reconstructed provenance, not a live NHL feed. No request-time training.')
    origins=[v.strip() for v in os.environ.get('NHL_API_CORS_ORIGINS','').split(',') if v.strip()]
    if origins:app.add_middleware(CORSMiddleware,allow_origins=origins,allow_methods=['GET'],allow_headers=['Accept','Content-Type'])

    @app.get('/health')
    def health(store:Store):
        return dict(status='ok',bundle_id=store.metadata['bundle_id'],as_of=store.metadata['as_of'],stale=provenance(store)['stale'])

    @app.get('/api/metadata',response_model=BundleInfo)
    def metadata(store:Store):return provenance(store)

    @app.get('/api/predictions/players',response_model=Page[PlayerProjection])
    def players(store:Store,season:int|None=None,model:Model='prior_shrinkage',prediction_at:datetime|None=None,
                team_id:Positive|None=None,player_id:Positive|None=None,limit:Limit=50,offset:Offset=0):
        return player_page(store,season,model,prediction_at,team_id,player_id,limit,offset)

    @app.get('/api/predictions/players/{player_id}',response_model=Detail[PlayerProjection])
    def player(player_id:Identifier,store:Store,season:int|None=None,model:Model='prior_shrinkage',prediction_at:datetime|None=None):
        result=player_page(store,season,model,prediction_at,None,player_id,1,0)
        if not result['items']:raise HTTPException(404,'Player has no saved projection for this snapshot')
        return dict(metadata=result['metadata'],item=result['items'][0])

    @app.get('/api/predictions/intervals',response_model=Page[PlayerInterval])
    def intervals(store:Store,season:int|None=None,model:Model='prior_shrinkage',prediction_at:datetime|None=None,
                  team_id:Positive|None=None,player_id:Positive|None=None,target:Target|None=None,nominal:NominalQuery|None=None,
                  limit:Limit=100,offset:Offset=0):
        return interval_page(store,season,model,prediction_at,team_id,player_id,target,nominal,limit,offset)

    @app.get('/api/predictions/players/{player_id}/intervals',response_model=Page[PlayerInterval])
    def player_intervals(player_id:Identifier,store:Store,season:int|None=None,model:Model='prior_shrinkage',prediction_at:datetime|None=None,
                         target:Target|None=None,nominal:NominalQuery|None=None):
        result=interval_page(store,season,model,prediction_at,None,player_id,target,nominal,8,0)
        if not result['items']:raise HTTPException(404,'Player has no saved intervals for this snapshot')
        return result

    @app.get('/api/predictions/games',response_model=Page[GamePrediction])
    def games(store:Store,season:int|None=None,team_id:Positive|None=None,status:Literal['upcoming','final','all']='upcoming',
              date_from:date|None=None,date_to:date|None=None,limit:Limit=50,offset:Offset=0):
        season=season if season is not None else max(store.metadata['seasons'])
        if season//10000+1!=season%10000 or season<19001901:raise HTTPException(422,'Invalid NHL season')
        if date_from and date_to and date_from>date_to:raise HTTPException(422,'date_from must not be after date_to')
        where='season=?';params=[season]
        if team_id is not None:where+=' AND (home=? OR away=?)';params.extend([team_id,team_id])
        if status=='upcoming':where+=' AND is_final=0 AND start>=?';params.append(store.metadata['as_of'])
        elif status=='final':where+=' AND is_final=1'
        if date_from:where+=' AND game_date>=?';params.append(date_from.isoformat())
        if date_to:where+=' AND game_date<=?';params.append(date_to.isoformat())
        total,rows=store.page(GAMES,where,params,'start,game',limit,offset)
        return dict(metadata=provenance(store),total=total,limit=limit,offset=offset,items=[game_item(r,store.metadata['as_of']) for r in rows])

    @app.get('/api/predictions/games/{game_id}',response_model=Detail[GamePrediction])
    def game(game_id:Identifier,store:Store):
        total,rows=store.page(GAMES,'game=?',[game_id],'game',1,0)
        if not total:raise HTTPException(404,'Game has no saved improved Elo prediction')
        return dict(metadata=provenance(store),item=game_item(rows[0],store.metadata['as_of']))

    return app


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,help='Pin a complete nhl-evaluate run; defaults to NHL_PREDICTIONS_RUN or newest complete run')
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8000)
    args=parser.parse_args()
    import uvicorn
    uvicorn.run(create_app(args.run),host=args.host,port=args.port,workers=1)


if __name__=='__main__':main()
