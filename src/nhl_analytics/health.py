"""Warehouse health inventory, source coverage, missingness and consistency checks."""

import argparse
import json
import re
from datetime import datetime,timezone
from pathlib import Path

from psycopg.rows import dict_row

from nhl_analytics.asof import past_final,read_games
from nhl_analytics.database import connect,timestamp
from nhl_analytics.ingest import season_argument


def query(connection,statement,params=()):
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(statement,params)
        return [dict(row) for row in cursor]


def build_report(connection,seasons,cutoff=None,deep=False,stale_hours=24):
    # Historical shot scans should not compete with ingestion for the small
    # local Docker VM's memory. Settings apply only to this transaction.
    with connection.transaction():
        connection.execute("SET LOCAL jit=off")
        connection.execute("SET LOCAL max_parallel_workers_per_gather=0")
        connection.execute("SET LOCAL work_mem='4MB'")
        return _build_report(connection,seasons,cutoff,deep,stale_hours)


def _build_report(connection,seasons,cutoff=None,deep=False,stale_hours=24):
    now=datetime.now(timezone.utc)
    cutoff=cutoff or now
    report=dict(schema_version=1,generated_at=now,prediction_at=cutoff,seasons=seasons,
                scope='Current warehouse health; prediction eligibility is evaluated separately at prediction_at',checks=[])
    checks=report['checks']
    def check(name,severity,count,detail):
        checks.append(dict(name=name,severity=severity,count=int(count),status='pass' if not count else severity,detail=detail))
    inventory={}
    for table in ('players','teams','seasons','games','skater_game_stats','goalie_game_stats','roster_observations',
                  'skater_season_stats','goalie_season_stats','team_standings','team_season_edge','game_log_sources',
                  'player_game_observations','game_schedule_observations'):
        inventory[table]=connection.execute('SELECT count(*) FROM analytics.'+table).fetchone()[0]
    report['inventory']=inventory
    report['coverage']={}
    for table in ('games','skater_game_stats','goalie_game_stats'):
        fields='count(*) AS rows,count(DISTINCT game_id) AS games'
        if table!='games':
            fields+=',count(DISTINCT player_id) AS players'
        report['coverage'][table]=query(connection,'SELECT season_id,game_type_id,'+fields+
            ' FROM analytics.'+table+' GROUP BY season_id,game_type_id ORDER BY season_id,game_type_id')
    for table in ('skater_season_stats','goalie_season_stats'):
        report['coverage'][table]=query(connection,'SELECT season_id,game_type_id,count(*) AS rows,'
            'count(DISTINCT player_id) AS players,count(DISTINCT run_id) AS versions FROM analytics.'+table+
            ' GROUP BY season_id,game_type_id ORDER BY season_id,game_type_id')
    report['coverage']['standings']=query(connection,'''SELECT season_id,count(*) AS rows,
        count(DISTINCT standings_date) AS dates,min(standings_date) AS first_date,max(standings_date) AS last_date
        FROM analytics.team_standings GROUP BY season_id ORDER BY season_id''')
    check('daily_standings_history_limited','warning',sum(r['dates']<2 for r in report['coverage']['standings']),
          'Final/latest standings snapshots exist; most historical daily prediction dates are not captured.')
    report['missingness']={}
    report['missingness']['players']=query(connection,'''SELECT count(*) AS rows,
        count(*) FILTER(WHERE first_name IS NULL OR last_name IS NULL) AS missing_name,
        count(*) FILTER(WHERE birth_date IS NULL) AS missing_birth_date,
        count(*) FILTER(WHERE first_name_inferred OR last_name_inferred) AS inferred_name
        FROM analytics.players''')[0]
    check('inferred_player_names','warning',report['missingness']['players']['inferred_name'],
          'Name heuristics are flagged; features exclude names and current biographies.')
    for table,fields in (('skater_game_stats',('shots','toi_seconds','power_play_points')),
                         ('goalie_game_stats',('shots_against','goals_against','toi_seconds','games_started')),
                         ('team_season_edge',('offensive_zone_pct','total_distance_km','skating_speed_max_kmh'))):
        expressions=','.join(f'count(*) FILTER(WHERE {f} IS NULL) AS missing_{f}' for f in fields)
        report['missingness'][table]=query(connection,'SELECT season_id,count(*) AS rows,'+expressions+
            ' FROM analytics.'+table+' GROUP BY season_id ORDER BY season_id')
    report['shot_metric_quality']=query(connection,'''SELECT r.season_id,count(*) AS rows,
        count(*) FILTER(WHERE r.xgoals_for IS NULL) AS missing_xg,
        count(*) FILTER(WHERE r.xgoals_for<0 OR r.xgoals_for>1) AS invalid_xg
        FROM analytics.moneypuck_records r JOIN analytics.moneypuck_files f USING(file_id)
        WHERE r.kind='shot' AND f.status='complete' AND r.season_id=ANY(%s)
        GROUP BY r.season_id ORDER BY r.season_id''',(seasons,))
    check('shot_probability_range','error',sum(r['invalid_xg'] for r in report['shot_metric_quality']),
          'Requested-season source shot probabilities must be between zero and one.')
    for table in ('skater_game_stats','goalie_game_stats'):
        n=connection.execute('SELECT count(*)-count(DISTINCT (player_id,game_id)) FROM analytics.'+table).fetchone()[0]
        check(table+'_duplicate_keys','error',n,'Canonical player/game keys must be unique.')
    n=connection.execute('''SELECT count(*) FROM analytics.skater_game_stats
        WHERE points IS NOT NULL AND goals IS NOT NULL AND assists IS NOT NULL AND points<>goals+assists''').fetchone()[0]
    check('skater_scoring_arithmetic','error',n,'Points should equal goals plus assists.')
    n=connection.execute('''SELECT count(*) FROM analytics.goalie_game_stats
        WHERE shots_against<goals_against OR saves<>shots_against-goals_against''').fetchone()[0]
    check('goalie_save_arithmetic','error',n,'Saves should equal shots against minus goals against.')
    n=connection.execute('''SELECT count(*) FROM analytics.games WHERE is_final AND
        (home_score IS NULL OR away_score IS NULL OR home_score=away_score OR winner_team_id IS NULL
        OR winner_team_id<>CASE WHEN home_score>away_score THEN home_team_id ELSE away_team_id END)''').fetchone()[0]
    check('completed_game_results','error',n,'Final games require scores and a consistent winner.')
    mismatch=query(connection,'''WITH results AS (
        SELECT home_team_id AS team_id,season_id,game_date,winner_team_id,home_score AS gf,away_score AS ga,result_type
        FROM analytics.games WHERE game_type_id=2 AND is_final
        UNION ALL SELECT away_team_id,season_id,game_date,winner_team_id,away_score,home_score,result_type
        FROM analytics.games WHERE game_type_id=2 AND is_final
    ) SELECT s.team_id,s.season_id,s.standings_date,s.games_played,count(r.team_id) AS result_games,
        s.points,coalesce(sum(CASE WHEN r.winner_team_id=s.team_id THEN 2 WHEN r.result_type IN ('OT','SO') THEN 1 ELSE 0 END),0) AS result_points
        FROM analytics.latest_team_standings s LEFT JOIN results r ON r.team_id=s.team_id
        AND r.season_id=s.season_id AND r.game_date<=s.standings_date
        GROUP BY s.team_id,s.season_id,s.standings_date,s.games_played,s.points
        HAVING count(r.team_id)<>s.games_played OR coalesce(sum(CASE WHEN r.winner_team_id=s.team_id THEN 2 WHEN r.result_type IN ('OT','SO') THEN 1 ELSE 0 END),0)<>s.points''')
    report['standings_result_mismatches']=mismatch
    check('standings_vs_game_results','warning',len(mismatch),'Different refresh times can explain disagreements; compare source timestamps before correcting.')
    files=query(connection,'''SELECT file_id,kind,status,rows_processed,paths,sha256,observed_at,completed_at,error
        FROM analytics.moneypuck_files ORDER BY kind,observed_at''')
    for file in files:
        file['file_id']=str(file['file_id'])
    report['moneypuck_files']=files
    report['inventory']['moneypuck_rows_from_checkpoints']=sum(f['rows_processed'] for f in files if f['status']=='complete')
    check('incomplete_moneypuck_files','warning',sum(f['status']!='complete' for f in files),
          'Incomplete/failed imports are excluded from feature shot aggregation.')
    report['coverage']['moneypuck_selected_seasons']=query(connection,'''SELECT r.kind,r.season_id,count(*) AS source_rows,
        count(DISTINCT r.file_id) AS versions FROM analytics.moneypuck_records r JOIN analytics.moneypuck_files f USING(file_id)
        WHERE f.status='complete' AND r.season_id=ANY(%s) GROUP BY r.kind,r.season_id ORDER BY r.kind,r.season_id''',(seasons,))
    available_shot_seasons={r['season_id'] for r in report['coverage']['moneypuck_selected_seasons'] if r['kind']=='shot'}
    missing_shots=[season for season in seasons if season not in available_shot_seasons]
    check('missing_requested_shot_seasons','warning',len(missing_shots),f'No complete shot archive rows for seasons {missing_shots}; xG remains null for uncovered games.')
    report['unmapped_moneypuck_team_codes']=query(connection,'''SELECT DISTINCT team_code
        FROM analytics.moneypuck_records WHERE kind<>'shot' AND team_code IS NOT NULL
        AND NOT EXISTS(SELECT 1 FROM analytics.teams t WHERE t.abbreviation=team_code) ORDER BY team_code''')
    check('provider_team_aliases','warning',len(report['unmapped_moneypuck_team_codes']),
          'Historical MoneyPuck codes need explicit mappings; core team features use observed NHL team IDs instead.')
    paths={}
    for file in files:
        for path in file['paths']:
            paths.setdefault(path,set()).add(file['sha256'])
    report['reused_download_paths']=[p for p,hashes in paths.items() if len(hashes)>1]
    check('download_path_versions','info',len(report['reused_download_paths']),
          'A path can refer to several imported versions; preserve content-hash archives for future raw-file reproducibility.')
    report['deep_scan']=deep
    if deep:
        duplicate_rows=connection.execute('''SELECT count(*)-count(DISTINCT (file_id,record_key))
            FROM analytics.moneypuck_records WHERE kind='shot' ''').fetchone()[0]
        report['raw_shot_duplicate_rows']=duplicate_rows
        check('raw_shot_duplicates','info',duplicate_rows,'Source duplicates are retained; as-of feature queries deduplicate by game/event key.')
    else:
        report['raw_shot_duplicate_rows']=None
        check('full_archive_duplicate_scan','info',1,'Not run by default; --deep scans all historical shot keys without loading raw payloads into Python.')
    latest_season=max(seasons)
    freshness=query(connection,'''WITH checks AS (
        SELECT 'schedules' AS source,schedule_checked_at AS checked FROM analytics.games
            WHERE season_id=%s AND game_type_id=2
        UNION ALL SELECT 'player_game_logs',last_checked_at FROM analytics.game_log_state WHERE season_id=%s AND game_type_id=2
        UNION ALL SELECT 'standings',observed_at FROM analytics.latest_team_standings WHERE season_id=%s
        UNION ALL SELECT 'team_edge',observed_at FROM analytics.team_season_edge WHERE season_id=%s AND game_type_id=2
    ) SELECT source,count(*) AS entities,min(checked) AS oldest,max(checked) AS latest,
        count(*) FILTER(WHERE checked IS NULL OR checked<%s-interval '1 hour'*%s) AS stale_entities
        FROM checks GROUP BY source ORDER BY source''',(latest_season,latest_season,latest_season,latest_season,now,stale_hours))
    for row in freshness:
        row['age_hours']=(now-row['latest']).total_seconds()/3600 if row['latest'] else None
    report['freshness']=freshness
    check('stale_sources','warning',sum(r['stale_entities'] for r in freshness),
          f'Entity checks in latest requested season {latest_season} older than {stale_hours} hours; one fresh entity does not hide stale peers.')
    report['availability']=query(connection,'''SELECT 'schedule_history' AS source,min(observed_at) AS first_observed_at,max(observed_at) AS last_observed_at
        FROM analytics.game_schedule_observations UNION ALL SELECT 'player_game_history',min(observed_at),max(observed_at)
        FROM analytics.game_log_sources UNION ALL SELECT 'standings_history',min(observed_at),max(observed_at)
        FROM analytics.team_standings_observations UNION ALL SELECT 'edge_history',min(observed_at),max(observed_at)
        FROM analytics.team_edge_observations''')
    known=read_games(connection,cutoff,seasons,2)
    report['prediction_eligibility']=dict(prediction_at=cutoff,known_regular_season_games=len(known),
        known_final_games=sum(past_final(g,cutoff) for g in known.values()))
    check('no_asof_final_games','warning',report['prediction_eligibility']['known_final_games']==0,
          'Strict retrieval-time eligibility can be empty before our first imports; historical final downloads are never backdated.')
    report['status']='error' if any(c['status']=='error' for c in checks) else ('warning' if any(c['status']=='warning' for c in checks) else 'pass')
    return report


