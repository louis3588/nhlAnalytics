import assert from 'node:assert/strict';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import path from 'node:path';
const output = process.env.NHL_PUBLIC_BUILD_OUTPUT || 'dist-public';
const base = (process.argv[2] || 'https://nhl-rinkside-beta.pages.dev').replace(/\/$/, '');
if (!/^https:\/\/[a-z0-9.-]+\.pages\.dev$/.test(base)) throw new Error('Expected an HTTPS Pages deployment URL');
const expected = JSON.parse(await readFile(path.join(output, 'publication/latest.json'), 'utf8'));
async function get(path) {
  const response = await fetch(base + path, { signal: AbortSignal.timeout(30000), cache: 'no-store' });
  assert.equal(response.status, 200, `${path}: HTTP ${response.status}`);
  return response;
}
const root = await get('/');
assert.match(root.headers.get('content-security-policy') || '', /connect-src 'self'/);
assert.equal(root.headers.get('x-content-type-options'), 'nosniff');
const html = await root.text();
assert.equal(html, await readFile(path.join(output, 'index.html'), 'utf8'), 'Hosted frontend version');
const pointerResponse = await get('/publication/latest.json');
assert.match(pointerResponse.headers.get('cache-control') || '', /no-store/);
const pointer = await pointerResponse.json();
assert.equal(pointer.release_id, expected.release_id);
const prefix = `/publication/releases/${pointer.release_id}/`;
const manifestResponse = await get(prefix + 'manifest.json');
assert.match(manifestResponse.headers.get('cache-control') || '', /immutable/);
const manifest = await manifestResponse.json();
for (const name of ['metadata.json', 'catalog.json', 'players.json', 'intervals.json', 'fixtures.json', 'standings.json', 'prospective.json']) {
  const bytes = Buffer.from(await (await get(prefix + name)).arrayBuffer());
  assert.equal(createHash('sha256').update(bytes).digest('hex'), manifest.files[name].sha256, name);
}
const players = JSON.parse(await readFile(path.join(output, prefix, 'players.json'), 'utf8'));
const fixtures = JSON.parse(await readFile(path.join(output, prefix, 'fixtures.json'), 'utf8'));
for (const path of ['/models', `/players/${players[0].player_id}`, `/teams/${players[0].last_played_team_id}`, `/games/${fixtures[0].game_id}`]) {
  const page = await get(path);
  assert.equal(new URL(page.url).pathname, path, `${path}: preserve SPA URL`);
  assert.equal(await page.text(), html, `${path}: SPA deep link`);
}
const absentAPI = await fetch(base + '/api/metadata');
assert.ok(absentAPI.status === 404 || absentAPI.status === 200 && !(absentAPI.headers.get('content-type') || '').includes('application/json'), 'No public JSON API');
const asset = /src="(\/assets\/[^\"]+\.js)"/.exec(html)?.[1];
assert.ok(asset);
const assetResponse = await get(asset);
assert.match(assetResponse.headers.get('cache-control') || '', /immutable/);
const bytes = Buffer.from(await assetResponse.arrayBuffer());
assert.equal(createHash('sha256').update(bytes).digest('hex'), createHash('sha256').update(await readFile(path.join(output, asset))).digest('hex'));
const result = { url: base, verified_at: new Date().toISOString(), release_id: pointer.release_id, source_as_of: pointer.source_as_of,
  player_count: manifest.player_count, checks: ['HTTPS', 'CSP', 'cache headers', 'seven core JSON hashes', 'four SPA deep links', 'JS asset hash', 'API absent'] };
const reportPath = process.env.NHL_PUBLIC_VERIFY_REPORT || 'test-results/deployment.json';
await mkdir(path.dirname(reportPath), { recursive: true });
await writeFile(reportPath, JSON.stringify(result, null, 2));
console.log(JSON.stringify(result, null, 2));
