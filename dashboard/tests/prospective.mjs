import assert from 'node:assert/strict';
import { chromium } from 'playwright-core';
import { mkdir } from 'node:fs/promises';

const base = process.env.DASHBOARD_URL || 'http://127.0.0.1:4173';
await mkdir('test-results', { recursive: true });
const browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true, args: ['--disable-gpu', '--renderer-process-limit=1'] });
const page = await browser.newPage({ viewport: { width: 1440, height: 1100 }, timezoneId: 'Europe/London', locale: 'en-GB' });
const errors = [];
page.on('pageerror', e => errors.push(e.message));
try {
  await page.goto(`${base}/models`);
  await page.getByRole('heading', { name: 'From this point forward' }).waitFor();
  const real = await (await page.request.get(`${base}/api/prospective/performance`)).json();
  assert.equal(await page.locator('.model-coverage div').nth(1).locator('strong').textContent(), String(real.report.coverage.validated_paired));
  await page.getByRole('heading', { name: 'Reconstructed benchmarks' }).waitFor();
  assert.ok(await page.locator('.historical-reference tbody tr').count() > 0);
  if (!real.report.coverage.validated_paired) await page.getByText('No validated prospective results yet.', { exact: false }).waitFor();
  await page.screenshot({ path: 'test-results/model-performance-desktop.png' });
  await page.setViewportSize({ width: 390, height: 844 });
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'performance mobile fits');
  await page.screenshot({ path: 'test-results/model-performance-mobile.png' });

  const gameId = 2026029998;
  const item = { game_id: gameId, season_id: 20262027, home_team_id: 12, away_team_id: 3, status: 'sealed', timing: 'standard',
    scheduled_start: '2026-11-11T20:00:00Z', cutoff_at: '2026-11-11T19:00:00Z', generated_at: '2026-11-11T18:58:00Z',
    outcome_status: 'awaiting_final', outcome: null, models: [
      { model: 'frozen_elo', version: 'elo0123456789', home_win_probability: .6, away_win_probability: .4, favourite_team_id: 12, favourite_won: null, brier: null },
      { model: 'logistic_team_context', version: 'log0123456789', home_win_probability: .7, away_win_probability: .3, favourite_team_id: 12, favourite_won: null, brier: null },
    ] };
  await page.route(`**/api/prospective/games/${gameId}`, route => route.fulfill({ json: { item, revisions: [], health: { status: 'running' } } }));
  await page.route(`**/api/predictions/games/${gameId}`, route => route.fulfill({ status: 404, json: { detail: 'No archived prediction' } }));
  const live = { status: 'ready', stale: false, checked_at: '2026-11-11T20:10:00Z', collector: { status: 'running', active_poll_seconds: 20 },
    game: { game_id: gameId, season_id: 20262027, game_state: 'LIVE', schedule_state: 'OK', start_time_utc: item.scheduled_start,
      home: { id: 12, abbreviation: 'CAR', name: 'Carolina Hurricanes', score: 0, shots: 1 }, away: { id: 3, abbreviation: 'NYR', name: 'New York Rangers', score: 0, shots: 0 },
      players: { home: [], away: [] }, events: [], stats_available: false, period: { number: 1, periodType: 'REG' }, clock: { timeRemaining: '19:00' } } };
  await page.route(`**/api/live/games/${gameId}`, route => route.fulfill({ json: live }));
  await page.addInitScript(data => {
    window.__liveFixture = data;
    window.EventSource = class extends EventTarget {
      constructor() { super(); window.__liveSource = this; queueMicrotask(() => this.push()); }
      push() { this.dispatchEvent(new MessageEvent('game', { data: JSON.stringify(window.__liveFixture) })); }
      close() {}
    };
  }, live);
  await page.goto(`${base}/games/${gameId}`);
  await page.locator('.prospective-game .model-forecast').first().waitFor();
  assert.equal(await page.locator('.prospective-game .model-forecast').count(), 2);
  assert.match(await page.locator('.prospective-game').textContent(), /60.0%/);
  assert.match(await page.locator('.forecast-dates').textContent(), /GMT/);
  await page.evaluate(() => { window.__liveFixture.game.home.score = 3; window.__liveSource.push(); });
  assert.deepEqual(await page.locator('.game-score strong').allTextContents(), ['0', '3']);
  assert.match(await page.locator('.prospective-game').textContent(), /60.0%/);
  assert.match(await page.locator('.prospective-game').textContent(), /70.0%/);
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'comparison mobile fits');
  await page.screenshot({ path: 'test-results/prospective-game-mobile.png' });

  item.outcome_status = 'validated'; item.outcome = { home_score: 3, away_score: 1 };
  item.models.forEach((m, i) => { m.favourite_won = true; m.brier = i ? .09 : .16; });
  await page.reload();
  await page.getByText(/Brier 0.1600/).waitFor();
  await page.getByText(/Brier 0.0900/).waitFor();
  await page.getByText(/Validated final:/).waitFor();
  item.status = 'missing'; item.reason = 'missed_cutoff'; item.models = []; item.generated_at = null; item.outcome = null; item.outcome_status = 'awaiting_final';
  await page.reload();
  await page.locator('.prospective-game').getByText('missed cutoff', { exact: true }).waitFor();
  assert.equal(await page.locator('.prospective-game .model-forecast').count(), 0);
  assert.deepEqual(errors, []);
  console.log('PASS: real prospective/historical separation, empty evidence, responsive performance, sealed model pair, local cutoff times, frozen SSE probabilities, validated scoring and missing-forecast states.');
} finally { await browser.close(); }
