"""One shared, bounded NHL GameCenter poller with durable observations."""

import argparse
import asyncio
import json
import logging
import os
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import psycopg
import requests

from nhl_analytics.asof import LEAGUE_TZ
from nhl_analytics.database import connect, migrate
from nhl_analytics.live_store import LiveRepository, digest

WEB = 'https://api-web.nhle.com/v1'
ACTIVE = {'LIVE', 'CRIT'}
FINAL = {'FINAL', 'OFF'}
LOG = logging.getLogger(__name__)


def valid_game_id(value):
    return type(value) is int and 1917 <= value // 1000000 <= datetime.now(timezone.utc).year + 1 and value // 10000 % 100 in (1, 2, 3)


def text(value, fallback=''):
    return value.get('default', fallback) if isinstance(value, dict) else value or fallback


def normalize(box, pbp):
    if not valid_game_id(box.get('id')) or box['id'] != pbp.get('id') or box.get('season') != pbp.get('season'):
        raise ValueError('GameCenter response identity mismatch')
    if box['gameType'] != pbp.get('gameType') or box['gameType'] != box['id'] // 10000 % 100 or box['season'] // 10000 != box['id'] // 1000000 or box['season'] % 10000 != box['season'] // 10000 + 1:
        raise ValueError('GameCenter season/type disagrees with its NHL game ID')
    for side in ('homeTeam', 'awayTeam'):
        if box[side]['id'] != pbp[side]['id']:
            raise ValueError('GameCenter team mismatch')
    if not isinstance(pbp.get('plays'), list):
        raise ValueError('Missing complete play list; refusing to infer event deletions')
    roster = {r['playerId']: ' '.join(text(r.get(k)) for k in ('firstName', 'lastName')).strip() for r in (pbp.get('rosterSpots') or [])}
    statistics=box.get('playerByGameStats') or {}
    if not isinstance(statistics,dict):
        raise ValueError('Invalid player statistics document')
    players = {}
    for side in ('home', 'away'):
        players[side] = []
        for group in ('forwards', 'defense', 'defensemen', 'goalies'):
            for row in (statistics.get(side + 'Team') or {}).get(group, []) or []:
                players[side].append(dict(player_id=row['playerId'], name=roster.get(row['playerId']) or text(row.get('name'), f"Player #{row['playerId']}"),
                    number=row.get('sweaterNumber'), position=row.get('position'), goalie=group == 'goalies',
                    stats={k: row.get(k) for k in ('goals','assists','points','sog','plusMinus','pim','toi','hits','blockedShots',
                        'powerPlayGoals','shifts','saves','shotsAgainst','goalsAgainst','savePctg','saveShotsAgainst')}))
    events = []
    seen = set()
    for play in pbp['plays']:
        event_id = play.get('eventId')
        if type(event_id) is not int or event_id in seen:
            raise ValueError('Missing or duplicate event identity')
        seen.add(event_id)
        details = play.get('details', {})
        player = details.get('scoringPlayerId') or details.get('shootingPlayerId') or details.get('committedByPlayerId') or details.get('hittingPlayerId')
        events.append(dict(event_id=event_id, sort_order=play.get('sortOrder', event_id), type=play.get('typeDescKey', 'unknown'),
            period=play.get('periodDescriptor', {}).get('number'), period_type=play.get('periodDescriptor', {}).get('periodType'),
            time=play.get('timeInPeriod'), team_id=details.get('eventOwnerTeamId'), player_id=player,
            player_name=roster.get(player), details=details))
    teams = {}
    for side in ('home', 'away'):
        source = {**box[side + 'Team'], **pbp[side + 'Team']}
        common=text(source.get('commonName') or source.get('name'), source['abbrev'])
        full=' '.join(filter(None,[text(source.get('placeName')),common]))
        teams[side] = dict(id=source['id'], abbreviation=source['abbrev'], name=full,
            score=source.get('score'), shots=source.get('sog'), logo=source.get('logo'))
    return dict(game_id=box['id'], season_id=box['season'], game_type=box['gameType'], game_date=box['gameDate'],
        start_time_utc=box['startTimeUTC'], game_state=pbp.get('gameState', box['gameState']),
        schedule_state=pbp.get('gameScheduleState'), venue=text(box.get('venue')),
        clock=pbp.get('clock') or box.get('clock'), period=pbp.get('periodDescriptor') or box.get('periodDescriptor'),
        **teams, players=players, stats_available=bool(players['home'] or players['away']),
        events=sorted(events, key=lambda e: e['sort_order']), summary=pbp.get('summary'))


