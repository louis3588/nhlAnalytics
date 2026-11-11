import { chromium } from 'playwright-core';
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
const base = process.env.DASHBOARD_URL || 'http://127.0.0.1:5173';
await mkdir('test-results', { recursive: true });
const browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true, args: ['--disable-gpu', '--renderer-process-limit=1'] });
const page = await browser.newPage({ viewport: { width: 1440, height: 1100 }, reducedMotion: 'reduce' });
const errors = []; const checks = [];
page.on('pageerror', error => errors.push(error.message));
async function ready() { await page.waitForFunction(() => document.querySelectorAll('tbody tr').length > 0 && !document.querySelector('[aria-busy="true"]')); await page.evaluate(() => document.fonts.ready); }
try {
  await page.goto(`${base}/teams?season=20262027`);
  await page.waitForSelector('.team-directory-card');
  assert.equal(await page.locator('.team-directory-card').count(), 32);
  await page.screenshot({ path: 'test-results/team-directory.png', fullPage: true });
  await page.locator('.team-directory-card').filter({ hasText: 'Carolina Hurricanes' }).click();
  await ready();
  assert.ok(page.url().includes('/teams/12?season=20262027'));
  assert.equal(await page.locator('h1').textContent(), 'Carolina Hurricanes');
  assert.equal(await page.locator('#team').inputValue(), '12');
  assert.equal(await page.locator('.workspace').evaluate(el => getComputedStyle(el).getPropertyValue('--team-primary').trim()), '#CD001A');
  assert.equal(await page.locator('.table-player small').evaluateAll(nodes => nodes.every(n => n.textContent.startsWith('CAR'))), true);
  await page.locator('.team-hero img').evaluate(img => img.decode());
  assert.ok(await page.locator('.team-hero img').evaluate(img => img.naturalWidth > 0));
  await page.getByRole('button', { name: 'View Sebastian Aho details', exact: true }).click();
  const portrait = page.locator('.focus-panel .player-headshot img');
  await portrait.evaluate(img => img.decode());
  assert.equal(await portrait.getAttribute('src'), 'https://assets.nhle.com/mugs/nhl/20262027/CAR/8478427.png');
  assert.ok(await portrait.evaluate(img => img.naturalWidth > 0));
  assert.equal(await page.locator('.observed-bar').first().evaluate(el => getComputedStyle(el).backgroundColor), 'rgb(205, 0, 26)');
  checks.push('32 team pages are discoverable; Carolina page loads its own players, theme, official logo and a real season/team/ID headshot.');
  await page.evaluate(() => scrollTo(0, 0));
  await page.screenshot({ path: 'test-results/carolina-team-page.png', fullPage: true });
  await page.reload(); await ready();
  assert.equal(await page.locator('h1').textContent(), 'Carolina Hurricanes');
  assert.ok((await page.title()).includes('Carolina Hurricanes'));
  const game = page.locator('.game-card').first();
  await game.scrollIntoViewIfNeeded();
  assert.equal(await game.locator('.team-logo img').count(), 2);
  assert.ok((await game.innerText()).includes('Carolina Hurricanes'));
  assert.ok((await game.locator('.probability-bar span').evaluateAll(nodes => nodes.map(n => n.style.backgroundColor))).includes('rgb(205, 0, 26)'));
  checks.push('Direct links and reloads preserve team/season; games use official logos and team-coloured probability segments.');

  await page.getByLabel('Season', { exact: true }).selectOption('20242025'); await ready();
  assert.equal(await page.locator('#team').inputValue(), '12');
  assert.ok(page.url().includes('season=20242025'));
  await page.goto(`${base}/teams/59?season=20242025`); await ready();
  assert.equal(await page.locator('h1').textContent(), 'Utah Hockey Club');
  await page.goto(`${base}/teams/68?season=20262027`); await ready();
  assert.equal(await page.locator('h1').textContent(), 'Utah Mammoth');
  checks.push('Team season changes retain filters, and Utah Hockey Club/Mammoth remain distinct historical pages.');

  await page.route('https://assets.nhle.com/mugs/nhl/20262027/CAR/8478427.png', route => route.abort());
  await page.route('**/api/catalog', async route => {
    const response = await route.fetch(); const catalog = await response.json();
    catalog.players[8478427].headshot = '/rink.svg';
    await route.fulfill({ response, json: catalog });
  });
  await page.goto(`${base}/teams/12?season=20262027`); await ready();
  await page.getByRole('button', { name: 'View Sebastian Aho details', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.focus-panel .player-headshot img')?.getAttribute('src') === '/rink.svg');
  await page.unroute('**/api/catalog');
  // The stored string equals the primary URL for Aho, so there is only one
  // request before the neutral silhouette (no retry loop or guessed photo).
  await page.reload(); await ready();
  await page.getByRole('button', { name: 'View Sebastian Aho details', exact: true }).click();
  await page.locator('.focus-panel .image-placeholder').waitFor();
  await page.unroute('https://assets.nhle.com/mugs/nhl/20262027/CAR/8478427.png');
  checks.push('Failed generated headshots try a distinct stored image, then use a neutral silhouette when no usable image remains.');

  await page.reload(); await ready();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => scrollTo(0, 0));
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await page.screenshot({ path: 'test-results/team-page-mobile.png', fullPage: true });
  await page.goto(base); await ready();
  await page.getByRole('link', { name: 'Browse team pages', exact: true }).click();
  await page.waitForSelector('.team-directory-card');
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  checks.push('Team pages and the team directory fit 390px mobile screens without page overflow.');
  await page.goto(`${base}/teams/999999?season=20262027`);
  await page.getByRole('heading', { name: 'Team page not found' }).waitFor();
  assert.deepEqual(errors, []);
  await writeFile('test-results/team-pages-report.json', JSON.stringify({ status: 'passed', checks, browserErrors: errors }, null, 2));
  console.log(checks.join('\n'));
} catch (error) {
  await page.screenshot({ path: 'test-results/team-failure.png', fullPage: true }).catch(() => {});
  throw error;
} finally { await browser.close(); }
