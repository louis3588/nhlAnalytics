import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nhl_analytics.prediction_store import PredictionStore,document
from nhl_analytics.publication import write_release,validate_release,public_values
from test_api import fixture


class PublicationTests(unittest.TestCase):
    def setUp(self):
        Path('.work/tmp').mkdir(parents=True,exist_ok=True)
        temporary=tempfile.TemporaryDirectory(dir='.work/tmp');self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()
        self.store=PredictionStore.load(fixture(self.root),self.root/'cache')
        self.catalog=dict(players={'101':dict(name='Test Player',kind='skater',headshot='https://assets.nhle.com/a.png',private_note='ignored')},
            teams={'1':dict(id=1,name='Home Team',abbreviation='AAA'),'2':dict(id=2,name='Away Team',abbreviation='BBB')},
            seasons={'20262027':[1,2]},sources=['D:/private/research/players.csv'])
        self.output=self.root/'public'

    def publish(self,**kwargs):return write_release(self.store,self.catalog,{},self.output,**kwargs)

    def test_offline_idempotence_hashes_compression_and_no_private_paths(self):
        first=self.publish();second=self.publish()
        self.assertEqual(first['release_id'],second['release_id'])
        folder=self.output/'releases'/first['release_id']
        self.assertEqual(validate_release(folder)['player_count'],1)
        metadata=document(folder/'metadata.json')
        self.assertEqual(metadata['seasons'],[20262027]);self.assertFalse(metadata['live_available'])
        self.assertEqual(metadata['model_versions']['archived_prediction_bundle'],metadata['bundle_sha256'])
        exported=document(folder/'catalog.json')
        self.assertNotIn('sources',exported);self.assertNotIn('private_note',exported['players']['101'])
        self.assertEqual(len(document(folder/'players/101.json')['projections']),4)
        self.assertEqual(len(list((self.output/'releases').iterdir())),1)

    def test_partial_export_never_changes_active_pointer(self):
        first=self.publish();before=(self.output/'latest.json').read_bytes()
        changed=copy.deepcopy(self.store.metadata);changed['limitations']=['D:/private/research/file']
        with patch.object(self.store,'metadata',changed),self.assertRaises(ValueError):self.publish()
        self.assertEqual((self.output/'latest.json').read_bytes(),before)
        self.assertEqual(document(self.output/'latest.json')['release_id'],first['release_id'])

    def test_tampering_or_missing_identity_rejected(self):
        first=self.publish();folder=self.output/'releases'/first['release_id']
        (folder/'players.json').write_text('[]')
        with self.assertRaisesRegex(ValueError,'hash/size'):validate_release(folder)
        self.catalog['players']['101'].pop('name')
        with self.assertRaisesRegex(ValueError,'identity'):self.publish()

    def test_private_fields_and_nonfinite_values_rejected(self):
        for value in [dict(password='x'),dict(value='postgresql://private'),dict(value=float('nan')),dict(value='C:\\private\\file')]:
            with self.assertRaises(ValueError):public_values(value)
        public_values(dict(image='https://assets.nhle.com/a.png'))

    def test_history_is_bounded(self):
        result=self.publish(history_limit=1)
        folder=self.output/'releases'/result['release_id']
        self.assertEqual(len(document(folder/'players/101.json')['projections']),2)
        self.assertEqual(len(document(folder/'metadata.json')['snapshots']),1)
        with self.assertRaises(ValueError):self.publish(history_limit=1000)

    def test_late_forecast_cannot_be_presented_as_sealed(self):
        extras=dict(prospective=dict(items=[dict(status='sealed',max_source_available_at='2026-11-11T17:00:00Z',
            feature_as_of='2026-11-11T18:00:00Z',generated_at='2026-11-11T19:00:00Z',cutoff_at='2026-11-11T18:30:00Z')]))
        with self.assertRaisesRegex(ValueError,'cutoff provenance'):write_release(self.store,self.catalog,extras,self.output)


if __name__=='__main__':unittest.main()
