"""Resumable daily NHL refresh. Each UTC day publishes at most one forecast."""

import argparse
import json
import logging
import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from nhl_analytics.database import connect
from nhl_analytics.features import write_document
from nhl_analytics.ingest import season_argument

LOG = logging.getLogger(__name__)


def current_season(now):
    year = now.year if now.month >= 9 else now.year - 1
    return year * 10000 + year + 1


@contextmanager
def job_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError('Another daily refresh is running; no duplicate work started') from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def commands(run, season, env_file, workers, edge=True):
    common = ['--env-file', str(env_file)]
    logs = sorted((run / 'game_logs').glob('*/plan.json'))
    log_args = ['--resume', str(logs[-1].parent)] if logs else ['--seasons', str(season), '--output', str(run / 'game_logs')]
    return [
        ('rosters_stats', ['nhl_analytics.ingest', '--season', str(season), '--database', '--output', str(run / 'season'), *common]),
        ('schedules_results', ['nhl_analytics.schedules', '--seasons', str(season), '--output', str(run / 'schedules'), *common]),
        ('skater_goalie_logs', ['nhl_analytics.game_logs', *log_args, '--workers', str(workers), *common]),
        ('standings_edge', ['nhl_analytics.team_context', '--seasons', str(season), '--output', str(run / 'context'), *(['--edge'] if edge else []), *common]),
        ('identity_catalog', ['nhl_analytics.catalog', *common]),
    ]


def run_refresh(root, season, env_file, base_run=None, workers=2, edge=True, ingest_only=False, runner=None, publisher=None):
    now = datetime.now(timezone.utc)
    run = root / now.date().isoformat()
    run.mkdir(parents=True, exist_ok=True)
    manifest_path = run / 'manifest.json'
    with job_lock(root / 'refresh.lock'):
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else dict(
            status='running', day=now.date().isoformat(), season=season, steps={}, started_at=now.isoformat())
        if manifest['season'] != season:
            raise ValueError('This UTC day already belongs to a different season')
        if manifest['status'] == 'complete':
            LOG.info('Already complete for %s; nothing repeated', manifest['day'])
            return manifest
        # Fail before any network downloads when Docker/Timescale is unavailable.
        with connect(env_file) as connection:
            connection.execute('SELECT 1')
        manifest['status'] = 'running'
        write_document(manifest_path, manifest)
        try:
            for name, arguments in commands(run, season, env_file, workers, edge):
                if manifest['steps'].get(name, {}).get('status') == 'complete':
                    LOG.info('Already completed: %s', name)
                    continue
                LOG.info('Starting %s (log: %s)', name, run / f'{name}.log')
                with (run / f'{name}.log').open('a', encoding='utf-8') as log:
                    result = (runner or subprocess.run)([sys.executable, '-m', *arguments], stdout=log, stderr=subprocess.STDOUT,
                        check=False, env=dict(os.environ, PYTHONUNBUFFERED='1'))
                if result.returncode:
                    raise RuntimeError(f'{name} failed; see {run / (name + ".log")}. Successful steps will not repeat.')
                manifest['steps'][name] = dict(status='complete', finished_at=datetime.now(timezone.utc).isoformat())
                write_document(manifest_path, manifest)
            if ingest_only:
                manifest['status'] = 'ingested'
            else:
                if publisher is None:
                    from nhl_analytics.daily_predictions import publish_daily
                    publisher = publish_daily
                manifest['publication'] = publisher(season, env_file, base_run)
                manifest['status'] = 'complete'
            manifest['finished_at'] = datetime.now(timezone.utc).isoformat()
            manifest.pop('error', None)
            write_document(manifest_path, manifest)
            return manifest
        except Exception as error:
            manifest.update(status='failed', error=type(error).__name__)
            write_document(manifest_path, manifest)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--season', type=season_argument)
    parser.add_argument('--output', type=Path, default=Path('data/refresh'))
    parser.add_argument('--env-file', type=Path, default=Path('.env'))
    parser.add_argument('--base-run', type=Path, help='Frozen evaluation/model run; never retuned by refresh')
    parser.add_argument('--workers', type=int, choices=[1, 2], default=2)
    parser.add_argument('--no-edge', action='store_true')
    parser.add_argument('--ingest-only', action='store_true', help='Refresh sources now; publish on the next normal invocation')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
    try:
        result = run_refresh(args.output, args.season or current_season(datetime.now(timezone.utc)), args.env_file,
            args.base_run, args.workers, not args.no_edge, args.ingest_only)
    except Exception as error:
        # Connection exceptions can contain passwords; retain only the safe class.
        parser.exit(1, f'Refresh stopped ({type(error).__name__}). Check Docker and the stage logs under {args.output}.\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