def cadence(state, start, now, active_seconds=20):
    if state in ACTIVE or state == 'PRE':
        return active_seconds
    if state in FINAL:
        return 900 if now - datetime.fromisoformat(start.replace('Z', '+00:00')) < timedelta(hours=8) else 3600
    return 60 if abs((datetime.fromisoformat(start.replace('Z', '+00:00')) - now).total_seconds()) < 900 else 300


class BackoffError(RuntimeError):
    pass


class LiveCollector:
    def __init__(self, active_seconds=20, connection_factory=connect, session=None):
        self.active_seconds = max(15, active_seconds)
        self.connection_factory = connection_factory
        self.connection = None
        self.repository = None
        self.owner = False
        self.session = session or requests.Session()
        self.session.headers.update({'Accept': 'application/json', 'User-Agent': 'nhl-analytics/0.1 live collector'})
        self.states = OrderedDict()
        self.directory = {}
        self.due = {}
        self.negative = {}
        self.score_due = 0
        self.score_checked_at = ''
        self.network_due = 0
        self.last_request = 0
        self.request_count = 0
        self.failures = 0
        self.error = None
        self.requested = set()

    def get(self, path):
        if time.monotonic() < self.network_due:
            raise BackoffError('NHL feed is backing off')
        delay = 0.2 - (time.monotonic() - self.last_request)
        if delay > 0:
            time.sleep(delay)
        self.last_request = time.monotonic()
        self.request_count += 1
        response = self.session.get(WEB + path, timeout=(3, 10))
        if response.status_code == 429:
            header = response.headers.get('Retry-After', '60')
            try:
                seconds = float(header)
            except ValueError:
                try:
                    seconds = (parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds()
                except (ValueError, TypeError):
                    seconds = 60
            self.network_due = time.monotonic() + max(1, seconds)
            raise BackoffError('NHL rate limit; waiting before retry')
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError('Invalid GameCenter document')
        return data

    def ensure_database(self):
        if self.connection is None or self.connection.closed:
            try:
                self.connection = self.connection_factory()
                migrate(self.connection)
            except Exception:
                if self.connection is not None:
                    self.connection.close()
                self.connection = None
                self.repository = None
                self.owner = False
                raise
            self.repository = LiveRepository(self.connection)
            self.owner = self.repository.acquire()
            self.states.update(reversed(list(self.repository.recent().items())))
            while len(self.states)>64:
                self.states.popitem(last=False)
            now=datetime.now(timezone.utc)
            for gid,entry in self.states.items():
                if 'game' in entry:
                    game=entry['game']
                    age=(now-datetime.fromisoformat(entry['checked_at'])).total_seconds()
                    self.due[gid]=time.monotonic()+max(0,cadence(game['game_state'],game['start_time_utc'],now,self.active_seconds)-age)
            # Advisory locks are session-scoped. Never acquire repeatedly on
            # an already-owned session (PostgreSQL locks would stack).

    def tick(self, discover=True, max_games=2):
        now = datetime.now(timezone.utc)
        if (self.connection is None or self.connection.closed) and time.monotonic() < self.network_due:
            return self.result('degraded')
        try:
            self.ensure_database()
            if not self.owner:
                self.owner = self.repository.acquire()
            for gid in self.requested:
                self.repository.request(gid, now)
            self.requested.clear()
            requested = self.repository.requested(now)
            if not self.owner:
                self.states.update(self.repository.recent(requested))
                while len(self.states)>64:
                    self.states.popitem(last=False)
                self.error = None
                return self.result('following')
            self.error = None
            mono = time.monotonic()
            if mono < self.network_due:
                self.error = 'NHL feed backing off; displaying last saved data'
                return self.result('backoff')
            if discover and mono >= self.score_due:
                day = now.astimezone(LEAGUE_TZ).date()
                directory = {}
                for date in (day - timedelta(days=1), day):
                    payload = self.get('/score/' + date.isoformat())
                    if not isinstance(payload.get('games'), list):
                        raise ValueError('Missing score feed games')
                    for game in payload['games']:
                        directory[game['id']] = game
                self.directory = directory
                self.score_checked_at = datetime.now(timezone.utc).isoformat()
                for gid,game in directory.items():
                    cached=self.states.get(gid,{}).get('game')
                    if cached and cached['game_state']!=game['gameState']:
                        self.due[gid]=0
                self.score_due = time.monotonic() + 60
            candidates = set(self.directory) | set(requested)
            def priority(gid):
                state = self.directory.get(gid, {}).get('gameState')
                return (0 if state in ACTIVE else 1 if gid in requested else 2 if state not in FINAL else 3, self.due.get(gid, 0))
            done = 0
            for gid in sorted(candidates, key=priority):
                if done >= max_games:
                    break
                if mono < self.due.get(gid, 0) or mono < self.negative.get(gid, 0):
                    continue
                done += 1
                try:
                    box = self.get(f'/gamecenter/{gid}/boxscore')
                    pbp = self.get(f'/gamecenter/{gid}/play-by-play')
                    view = normalize(box, pbp)
                    if view['game_id'] != gid:
                        raise ValueError('Requested game does not match response')
                    observed = datetime.now(timezone.utc)
                    entry = self.repository.save(dict(view=view, boxscore=box, play_by_play=pbp), observed)
                    self.states[gid] = entry
                    self.states.move_to_end(gid)
                    self.due[gid] = time.monotonic() + cadence(view['game_state'], view['start_time_utc'], observed, self.active_seconds)
                    self.failures = 0
                except requests.HTTPError as error:
                    if error.response is not None and error.response.status_code == 404:
                        self.negative[gid] = time.monotonic() + 1800
                        self.states[gid] = dict(game_id=gid, not_found=True, checked_at=now.isoformat())
                    else:
                        raise
                while len(self.states) > 64:
                    self.states.popitem(last=False)
            return self.result('running')
        except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError, OSError, BackoffError, psycopg.Error) as error:
            self.failures += 1
            self.network_due = max(self.network_due, time.monotonic() + min(300, 5 * 2 ** min(self.failures, 6)))
            self.error = ('Database unavailable' if isinstance(error, psycopg.Error) or self.repository is None else 'NHL feed unavailable') + '; retrying with last saved data'
            LOG.warning('Live collector retry (%s)', type(error).__name__)
            if self.connection is not None and isinstance(error, psycopg.Error):
                self.connection.close()
                self.connection = None
                self.repository = None
                self.owner = False
            return self.result('degraded')

    def result(self, status):
        return dict(states=dict(self.states), directory=list(self.directory.values()), health=dict(status=status,
            error=self.error, active_poll_seconds=self.active_seconds, checked_at=datetime.now(timezone.utc).isoformat(), owner=self.owner,
            upstream_requests=self.request_count))

    def close(self):
        self.session.close()
        if self.connection is not None:
            self.connection.close()


