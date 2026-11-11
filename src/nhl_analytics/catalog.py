"""Small saved display-identity catalog; never used as prediction features."""

import argparse
import json
import os
from pathlib import Path

from nhl_analytics.database import connect
from nhl_analytics.features import write_document


def export_catalog(connection, output, seed=Path('dashboard/public/catalog.json')):
    catalog = json.loads(seed.read_text(encoding='utf-8')) if seed.exists() else dict(players={}, teams={}, seasons={})
    for player, first, last, full in connection.execute('SELECT player_id,first_name,last_name,full_name FROM analytics.players'):
        name = full or ' '.join(p for p in (first, last) if p)
        if name:
            key = str(player)
            catalog['players'][key] = dict(catalog['players'].get(key, {}), name=name)
    for player, headshot in connection.execute('''SELECT DISTINCT ON (player_id) player_id,headshot
        FROM analytics.roster_observations WHERE headshot IS NOT NULL ORDER BY player_id,observed_at DESC'''):
        if str(player) in catalog['players']:
            catalog['players'][str(player)]['headshot'] = headshot
    for team, abbreviation, name in connection.execute('SELECT team_id,abbreviation,name FROM analytics.teams'):
        key = str(team)
        catalog['teams'][key] = dict(catalog['teams'].get(key, {}), id=team, abbreviation=abbreviation, name=name)
    seasons = {}
    for season, home, away in connection.execute('SELECT DISTINCT season_id,home_team_id,away_team_id FROM analytics.games WHERE home_team_id IS NOT NULL AND away_team_id IS NOT NULL'):
        seasons.setdefault(str(season), set()).update((home, away))
    catalog['seasons'].update({s: sorted(ids) for s, ids in seasons.items()})
    catalog['sources'] = ['Saved NHL identity tables; display only, excluded from model features']
    write_document(output, catalog)
    return dict(players=len(catalog['players']), teams=len(catalog['teams']), output=str(output))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--env-file', type=Path, default=Path('.env'))
    args = parser.parse_args()
    with connect(args.env_file) as connection:
        output = args.output or Path(os.environ.get('NHL_DAILY_ROOT','data/predictions/daily')).parent/'catalog.json'
        result = export_catalog(connection, output)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
