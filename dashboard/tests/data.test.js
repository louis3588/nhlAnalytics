import test from 'node:test';
import assert from 'node:assert/strict';
import { chartMaximum, fetchAllPages, median, queryString, teamFor } from '../src/data.js';

test('chart domain includes prediction interval upper endpoints', () => {
  const rows = [{ player_id: 1, observed: { points: 50 }, projected: { points: 105 } }];
  assert.ok(chartMaximum(rows, new Map([[1, { available: true, lower: 80, upper: 163 }]]), 'points') >= 163);
  assert.ok(chartMaximum(rows, new Map([[1, { available: false, upper: null }]]), 'points') >= 105);
});

test('missing calibration is not converted into a zero-width interval', () => {
  assert.equal(chartMaximum([], new Map(), 'points'), 5);
  assert.equal(median([]), null);
  assert.equal(median([100, 20, 30, 40]), 35);
});

test('query filters preserve timestamp timezone and omit missing team', () => {
  const query = new URLSearchParams(queryString({ prediction_at: '2026-11-10T19:35:00+00:00', team_id: undefined, offset: 0 }));
  assert.equal(query.get('prediction_at'), '2026-11-10T19:35:00+00:00');
  assert.equal(query.get('offset'), '0');
  assert.equal(query.has('team_id'), false);
});

test('team identities remain separate across Utah seasons', () => {
  const catalog = { teams: { 59: { id: 59, name: 'Utah Hockey Club' }, 68: { id: 68, name: 'Utah Mammoth' } } };
  assert.equal(teamFor(catalog, 59).name, 'Utah Hockey Club');
  assert.equal(teamFor(catalog, 68).name, 'Utah Mammoth');
});

test('pagination loads the entire filtered cohort before calculating leaders', async t => {
  const previous = globalThis.fetch;
  t.after(() => { globalThis.fetch = previous; });
  const calls = [];
  globalThis.fetch = async url => {
    calls.push(url);
    const offset = Number(new URL(url, 'http://localhost').searchParams.get('offset'));
    return { ok: true, json: async () => ({ metadata: { bundle_sha256: 'a' }, total: 201, snapshot_at: 'date', items: offset ? [200] : Array.from({ length: 200 }, (_, i) => i) }) };
  };
  const result = await fetchAllPages('/api/predictions/players', {}, undefined, 'a');
  assert.equal(result.items.length, 201);
  assert.equal(result.items.at(-1), 200);
  assert.equal(calls.length, 2);
});

test('mixed snapshot pages fail instead of displaying a misleading combined cohort', async t => {
  const previous = globalThis.fetch;
  t.after(() => { globalThis.fetch = previous; });
  let calls = 0;
  globalThis.fetch = async () => ({ ok: true, json: async () => ({ metadata: { bundle_sha256: ++calls === 1 ? 'a' : 'b' }, total: 201, snapshot_at: 'date', items: Array(200).fill(1) }) });
  await assert.rejects(fetchAllPages('/api/predictions/players', {}, undefined, 'a'), /snapshot changed/);
});
