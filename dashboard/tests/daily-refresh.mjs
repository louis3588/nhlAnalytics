import { chromium } from 'playwright-core';
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
const base = process.env.DASHBOARD_URL || 'http://127.0.0.1:5173';
await mkdir('test-results', { recursive: true });
const browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true, args: ['--disable-gpu', '--renderer-process-limit=1'] });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, reducedMotion: 'reduce' });
const checks = [], errors = [];
page.on('pageerror', error => errors.push(error.message));
let playerRequests = 0, gameRequests = 0;
page.on('request', request => {
  if (request.url().includes('/api/predictions/players?')) playerRequests++;
  if (request.url().includes('/api/predictions/games?')) gameRequests++;
});
async function ready() { await page.waitForFunction(() => document.querySelectorAll('tbody tr').length && !document.querySelector('[aria-busy="true"]')); }
try {
  await page.clock.install();
  await page.goto(base); await ready();
  const beforePlayers = playerRequests, beforeGames = gameRequests;
  // A clock tick refreshes fixtures even if the saved bundle is unchanged.
  await page.clock.fastForward(61000);
  await page.waitForFunction(() => !document.querySelector('[aria-busy="true"]'));
  assert.ok(gameRequests > beforeGames);
  assert.equal(playerRequests, beforePlayers);
  checks.push('Minute clock refreshes upcoming games without reloading unchanged player predictions.');
  // Simulate a newly published coherent bundle at the API boundary.
  await page.route('**/api/**', async route => {
    if (new URL(route.request().url()).pathname.startsWith('/api/live/')) return route.continue();
    const response = await route.fetch();
    const data = await response.json();
    if (data.bundle_sha256) data.bundle_sha256 = 'test-new-daily-publication';
    if (data.metadata) data.metadata.bundle_sha256 = 'test-new-daily-publication';
    await route.fulfill({ response, json: data });
  });
  const reloadedPlayers = page.waitForRequest(request => request.url().includes('/api/predictions/players?'));
  await page.clock.fastForward(61000);
  await reloadedPlayers;
  await ready();
  assert.ok(playerRequests > beforePlayers);
  assert.equal(await page.getByRole('alert').count(), 0);
  checks.push('A changed daily bundle automatically reloads a coherent forecast cohort.');
  await page.unroute('**/api/**');
  await page.route('**/api/predictions/players/8478427/history?*', async route => {
    const response = await route.fetch(); const data = await response.json();
    const latest = data.items.at(-1);
    data.items = Array.from({ length: 30 }, (_, i) => {
      const item = structuredClone(latest);
      const date = i === 29 ? latest.projection.prediction_at : new Date(Date.parse(latest.projection.prediction_at) - (29 - i) * 86400000).toISOString();
      item.projection.prediction_at = date;
      if (item.interval) item.interval.prediction_at = date;
      return item;
    });
    data.total = 30;
    await route.fulfill({ response, json: data });
  });
  await page.goto(`${base}/players/8478427?season=20262027`);
  await page.locator('.history-picker').waitFor();
  assert.equal(await page.getByLabel('Saved prediction date', { exact: true }).locator('option').count(), 30);
  assert.ok(await page.locator('.history-value').count() <= 7);
  assert.equal(await page.locator('.history-chart tbody tr').count(), 30);
  await page.setViewportSize({ width: 390, height: 844 });
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  checks.push('Thirty daily points retain every data row while graph labels/date buttons remain bounded on mobile.');
  assert.deepEqual(errors, []);
  await writeFile('test-results/daily-refresh.json', JSON.stringify({ checks, errors }, null, 2));
  console.log(`PASS: ${checks.length} automatic-refresh browser check groups`);
} finally { await browser.close(); }
