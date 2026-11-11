import test from 'node:test';
import assert from 'node:assert/strict';
import { chartMaximum, fetchAllPages, gameTime, median, queryString, teamFor } from '../src/data.js';
import { headshotSources, logoSources, matchupColors, pageRoute, paletteFor, playerPageURL, teamPageURL, themeStyle } from '../src/teamBranding.js';

test('player deep links preserve forecast context', () => {
  assert.deepEqual(pageRoute('/players/8478427'), { kind: 'player', playerId: 8478427 });
  assert.equal(playerPageURL(8478427, 20262027, { model: 'pace', target: 'goals', nominal: 0.95 }), '/players/8478427?season=20262027&model=pace&target=goals&nominal=0.95');
  assert.equal(pageRoute('/players/invalid').kind, 'not-found');
});

test('game clocks use viewer timezone, local date and daylight saving', () => {
  const winter = '2026-11-12T00:30:00Z';
  const london = gameTime(winter, 'Europe/London');
  const newYork = gameTime(winter, 'America/New_York');
  assert.match(london, /12/); assert.match(london, /00:30|12:30/); assert.match(london, /GMT|UTC/);
  assert.match(newYork, /11/); assert.match(newYork, /19:30|07:30|7:30/);
  assert.match(gameTime('2026-07-12T00:30:00Z', 'Europe/London'), /01:30|1:30/);
});

test('headshots use forecast season/team/ID, then a distinct stored URL', () => {
  const player = { player_id: 8478427, season_id: 20262027 };
  const team = { abbreviation: 'CAR' };
  const generated = 'https://assets.nhle.com/mugs/nhl/20262027/CAR/8478427.png';
  assert.deepEqual(headshotSources(player, team, { players: { 8478427: { headshot: generated } } }), [generated]);
  assert.deepEqual(headshotSources(player, team, { players: { 8478427: { headshot: 'https://assets.nhle.com/saved.png' } } }), [generated, 'https://assets.nhle.com/saved.png']);
});

test('team routes and logos keep historical IDs/seasons separate', () => {
  assert.deepEqual(pageRoute('/teams/59'), { kind: 'team', teamId: 59 });
  assert.equal(pageRoute('/teams').kind, 'directory');
  assert.equal(pageRoute('/teams/no-such-team').kind, 'not-found');
  assert.equal(teamPageURL(59, 20242025), '/teams/59?season=20242025');
  const team = { id: 59, abbreviation: 'UTA', logo: 'current.svg', seasonLogos: { 20242025: 'historical.svg' } };
  assert.equal(logoSources(team, 20242025)[0], '/team-logos/59-20242025.svg');
});

test('team themes have readable text and probability bars use team colours', () => {
  assert.equal(paletteFor({ abbreviation: 'CAR' }).primary, '#CD001A');
  assert.equal(themeStyle({ abbreviation: 'NSH' })['--team-on-primary'], '#000000');
  assert.deepEqual(matchupColors({ abbreviation: 'CAR' }, { abbreviation: 'NJD' }), { away: '#010101', home: '#CD001A' });
});

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
