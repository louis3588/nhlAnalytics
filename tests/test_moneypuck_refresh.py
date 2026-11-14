import csv
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from nhl_analytics.database import connect, migrate
from nhl_analytics.moneypuck import import_file
from nhl_analytics.moneypuck_refresh import URL, BoundedReader, download, select_current, run, read_observed_team_xg

HEADER=['team','season','gameId','gameDate','situation','xGoalsFor','xGoalsAgainst','playoffGame']


def row(team='NYR',game='2026020001',season='2026',day='20261010'):
    return [team,season,game,day,'5on5','2.5','1.2','0']


def payload(rows):
    stream=io.StringIO(newline='');writer=csv.writer(stream);writer.writerow(HEADER);writer.writerows(rows)
    return stream.getvalue().encode()


class RefreshParsingTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(dir='.work/tmp');self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.now=datetime(2026,11,12,tzinfo=timezone.utc)
    def response(self,data,status=200):
        response=MagicMock();response.__enter__.return_value=response
        response.url=URL;response.status_code=status;response.headers={'Content-Type':'text/csv','ETag':'"fixture"'}
        raw=io.BytesIO(data);response.raw.read=lambda n,decode_content=True:raw.read(n)
        return response
    def test_only_current_regular_season_rows_retained(self):
        rows=[row(),row('BOS'),row(game='2024020001',season='2024',day='20241010'),row(game='2026030001')]
        dest=self.root/'selected.csv'
        result=select_current(csv.reader(io.StringIO(payload(rows).decode())),dest,20262027,self.now.date())
        self.assertEqual(result['source_rows'],4);self.assertEqual(result['selected_rows'],2)
        self.assertEqual(result['pairs'],{'2026020001':['BOS','NYR']})
        self.assertNotIn('2024',dest.read_text())
    def test_incomplete_pair_and_negative_xg_rejected(self):
        for rows in ([row()],[row(),row('BOS')[:-3]+['-1','1','0']]):
            with self.assertRaises(ValueError):
                select_current(csv.reader(io.StringIO(payload(rows).decode())),self.root/'bad.csv',20262027,self.now.date())
    def test_stream_byte_cap(self):
        raw=Mock();raw.read.return_value=b'too large'
        with self.assertRaisesRegex(ValueError,'byte limit'):BoundedReader(raw,limit=2).readinto(bytearray(20))
    def test_download_dedup_and_conditional_304_preserve_content(self):
        session=Mock();session.get.return_value=self.response(payload([row(),row('BOS')]))
        first,meta=download(self.root,20262027,session,self.now)
        session.get.return_value=self.response(b'',304)
        second,repeat=download(self.root,20262027,session,self.now+timedelta(hours=1))
        self.assertEqual(first,second);self.assertTrue(repeat['not_modified'])
        self.assertEqual(session.get.call_args.kwargs['headers']['If-None-Match'],'"fixture"')
        self.assertEqual(repeat['downloaded_at'],meta['downloaded_at'])
    def test_license_redirect_not_treated_as_data(self):
        session=Mock();response=self.response(b'<html>');response.url='https://moneypuck.com/data_license.htm'
        session.get.return_value=response
        with self.assertRaisesRegex(ValueError,'redirected'):download(self.root,20262027,session,self.now)
        self.assertFalse((self.root/'http-cache.json').exists())
    def test_completed_day_noop_does_not_repeat_network_or_import(self):
        metadata=dict(pairs={'2026020001':['BOS','NYR']},latest_game_date='2026-10-10')
        with patch('nhl_analytics.moneypuck_refresh.connect'),patch('nhl_analytics.moneypuck_refresh.migrate'), \
             patch('nhl_analytics.moneypuck_refresh.download',return_value=(self.root/'file.csv',metadata)) as fetch, \
             patch('nhl_analytics.moneypuck_refresh.import_file',return_value=2) as importer, \
             patch('nhl_analytics.moneypuck_refresh.coverage',return_value={'missing_completed_games':[]}):
            first=run(self.root,20262027);second=run(self.root,20262027)
            self.assertEqual(first,second);self.assertEqual(fetch.call_count,1);self.assertEqual(importer.call_count,1)


@unittest.skipUnless(os.environ.get('NHL_DB_TESTS')=='1','Set NHL_DB_TESTS=1 for database checks')
class ObservedSnapshotTests(unittest.TestCase):
    def test_completion_cutoff_and_retraction_select_whole_file(self):
        temporary=tempfile.TemporaryDirectory(dir='.work/tmp');self.addCleanup(temporary.cleanup)
        root=Path(temporary.name);paths=[];ids=[]
        with connect() as c:
            migrate(c)
            try:
                for i,rows in enumerate(([row(game='1991020001',season='1991',day='19911010'),row('BOS',game='1991020001',season='1991',day='19911010'),
                                          row(game='1991020002',season='1991',day='19911011'),row('BOS',game='1991020002',season='1991',day='19911011')],
                                         [row(game='1991020001',season='1991',day='19911010'),row('BOS',game='1991020001',season='1991',day='19911010')])):
                    path=root/f'{i}.csv';path.write_bytes(payload(rows));paths.append(path)
                    import_file(c,path,chunk_size=1)
                    from nhl_analytics.moneypuck import fingerprint
                    file_id=c.execute('SELECT file_id FROM analytics.moneypuck_files WHERE sha256=%s',(fingerprint(path),)).fetchone()[0];ids.append(file_id)
                future=datetime.now(timezone.utc)+timedelta(days=1)
                for i,file_id in enumerate(ids):c.execute('UPDATE analytics.moneypuck_files SET completed_at=%s WHERE file_id=%s',(future+timedelta(hours=i),file_id))
                before,_=read_observed_team_xg(c,19911992,future)
                self.assertEqual(before,[])
                older,_=read_observed_team_xg(c,19911992,future+timedelta(hours=1))
                self.assertEqual(len(older),4)
                latest,meta=read_observed_team_xg(c,19911992,future+timedelta(hours=1,microseconds=1))
                self.assertEqual(len(latest),2);self.assertEqual(meta['file_id'],str(ids[1]))
                self.assertEqual({r['game_id'] for r in latest},{1991020001})
            finally:
                for file_id in ids:
                    c.execute('DELETE FROM analytics.moneypuck_records WHERE file_id=%s',(file_id,))
                    c.execute('DELETE FROM analytics.moneypuck_files WHERE file_id=%s',(file_id,))
