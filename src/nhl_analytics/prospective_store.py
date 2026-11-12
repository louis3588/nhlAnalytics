"""Immutable forecast pairs and independent, correction-aware final outcomes."""

import json
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from nhl_analytics.asof import LEAGUE_TZ
from nhl_analytics.live_store import digest


def jsonable(value):
    def default(item):
        if isinstance(item,(datetime,date)):return item.isoformat()
        if isinstance(item,UUID):return str(item)
        raise TypeError(type(item).__name__)
    return json.loads(json.dumps(value,default=default))


def jsonb(value):return Jsonb(jsonable(value))


class ProspectiveRepository:
    def __init__(self,connection):self.connection=connection

    def rows(self,query,params=()):
        with self.connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query,params);return cursor.fetchall()

    def clock(self):return self.connection.execute('SELECT clock_timestamp()').fetchone()[0]

    def acquire(self):return self.connection.execute('SELECT pg_try_advisory_lock(768432110)').fetchone()[0]

    def campaign(self,name):
        rows=self.rows('SELECT * FROM analytics.prospective_campaigns WHERE campaign=%s',(name,))
        return rows[0] if rows else None

    def register(self,name,bundle):
        existing=self.campaign(name)
        model_set=digest(dict(campaign=name,elo=bundle['elo_version'],logistic=bundle['logistic_version'],policy=bundle['policy']))
        if existing:
            if existing['model_set_id']!=model_set:raise ValueError('Campaign is already pinned to different model artifacts')
            return existing
        now=self.clock()
        self.connection.execute('''INSERT INTO analytics.prospective_campaigns
            (campaign,model_set_id,tracking_date,season_id,elo_version,logistic_version,bundle,policy)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
            (name,model_set,now.astimezone(LEAGUE_TZ).date(),bundle['season_id'],bundle['elo_version'],bundle['logistic_version'],jsonb(bundle),jsonb(bundle['policy'])))
        return self.campaign(name)

    def discover(self,campaign,now):
        rows=self.rows('''SELECT g.game_id,g.season_id,g.game_date,g.home_team_id,g.away_team_id,g.start_time_utc,
            g.game_state,g.schedule_state,g.schedule_checked_at,l.payload,l.checked_at AS live_checked_at
            FROM analytics.games g LEFT JOIN analytics.live_games l USING(game_id)
            WHERE g.season_id=%s AND g.game_type_id=2 AND g.game_date>=%s
                AND g.start_time_utc<=%s ORDER BY g.start_time_utc,g.game_id''',
            (campaign['season_id'],campaign['tracking_date'],now+timedelta(days=2)))
        output=[]
        for row in rows:
            live=row['payload']['view'] if row['payload'] and row['live_checked_at'] and (not row['schedule_checked_at'] or row['live_checked_at']>=row['schedule_checked_at']) else None
            fixture=dict(game_id=row['game_id'],season_id=row['season_id'],game_date=date.fromisoformat(live['game_date']) if live else row['game_date'],
                home_team_id=live['home']['id'] if live else row['home_team_id'],away_team_id=live['away']['id'] if live else row['away_team_id'],
                scheduled_start=datetime.fromisoformat(live['start_time_utc'].replace('Z','+00:00')) if live else row['start_time_utc'],
                schedule_state=(live.get('schedule_state') or 'TBD') if live else row['schedule_state'] or 'TBD',
                game_state=live['game_state'] if live else row['game_state'] or 'FUT',
                provider_checked_at=row['live_checked_at'] if live else row['schedule_checked_at'])
            if not fixture['provider_checked_at'] or not fixture['home_team_id'] or not fixture['away_team_id']:continue
            output.append(self.observe_fixture(fixture))
        return output

    def observe_fixture(self,item):
        item=dict(item);item['cutoff_at']=item['scheduled_start']-timedelta(hours=1)
        identity={k:item[k] for k in ('game_id','season_id','game_date','home_team_id','away_team_id','scheduled_start','schedule_state')}
        fid=digest(jsonable(identity));item['fixture_id']=fid
        self.connection.execute('''INSERT INTO analytics.prospective_fixtures
            (fixture_id,game_id,season_id,home_team_id,away_team_id,scheduled_start,cutoff_at,game_date,schedule_state,source_observed_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
            (fid,item['game_id'],item['season_id'],item['home_team_id'],item['away_team_id'],item['scheduled_start'],item['cutoff_at'],item['game_date'],item['schedule_state'],item['provider_checked_at']))
        self.connection.execute('''INSERT INTO analytics.prospective_fixture_heads(game_id,fixture_id,provider_checked_at,game_state)
            VALUES (%s,%s,%s,%s) ON CONFLICT(game_id) DO UPDATE SET fixture_id=EXCLUDED.fixture_id,
            provider_checked_at=EXCLUDED.provider_checked_at,game_state=EXCLUDED.game_state,checked_at=clock_timestamp()
            WHERE EXCLUDED.provider_checked_at>=prospective_fixture_heads.provider_checked_at''',
            (item['game_id'],fid,item['provider_checked_at'],item['game_state']))
        return item

    def batch(self,campaign,fid):
        rows=self.rows('SELECT * FROM analytics.prospective_batches WHERE campaign=%s AND fixture_id=%s',(campaign,fid))
        return rows[0] if rows else None

    def attempt(self,campaign,fid,reason):
        self.connection.execute('INSERT INTO analytics.prospective_attempts(campaign,fixture_id,reason) VALUES (%s,%s,%s)',(campaign,fid,reason))

    def missing(self,campaign,fixture,reason):
        with self.connection.transaction():
            self.connection.execute('''INSERT INTO analytics.prospective_batches(campaign,fixture_id,status,timing,reason,provenance)
                VALUES (%s,%s,'missing','none',%s,%s) ON CONFLICT DO NOTHING''',
                (campaign,fixture['fixture_id'],reason,jsonb(dict(record_kind='coverage_only',prediction_generated=False))))
        return self.batch(campaign,fixture['fixture_id'])

    def seal(self,campaign,fixture,features,elo,logistic,feature_as_of,latest,provenance,timing):
        with self.connection.transaction():
            # Recheck discovery identity before sealing; never overwrite an old fixture.
            head=self.rows('SELECT * FROM analytics.prospective_fixture_heads WHERE game_id=%s FOR UPDATE',(fixture['game_id'],))[0]
            existing=self.batch(campaign,fixture['fixture_id'])
            if existing:return existing
            if head['fixture_id']!=fixture['fixture_id'] or head['game_state'] not in ('FUT','PRE'):
                raise ValueError('Fixture changed or game already started')
            # Discovery can race a collector update during feature extraction.
            # Recheck only schedule/state metadata, never target-game statistics.
            live=self.rows('SELECT payload,checked_at FROM analytics.live_games WHERE game_id=%s FOR SHARE',(fixture['game_id'],))
            if live and live[0]['checked_at']>=fixture['provider_checked_at']:
                view=live[0]['payload']['view']
                start=datetime.fromisoformat(view['start_time_utc'].replace('Z','+00:00'))
                if (view['game_state'] not in ('FUT','PRE') or view.get('schedule_state')!='OK' or start!=fixture['scheduled_start']
                    or view['home']['id']!=fixture['home_team_id'] or view['away']['id']!=fixture['away_team_id']):
                    raise ValueError('Latest provider check changed the fixture or started the game')
            self.connection.execute('''INSERT INTO analytics.prospective_batches(campaign,fixture_id,status,timing,
                feature_as_of,max_source_available_at,feature_sha256,features,provenance,elo_home_probability,logistic_home_probability)
                VALUES (%s,%s,'sealed',%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                (campaign,fixture['fixture_id'],timing,feature_as_of,latest,digest(jsonable(features)),jsonb(features),jsonb(provenance),elo,logistic))
        return self.batch(campaign,fixture['fixture_id'])

    def observe_results(self,campaign):
        games=self.rows('''SELECT l.game_id,g.season_id,l.payload,l.checked_at,l.content_sha256 FROM analytics.live_games l
            JOIN analytics.games g USING(game_id) WHERE g.season_id=%s AND g.game_type_id=2 AND g.game_date>=%s
            AND (l.game_state IN ('OFF','FINAL') OR EXISTS(SELECT 1 FROM analytics.prospective_result_samples s WHERE s.game_id=l.game_id))
            AND NOT EXISTS(SELECT 1 FROM analytics.prospective_result_samples s WHERE s.game_id=l.game_id AND s.checked_at>=l.checked_at)''',
            (campaign['season_id'],campaign['tracking_date']))
        for row in games:
            if row['checked_at']>self.clock():continue
            sample=final_sample(row)
            self.connection.execute('''INSERT INTO analytics.prospective_result_samples(game_id,checked_at,source_sha256,status,
                scheduled_start,home_team_id,away_team_id,home_score,away_score,winner_team_id,validation)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                (row['game_id'],row['checked_at'],sample['hash'],sample['status'],sample['start'],sample['home'],sample['away'],
                 sample['home_score'],sample['away_score'],sample['winner'],jsonb(sample['validation'])))
            if sample['status']!='candidate_final':continue
            previous=self.rows('SELECT * FROM analytics.prospective_result_samples WHERE game_id=%s ORDER BY checked_at DESC LIMIT 128',(row['game_id'],))
            matching=[]
            for value in previous:
                if value['status']!='candidate_final' or value['source_sha256']!=sample['hash']:break
                matching.append(value)
            first=min((r['checked_at'] for r in matching),default=row['checked_at'])
            if len(matching)<2 or row['checked_at']<first+timedelta(minutes=10):continue
            latest=self.rows('SELECT source_sha256 FROM analytics.prospective_validated_results WHERE game_id=%s ORDER BY checked_at DESC LIMIT 1',(row['game_id'],))
            if latest and latest[0]['source_sha256']==sample['hash']:continue
            self.connection.execute('''INSERT INTO analytics.prospective_validated_results(game_id,checked_at,first_confirmed_at,
                source_sha256,scheduled_start,home_team_id,away_team_id,home_score,away_score,winner_team_id,validation)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                (row['game_id'],row['checked_at'],first,sample['hash'],sample['start'],sample['home'],sample['away'],sample['home_score'],sample['away_score'],sample['winner'],
                 jsonb(dict(sample['validation'],confirmations=len(matching),stability_seconds=(row['checked_at']-first).total_seconds()))))

    def recover_results(self,campaign,now):
        # Ask the existing collector for a bounded number of older unresolved games
        # after an outage. This never creates a forecast or another NHL poller.
        rows=self.rows('''SELECT DISTINCT f.game_id FROM analytics.prospective_fixtures f
            JOIN analytics.prospective_batches b USING(fixture_id)
            WHERE b.campaign=%s AND b.status='sealed' AND f.scheduled_start<%s
            AND NOT EXISTS(SELECT 1 FROM analytics.prospective_validated_results r WHERE r.game_id=f.game_id)
            AND NOT EXISTS(SELECT 1 FROM analytics.live_game_requests q WHERE q.game_id=f.game_id AND q.expires_at>%s)
            ORDER BY f.game_id DESC LIMIT 8''',(campaign['campaign'],now-timedelta(days=1),now))
        from nhl_analytics.live_store import LiveRepository
        live=LiveRepository(self.connection)
        for row in rows:live.request(row['game_id'],now)

    def snapshot(self,campaign):
        rows=self.rows('''SELECT f.*,h.game_state,h.provider_checked_at,b.batch_id,b.status,b.timing,b.reason,
            b.recorded_at AS generated_at,b.feature_as_of,b.max_source_available_at,b.feature_sha256,
            b.elo_home_probability,b.logistic_home_probability,
            r.result_id,r.checked_at AS result_checked_at,r.source_sha256 AS result_hash,r.home_score,r.away_score,r.winner_team_id,
            r.scheduled_start AS result_start,r.home_team_id AS result_home,r.away_team_id AS result_away,
            s.status AS result_sample_status,s.source_sha256 AS sample_hash,
            a.reason AS last_attempt_reason
            FROM analytics.prospective_fixture_heads h JOIN analytics.prospective_fixtures f USING(fixture_id)
            LEFT JOIN analytics.prospective_batches b ON b.fixture_id=f.fixture_id AND b.campaign=%s
            LEFT JOIN LATERAL (SELECT * FROM analytics.prospective_validated_results WHERE game_id=f.game_id ORDER BY checked_at DESC LIMIT 1) r ON true
            LEFT JOIN LATERAL (SELECT * FROM analytics.prospective_result_samples WHERE game_id=f.game_id ORDER BY checked_at DESC LIMIT 1) s ON true
            LEFT JOIN LATERAL (SELECT reason FROM analytics.prospective_attempts WHERE fixture_id=f.fixture_id AND campaign=%s ORDER BY attempted_at DESC LIMIT 1) a ON true
            WHERE f.season_id=%s AND f.game_date>=%s ORDER BY f.scheduled_start,f.game_id''',
            (campaign['campaign'],campaign['campaign'],campaign['season_id'],campaign['tracking_date']))
        for row in rows:
            row['outcome_status']='awaiting_final'
            row['valid_for_scoring']=False
            if row['result_id']:
                match=row['result_start']==row['scheduled_start'] and row['result_home']==row['home_team_id'] and row['result_away']==row['away_team_id']
                reviewed=row['result_sample_status']=='candidate_final' and row['sample_hash']==row['result_hash']
                row['outcome_status']='validated' if match and reviewed else 'under_review'
                row['valid_for_scoring']=match and reviewed and row['status']=='sealed'
            elif row['result_sample_status']=='candidate_final':row['outcome_status']='preliminary_final'
            elif row['result_sample_status']=='conflict':row['outcome_status']='conflict'
        return rows

    def revisions(self,campaign):
        return self.rows('''SELECT f.game_id,f.fixture_id,f.scheduled_start,f.cutoff_at,f.schedule_state,
            b.batch_id,b.status,b.timing,b.reason,b.recorded_at AS generated_at,b.feature_sha256,
            b.elo_home_probability,b.logistic_home_probability,
            f.fixture_id=h.fixture_id AS current_fixture
            FROM analytics.prospective_fixtures f JOIN analytics.prospective_batches b USING(fixture_id)
            JOIN analytics.prospective_fixture_heads h USING(game_id)
            WHERE b.campaign=%s ORDER BY f.game_id,b.recorded_at''',(campaign['campaign'],))


def final_sample(row):
    envelope=row['payload'];box=envelope['boxscore'];pbp=envelope['play_by_play'];view=envelope['view']
    result=dict(start=datetime.fromisoformat(view['start_time_utc'].replace('Z','+00:00')),home=view['home']['id'],away=view['away']['id'],
        home_score=None,away_score=None,winner=None,status='nonfinal',validation={})
    states=[box.get('gameState'),pbp.get('gameState')]
    if not any(state in ('OFF','FINAL') for state in states):
        result['hash']=digest(dict(state='nonfinal',game_id=row['game_id']))
        return result
    try:
        home,away=box['homeTeam']['score'],box['awayTeam']['score']
        period=pbp.get('periodDescriptor') or {};clock=pbp.get('clock') or {}
        valid=all(s in ('OFF','FINAL') for s in states) and box['id']==pbp['id']==row['game_id'] and box['season']==pbp['season']==row.get('season_id',view.get('season_id'))
        valid=valid and type(home) is int and type(away) is int and min(home,away)>=0 and home!=away
        valid=valid and box['homeTeam']['id']==pbp['homeTeam']['id']==result['home'] and box['awayTeam']['id']==pbp['awayTeam']['id']==result['away']
        valid=valid and home==pbp['homeTeam']['score'] and away==pbp['awayTeam']['score']
        valid=valid and datetime.fromisoformat(box['startTimeUTC'].replace('Z','+00:00'))==datetime.fromisoformat(pbp['startTimeUTC'].replace('Z','+00:00'))==result['start'] and row['checked_at']>=result['start']
        valid=valid and result['home']!=result['away']
        valid=valid and period.get('number',0)>=3 and period.get('periodType') in ('REG','OT','SO')
        box_period=box.get('periodDescriptor') or {}
        valid=valid and all(box_period.get(k)==period.get(k) for k in ('number','periodType'))
        if period.get('periodType')=='REG':
            valid=valid and (clock.get('secondsRemaining')==0 or clock.get('timeRemaining')=='00:00')
        else:
            # Sudden death ends before the clock reaches zero. Shootouts also
            # have no running countdown; both provider feeds must be final.
            valid=valid and period.get('number',0)>=4 and clock.get('running') is False and abs(home-away)==1
        result.update(home_score=home,away_score=away,winner=result['home'] if home>away else result['away'],status='candidate_final' if valid else 'conflict',
            validation=dict(boxscore_state=states[0],play_by_play_state=states[1],two_views_agree=bool(valid),result_type=period.get('periodType'),
                raw_source_sha256=row['content_sha256'],validation_policy='two matching provider finals at least ten minutes apart'))
    except (KeyError,TypeError,ValueError):result['status']='conflict'
    result['hash']=digest(jsonable({k:result[k] for k in ('start','home','away','home_score','away_score','winner','status')}))
    return result
