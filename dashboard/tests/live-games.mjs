import { chromium } from 'playwright-core';
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
const base = process.env.DASHBOARD_URL || 'http://127.0.0.1:5173';
await mkdir('test-results', { recursive: true });
const browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true, args: ['--disable-gpu', '--renderer-process-limit=1'] });
const page = await browser.newPage({ viewport: { width: 1440, height: 1100 }, timezoneId: 'Europe/London', locale: 'en-GB', reducedMotion: 'reduce' });
const errors = [], checks = [];
page.on('pageerror', error => errors.push(error.message));
try {
  await page.goto(base);
  await page.locator('.live-game-tile').first().waitFor();
  await page.locator('.live-game-tile').filter({ hasText: 'Final' }).first().click();
  await page.locator('.game-stats-table tbody tr').first().waitFor();
  const id = Number(new URL(page.url()).pathname.split('/').at(-1));
  const expected = await (await page.request.get(`${base}/api/live/games/${id}`)).json();
  assert.deepEqual(await page.locator('.game-score strong').allTextContents(), [String(expected.game.away.score), String(expected.game.home.score)]);
  assert.equal(await page.locator('.scoreboard-team img').count(), 2);
  assert.match(await page.locator('.scoreboard-meta time').textContent(), /GMT/);
  await page.getByRole('button', { name: 'Show all events', exact: true }).click();
  assert.ok(await page.locator('.live-event-list li').count() > 0);
  await page.evaluate(() => document.fonts.ready);
  await page.evaluate(() => scrollTo(0, 0));
  await page.screenshot({ path: 'test-results/game-center-desktop.png' });
  await page.reload(); await page.locator('.game-stats-table tbody tr').first().waitFor();
  checks.push('Real GameCenter directory opens a persisted final with accurate scores, local game time, logos, tables, timeline and direct reload.');
  await page.setViewportSize({ width: 390, height: 844 });
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.screenshot({ path: 'test-results/game-center-mobile.png' });
  checks.push('Game center fits 390px; wide stats tables scroll inside the panel.');
  const fixture = {
    status: 'ready', stale: false, checked_at: '2026-11-11T13:00:00Z', observed_at: '2026-11-11T13:00:00Z', revision: 'fixture',
    collector: { status: 'running', error: null, active_poll_seconds: 20 },
    game: { game_id: 2026029998, season_id: 20262027, game_state: 'LIVE', schedule_state: 'OK', start_time_utc: '2026-11-11T12:30:00Z', game_date: '2026-11-11', venue: 'Browser fixture arena',
      home: { id: 12, abbreviation: 'CAR', name: 'Carolina Hurricanes', score: 1, shots: 5 }, away: { id: 3, abbreviation: 'NYR', name: 'New York Rangers', score: 0, shots: 3 },
      period: { number: 1, periodType: 'REG' }, clock: { timeRemaining: '12:00', running: true }, stats_available: true,
      players: { home: [{ player_id: 8478427, name: 'Sebastian Aho', number: 20, position: 'C', goalie: false, stats: { goals: 1, assists: 0, points: 1, sog: 2, plusMinus: 1, pim: 0, toi: '04:00', hits: 1, blockedShots: 0 } }], away: [] },
      events: [{ event_id: 1, sort_order: 1, type: 'goal', period: 1, period_type: 'REG', time: '08:00', team_id: 12, player_id: 8478427, player_name: 'Sebastian Aho', details: { homeScore: 1, awayScore: 0 } }],
    },
  };
  await page.route('**/api/live/games/2026029998', route => route.fulfill({ json: fixture }));
  await page.addInitScript(data => {
    window.__liveFixture = data;
    window.EventSource = class extends EventTarget {
      constructor() { super(); window.__liveSource = this; queueMicrotask(() => this.push()); }
      push() { this.dispatchEvent(new MessageEvent('game', { data: JSON.stringify(window.__liveFixture) })); }
      close() {}
    };
  }, fixture);
  await page.goto(`${base}/games/2026029998`);
  await page.locator('.game-score').waitFor();
  await page.evaluate(() => { window.__liveFixture.game.home.score = 2; window.__liveFixture.game.events[0].player_name = 'Corrected scorer'; window.__liveSource.push(); });
  await page.getByText('Corrected scorer', { exact: true }).waitFor();
  assert.deepEqual(await page.locator('.game-score strong').allTextContents(), ['0', '2']);
  assert.equal(await page.locator('.live-event-list li').count(), 1);
  await page.evaluate(() => { window.__liveFixture.game.events = []; window.__liveSource.push(); });
  await page.getByText('The story is still to come', { exact: true }).waitFor();
  assert.equal(await page.locator('.live-event-list li').count(), 0);
  checks.push('SSE updates change scores, replace corrected events and remove retracted events without duplicate counting.');
  await page.evaluate(() => window.__liveSource.onerror());
  await page.getByText(/Live connection interrupted/).waitFor();
  assert.deepEqual(await page.locator('.game-score strong').allTextContents(), ['0', '2']);
  await page.evaluate(() => { window.__liveFixture.stale = true; window.__liveFixture.collector.error = 'NHL feed delayed; showing saved data'; window.__liveSource.push(); });
  await page.getByText('Updates delayed', { exact: true }).waitFor();
  await page.evaluate(() => { window.__liveFixture.stale = false; window.__liveFixture.collector.error = null; window.__liveSource.push(); });
  await page.getByText('Updates connected', { exact: true }).waitFor();
  checks.push('Reconnects retain the last score, expose stale provider data and recover when a fresh snapshot arrives.');
  await page.evaluate(() => { const game = window.__liveFixture.game; game.game_state = 'FUT'; game.players.home = []; game.stats_available = false; game.home.score = null; game.away.score = null; game.clock = null; window.__liveSource.push(); });
  await page.getByText('Player stats aren’t published yet', { exact: true }).waitFor();
  assert.deepEqual(await page.locator('.game-score strong').allTextContents(), ['—', '—']);
  checks.push('Pregame missing statistics and scores remain unavailable rather than fabricated zeroes.');
  assert.deepEqual(errors, []);
  await writeFile('test-results/live-games.json', JSON.stringify({ checks, errors }, null, 2));
  console.log(`PASS: ${checks.length} live-game browser check groups`);
} finally { await browser.close(); }