class LiveHub:
    def __init__(self, collector=None):
        self.collector = collector or LiveCollector(int(os.environ.get('NHL_LIVE_POLL_SECONDS', '20')))
        self.states = {}
        self.directory = []
        self.health = dict(status='starting', error=None, active_poll_seconds=self.collector.active_seconds)
        self.pending = set()
        self.condition = asyncio.Condition()
        self.version = 0
        self.clients = 0

    def request(self, game_id):
        if not valid_game_id(game_id):
            raise ValueError('Use a valid NHL game ID')
        if game_id not in self.states:
            if len(self.pending) >= 16:
                raise OverflowError('Live game queue is full; retry shortly')
            self.pending.add(game_id)

    def detail(self, game_id):
        entry = self.states.get(game_id)
        if entry is None:
            return dict(status='loading', game_id=game_id, game=None, collector=self.health, stale=True)
        if entry.get('not_found'):
            return dict(status='not_found', game_id=game_id, game=None, collector=self.health, stale=True)
        game = entry['game']
        age = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(entry['checked_at'])).total_seconds())
        stale = bool(self.health.get('error')) or (game['game_state'] not in FINAL and age > (90 if game['game_state'] in ACTIVE else 900))
        return dict(entry, status='ready', collector=self.health, stale=stale, age_seconds=round(age))

    def listing(self):
        items = []
        source = self.directory or [e['game'] for e in self.states.values() if 'game' in e]
        for item in source:
            gid=item.get('game_id',item.get('id'))
            entry=self.states.get(gid,{})
            if entry.get('game') and entry.get('checked_at','')>self.collector.score_checked_at:
                item=entry['game']
            if 'game_id' in item:
                items.append(dict(game_id=item['game_id'], season_id=item['season_id'], start_time_utc=item['start_time_utc'],
                    game_state=item['game_state'], schedule_state=item.get('schedule_state'), game_date=item['game_date'], home=item['home'], away=item['away']))
            else:
                teams = {side: dict(id=item[side+'Team']['id'], abbreviation=item[side+'Team']['abbrev'], name=text(item[side+'Team'].get('name'), item[side+'Team']['abbrev']),
                    score=item[side+'Team'].get('score'), shots=item[side+'Team'].get('sog')) for side in ('home', 'away')}
                items.append(dict(game_id=item['id'], season_id=item['season'], start_time_utc=item['startTimeUTC'], game_date=item['gameDate'], game_state=item['gameState'], schedule_state=item.get('gameScheduleState'), **teams))
        return dict(items=sorted(items, key=lambda g: (g['game_state'] not in ACTIVE, g['start_time_utc'])), collector=self.health)

    async def run(self):
        try:
            while True:
                self.collector.requested.update(self.pending)
                self.pending.clear()
                poll=asyncio.create_task(asyncio.to_thread(self.collector.tick))
                try:
                    result=await asyncio.shield(poll)
                except asyncio.CancelledError:
                    # Let the bounded HTTP/DB operation finish before closing
                    # its connection and releasing the single-collector lock.
                    await poll
                    raise
                self.states, self.directory, self.health = result['states'], result['directory'], result['health']
                async with self.condition:
                    self.version += 1
                    self.condition.notify_all()
                await asyncio.sleep(2 if self.health['status'] != 'following' else 5)
        finally:
            await asyncio.to_thread(self.collector.close)

    async def events(self, request, game_id=None):
        self.clients += 1
        previous = None
        last_sent = time.monotonic()
        try:
            yield 'retry: 5000\n\n'
            while not await request.is_disconnected():
                value = self.listing() if game_id is None else self.detail(game_id)
                # Health timestamps change each loop; send state only when the
                # game/health content changes, with independent keepalives.
                comparable = dict(value)
                comparable['collector'] = {k:v for k,v in self.health.items() if k not in ('checked_at','upstream_requests')}
                comparable.pop('age_seconds', None)
                revision = digest(comparable)
                if revision != previous:
                    yield f'id: {revision}\nevent: game\ndata: {json.dumps(value, separators=(",", ":"))}\n\n'
                    previous = revision
                    last_sent = time.monotonic()
                elif time.monotonic() - last_sent >= 10:
                    yield ': keepalive\n\n'
                    last_sent = time.monotonic()
                try:
                    async with self.condition:
                        await asyncio.wait_for(self.condition.wait(), timeout=max(0.1, 10 - (time.monotonic() - last_sent)))
                except TimeoutError:
                    yield ': keepalive\n\n'
                    last_sent = time.monotonic()
        finally:
            self.clients -= 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--game', type=int)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
    collector = LiveCollector()
    if args.game:
        if not valid_game_id(args.game):
            parser.error('Invalid NHL game ID')
        collector.requested.add(args.game)
    try:
        while True:
            result = collector.tick(discover=not bool(args.game), max_games=1 if args.game else 2)
            print(json.dumps(dict(health=result['health'], games=len(result['states']))), flush=True)
            if args.once:
                if result['health']['status'] == 'degraded':
                    raise SystemExit(1)
                break
            time.sleep(2)
    except KeyboardInterrupt:
        pass
    finally:
        collector.close()


if __name__ == '__main__':
    main()
