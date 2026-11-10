"""Show import checkpoints; optionally verify stored row counts."""

import argparse

from nhl_analytics.database import connect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify',action='store_true',help='Scan stored counts after import')
    parser.add_argument('--kind',help='Show one dataset kind, e.g. shot')
    args = parser.parse_args()
    with connect() as connection:
        rows = connection.execute('''SELECT file_id,kind,status,rows_processed,paths
            FROM analytics.moneypuck_files ORDER BY kind,observed_at''').fetchall()
        for file_id,kind,status,count,paths in rows:
            if args.kind and args.kind != kind:
                continue
            print(f'{kind:16} {status:10} {count:>10,} rows | {paths}')
            if args.verify:
                if status != 'complete':
                    raise SystemExit('Wait for all selected imports to complete before verifying stored counts.')
                stored = connection.execute('SELECT count(*) FROM analytics.moneypuck_records WHERE file_id=%s',(file_id,)).fetchone()[0]
                if stored != count:
                    raise SystemExit(f'Count mismatch: checkpoint={count}, stored={stored}')
        if args.verify:
            print('All stored row counts match their committed checkpoints.')
        print('Unique player identities:',connection.execute('SELECT count(*) FROM analytics.players').fetchone()[0])


if __name__ == '__main__':
    main()