def render_markdown(report):
    lines=['# Dataset health report','',f"Generated: {report['generated_at'].isoformat()}",
           f"Prediction cutoff: {report['prediction_at'].isoformat()}",f"Status: **{report['status']}**",'',
           '| Check | Status | Count | Detail |','| --- | --- | ---: | --- |']
    for row in report['checks']:
        lines.append(f"| {row['name']} | {row['status']} | {row['count']} | {row['detail'].replace('|','/')} |")
    lines+=['','## Inventory','','| Entity | Rows |','| --- | ---: |']
    lines += [f'| {name} | {count:,} |' for name,count in report['inventory'].items()]
    lines+=['','## NHL coverage','','| Dataset | Season | Game type | Rows | Players |','| --- | --- | --- | ---: | ---: |']
    for dataset,rows in report['coverage'].items():
        if dataset in ('standings','moneypuck_selected_seasons'):
            continue
        lines += [f"| {dataset} | {r['season_id']} | {r['game_type_id']} | {r['rows']:,} | {r.get('players','—')} |" for r in rows]
    lines+=['','## Missingness','','| Dataset | Season | Metric | Rows | Missing/inferred |','| --- | --- | --- | ---: | ---: |']
    for dataset,entries in report['missingness'].items():
        for row in ([entries] if isinstance(entries,dict) else entries):
            for key,value in row.items():
                if key.startswith('missing_') or key=='inferred_name':
                    lines.append(f"| {dataset} | {row.get('season_id','all')} | {key} | {row['rows']:,} | {value:,} |")
    lines+=['','## Freshness','','| Source | Latest check (UTC) | Age in hours |','| --- | --- | ---: |']
    lines += [f"| {r['source']} | {r['latest']} | {round(r['age_hours'],2) if r['age_hours'] is not None else 'unknown'} |" for r in report['freshness']]
    if 'feature_export' in report:
        lines+=['','## Point-in-time feature coverage','','| Kind | Season | Player-games | xG-covered games |','| --- | --- | ---: | ---: |']
        for row in report['feature_export']['diagnostics']['coverage_by_kind_season']:
            lines.append(f"| {row['kind']} | {row['season_id']} | {row['player_games']:,} | {row['xg_covered_games']:,} |")
    lines+=['','[Full machine-readable report](health.json) contains source files, checkpoints and mismatch details.',
            'MoneyPuck coverage counts are source rows across situations/game types, not independent training examples.','']
    return '\n'.join(lines)


def write_report(report,output):
    from nhl_analytics.features import write_document
    write_document(output/'health.json',report)
    (output/'health.md').write_text(render_markdown(report),encoding='utf-8')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seasons',type=season_argument,nargs='+',default=[20252026,20262027])
    parser.add_argument('--prediction-at',type=timestamp,default=None)
    parser.add_argument('--deep',action='store_true')
    parser.add_argument('--stale-hours',type=float,default=24)
    parser.add_argument('--output',type=Path,default=Path('data/health'))
    parser.add_argument('--env-file',type=Path,default=Path('.env'))
    args=parser.parse_args()
    if args.stale_hours<=0:
        parser.error('--stale-hours must be positive')
    output=args.output/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    with connect(args.env_file) as connection,connection.transaction():
        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        report=build_report(connection,list(dict.fromkeys(args.seasons)),args.prediction_at,args.deep,args.stale_hours)
    write_report(report,output)
    print(json.dumps(dict(status=report['status'],output=str(output),checks=len(report['checks'])),indent=2))
    if report['status']=='error':
        raise SystemExit(1)


if __name__=='__main__':
    main()
