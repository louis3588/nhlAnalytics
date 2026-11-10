"""Fill missing player name components from full_name, retaining inference provenance."""

import argparse
from datetime import datetime,timezone
from pathlib import Path

from nhl_analytics.database import connect,migrate


def infer_names(first,last,full):
    missing_first = not first or not first.strip()
    missing_last = not last or not last.strip()
    full = ' '.join((full or '').split())
    if not full or not (missing_first or missing_last):
        return None,None
    if not missing_first and missing_last and full.startswith(first.strip()+' '):
        return None,full[len(first.strip())+1:]
    if missing_first and not missing_last and full.endswith(' '+last.strip()):
        return full[:-len(last.strip())-1],None
    parts = full.split(' ',1)
    return (parts[0] if missing_first else None,
            parts[1] if missing_last and len(parts)>1 else None)


def backfill(connection,dry_run=False):
    with connection.transaction():
        connection.execute('SELECT pg_advisory_xact_lock(768432101)')
        rows = connection.execute('''SELECT player_id,first_name,last_name,full_name FROM analytics.players
            WHERE NULLIF(btrim(first_name),'') IS NULL OR NULLIF(btrim(last_name),'') IS NULL
            ORDER BY player_id FOR UPDATE''').fetchall()
        updates = []
        unresolved = 0
        for player,first,last,full in rows:
            inferred_first,inferred_last = infer_names(first,last,full)
            if inferred_first is not None or inferred_last is not None:
                updates.append((player,first,last,full,inferred_first,inferred_last))
            projected_first=inferred_first if inferred_first is not None else first
            projected_last=inferred_last if inferred_last is not None else last
            if not projected_first or not projected_first.strip() or not projected_last or not projected_last.strip():
                unresolved += 1
        if not dry_run:
            observed = datetime.now(timezone.utc)
            with connection.cursor() as cursor:
                cursor.executemany('''INSERT INTO analytics.player_name_inferences
                    (player_id,inferred_at,original_first_name,original_last_name,original_full_name,inferred_first_name,inferred_last_name)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)''',
                    [(p,observed,f,l,full,nf,nl) for p,f,l,full,nf,nl in updates])
                cursor.executemany('''UPDATE analytics.players SET
                    first_name=COALESCE(%s,first_name),last_name=COALESCE(%s,last_name),
                    first_name_inferred=first_name_inferred OR %s,last_name_inferred=last_name_inferred OR %s
                    WHERE player_id=%s''',
                    [(nf,nl,nf is not None,nl is not None,p) for p,f,l,full,nf,nl in updates])
        return dict(updated=len(updates),unresolved=unresolved,dry_run=dry_run)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--env-file',type=Path,default=Path('.env'))
    args = parser.parse_args()
    with connect(args.env_file) as connection:
        if not args.dry_run:
            migrate(connection)
        print(backfill(connection,args.dry_run))


if __name__=='__main__':
    main()
