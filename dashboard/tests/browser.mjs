import { chromium } from 'playwright-core';
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../', import.meta.url));
const output = path.join(root, 'test-results');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true, args: ['--disable-gpu', '--renderer-process-limit=1'], timeout: 45000 });
const errors = [];
const page = await browser.newPage({ viewport: { width: 1440, height: 1100 }, reducedMotion: 'reduce' });
page.on('pageerror', error => errors.push(error.message));
const base = process.env.DASHBOARD_URL || 'http://127.0.0.1:5173';
const report = [];
async function ready() {
  await page.waitForFunction(() => document.querySelectorAll('tbody tr').length > 0 && !document.querySelector('.loader, [aria-busy="true"]'), undefined, { timeout: 30000 });
  await page.evaluate(() => document.fonts.ready);
}
try {
  await page.goto(base, { waitUntil: 'networkidle' });
  await ready();
  assert.equal(await page.locator('tbody tr').count(), 10);
  assert.equal(await page.getByLabel('Season', { exact: true }).inputValue(), '20262027');
  assert.ok(!(await page.locator('tbody').innerText()).includes('NHL player #'));
  await page.screenshot({ path: path.join(output, 'overview-desktop.png'), fullPage: true });
  report.push('Default current season, real names, forecasts, intervals and game cards load.');

  await page.getByLabel('Team', { exact: true }).selectOption('21');
  await ready();
  assert.ok((await page.locator('tbody').innerText()).includes('COL'));
  assert.ok((await page.locator('.stat-card').first().innerText()).includes('Players in view'));
  assert.equal(await page.locator('.table-player small').evaluateAll(nodes => nodes.every(n => n.textContent.startsWith('COL'))), true);
  report.push('Team filter fetches Colorado-only players and games.');

  await page.getByLabel('Team', { exact: true }).selectOption('');
  await ready();
  await page.getByLabel('Search players').fill('Crosby');
  await page.waitForFunction(() => document.querySelectorAll('tbody tr').length === 1);
  assert.ok((await page.locator('tbody').innerText()).includes('Sidney Crosby'));
  await page.getByRole('button', { name: 'View Sidney Crosby details', exact: true }).click();
  assert.ok((await page.getByTestId('player-focus').innerText()).includes('Sidney Crosby'));
  await page.getByLabel('Clear player search').click();
  await page.locator('.confidence-control').getByRole('button', { name: '95%' }).click();
  await ready();
  assert.equal(await page.locator('.confidence-control').getByRole('button', { name: '95%' }).getAttribute('aria-pressed'), 'true');
  assert.ok((await page.locator('.band-card').innerText()).includes('95% prediction interval'));
  await page.locator('.target-control').getByRole('button', { name: 'Goals', exact: true }).click();
  await ready();
  assert.ok((await page.locator('figcaption').innerText()).includes('Season goals'));
  await page.getByLabel('Projection model', { exact: true }).selectOption('pace');
  await ready();
  await page.getByRole('button', { name: 'Next player page' }).click();
  assert.ok((await page.locator('.table-panel .pagination').innerText()).includes('11–20'));
  report.push('Search, player focus, nominal level, target, model and player pagination work.');

  await page.getByLabel('Season', { exact: true }).selectOption('20212022');
  await ready();
  assert.ok((await page.locator('.band-card').innerText()).includes('Not available'));
  assert.ok((await page.locator('#games').innerText()).includes('No upcoming games'));
  await page.getByRole('button', { name: /View all saved games/ }).click();
  await page.waitForSelector('.game-card');
  assert.ok((await page.locator('.game-card').first().innerText()).includes('Completed'));
  report.push('Historical season shows missing calibration honestly and can display saved completed-game forecasts.');

  await page.getByLabel('Season', { exact: true }).selectOption('20242025');
  await ready();
  assert.equal(await page.locator('#team option[value="59"]').textContent(), 'Utah Hockey Club');
  await page.getByLabel('Season', { exact: true }).selectOption('20262027');
  await ready();
  assert.equal(await page.locator('#team option[value="68"]').textContent(), 'Utah Mammoth');
  report.push('Historical Utah team IDs/names remain season-specific.');

  await page.locator('.target-control').getByRole('button', { name: 'Points', exact: true }).click();
  await page.getByLabel('Projection model', { exact: true }).selectOption('prior_shrinkage');
  await page.locator('.confidence-control').getByRole('button', { name: '80%' }).click();
  await page.getByRole('button', { name: 'Upcoming', exact: true }).click();
  await ready();
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.locator('.chart-row').first().hover();
  await page.locator('.chart-row').first().locator('[role="tooltip"]').waitFor({ state: 'visible' });
  await page.screenshot({ path: path.join(output, 'hover-desktop.png') });
  await page.mouse.move(2, 2);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => window.scrollTo(0, 0));
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
  await page.screenshot({ path: path.join(output, 'overview-mobile.png'), fullPage: true });
  report.push('Floating chart tooltips and mobile layout work without page-level horizontal overflow.');

  const offline = page;
  await offline.route('**/api/metadata', route => route.abort());
  await offline.goto(base);
  await offline.getByRole('heading', { name: 'Let’s reconnect to your forecasts' }).waitFor();
  assert.ok((await offline.locator('.connection-error').innerText()).includes('Retry connection'));
  await offline.unroute('**/api/metadata');
  await offline.getByRole('button', { name: 'Retry connection' }).click();
  await offline.waitForSelector('tbody tr');
  report.push('API disconnection displays an actionable error, and retry restores the dashboard.');
  await page.route('**/api/predictions/intervals?**', route => route.abort());
  await page.getByLabel('Refresh saved forecasts').click();
  await page.waitForFunction(() => document.querySelector('.band-card')?.textContent.includes('Couldn’t load interval'));
  assert.ok(!(await page.locator('.band-card').innerText()).includes('Not enough earlier calibration history'));
  await page.unroute('**/api/predictions/intervals?**');
  await page.getByLabel('Refresh saved forecasts').click();
  await ready();
  report.push('A failed interval request is distinguished from genuinely unavailable historical calibration.');
  assert.deepEqual(errors, []);
  await writeFile(path.join(output, 'browser-report.json'), JSON.stringify({ status: 'passed', checks: report, browserErrors: errors }, null, 2));
  console.log(report.join('\n'));
} catch (error) {
  await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {});
  throw error;
} finally { await browser.close(); }
