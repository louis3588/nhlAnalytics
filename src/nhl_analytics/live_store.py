"""Durable live snapshots, idempotent events and explicit correction history."""

import hashlib
import json
from datetime import timedelta

from psycopg.types.json import Jsonb


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


class LiveRepository:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return self.connection.execute('SELECT pg_try_advisory_lock(768432109)').fetchone()[0]

    def request(self, game_id, now):
        self.connection.execute('''INSERT INTO analytics.live_game_requests VALUES (%s,%s,%s)
            ON CONFLICT(game_id) DO UPDATE SET requested_at=EXCLUDED.requested_at,expires_at=EXCLUDED.expires_at''',
            (game_id, now, now + timedelta(hours=6)))

    def requested(self, now):
        self.connection.execute('DELETE FROM analytics.live_game_requests WHERE expires_at<%s', (now,))
        return [r[0] for r in self.connection.execute('SELECT game_id FROM analytics.live_game_requests ORDER BY requested_at DESC LIMIT 16')]

    def recent(self, requested=()):
        rows = self.connection.execute('''SELECT game_id,payload,observed_at,checked_at,content_sha256
            FROM analytics.live_games WHERE game_date>=CURRENT_DATE-2 OR game_id=ANY(%s)
            ORDER BY checked_at DESC LIMIT 64''', (list(requested),)).fetchall()
        return {r[0]: dict(game=r[1]['view'], observed_at=r[2].isoformat(), checked_at=r[3].isoformat(), revision=r[4]) for r in rows}

    def save(self, envelope, now):
        view = envelope['view']
        gid = view['game_id']
        content = digest(envelope)
        with self.connection.transaction():
            previous = self.connection.execute('SELECT observed_at,checked_at,content_sha256 FROM analytics.live_games WHERE game_id=%s FOR UPDATE', (gid,)).fetchone()
            if previous and now < previous[1]:
                raise ValueError('Cannot backdate a live observation')
            self.connection.execute('INSERT INTO analytics.seasons VALUES (%s) ON CONFLICT DO NOTHING', (view['season_id'],))
            for team in (view['home'], view['away']):
                self.connection.execute('''INSERT INTO analytics.teams(team_id,abbreviation,name,updated_at) VALUES (%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING''', (team['id'], team['abbreviation'], team['name'], now))
            self.connection.execute('''INSERT INTO analytics.games(game_id,season_id,game_type_id,game_date,
                home_team_abbrev,away_team_abbrev,home_team_id,away_team_id,start_time_utc,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                (gid, view['season_id'], view['game_type'], view['game_date'], view['home']['abbreviation'], view['away']['abbreviation'],
                 view['home']['id'], view['away']['id'], view['start_time_utc'], now))
            self.connection.execute('''INSERT INTO analytics.live_games VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(game_id) DO UPDATE SET game_date=EXCLUDED.game_date,game_state=EXCLUDED.game_state,
                observed_at=EXCLUDED.observed_at,checked_at=EXCLUDED.checked_at,content_sha256=EXCLUDED.content_sha256,payload=EXCLUDED.payload''',
                (gid, view['game_date'], view['game_state'], previous[0] if previous and previous[2] == content else now, now, content, Jsonb(envelope)))
            if not previous or previous[2] != content:
                self.connection.execute('INSERT INTO analytics.live_game_snapshots VALUES (%s,%s,%s,%s)', (gid, now, content, Jsonb(envelope)))
            players = {r['player_id']: r['name'] for side in ('home', 'away') for r in view['players'][side]}
            for r in envelope['play_by_play'].get('rosterSpots') or []:
                players.setdefault(r['playerId'], ' '.join((r.get(k) or {}).get('default', '') for k in ('firstName', 'lastName')).strip() or None)
            existing = {r[0]: r[1:] for r in self.connection.execute('SELECT event_id,content_sha256,is_deleted,payload FROM analytics.live_game_events WHERE game_id=%s', (gid,))}
            raw_events={e['eventId']:e for e in envelope['play_by_play']['plays']}
            events = {e['event_id']: dict(e,source_event=raw_events[e['event_id']]) for e in view['events']}
            if existing and not events and view['game_state'] in ('LIVE','CRIT','OFF','FINAL'):
                raise ValueError('Empty play feed after play began; retaining last good observation')
            for event in events.values():
                if event['player_id']:
                    players.setdefault(event['player_id'], None)
            with self.connection.cursor() as cursor:
                cursor.executemany('''INSERT INTO analytics.players(player_id,full_name,updated_at) VALUES (%s,%s,%s)
                    ON CONFLICT DO NOTHING''', [(p, name, now) for p, name in players.items()])
            for event_id in sorted(set(existing) | set(events)):
                removed = event_id not in events
                old = existing.get(event_id)
                event = events.get(event_id) or old[2]
                sha = digest(dict(deleted=removed, event=event))
                if old and old[0] == sha:
                    continue
                self.connection.execute('''INSERT INTO analytics.live_game_events VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(game_id,event_id) DO UPDATE SET player_id=EXCLUDED.player_id,last_seen_at=EXCLUDED.last_seen_at,
                    content_sha256=EXCLUDED.content_sha256,is_deleted=EXCLUDED.is_deleted,payload=EXCLUDED.payload''',
                    (gid, event_id, event['player_id'], now, now, sha, removed, Jsonb(event)))
                self.connection.execute('INSERT INTO analytics.live_game_event_revisions VALUES (%s,%s,%s,%s,%s,%s)',
                    (gid, event_id, now, sha, removed, Jsonb(event)))
            if events:
                self.connection.execute('UPDATE analytics.live_game_events SET last_seen_at=%s WHERE game_id=%s AND event_id=ANY(%s)',
                    (now,gid,list(events)))
        return dict(game=view, observed_at=(previous[0] if previous and previous[2] == content else now).isoformat(), checked_at=now.isoformat(), revision=content)
