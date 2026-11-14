import assert from 'node:assert/strict';
import { chromium } from 'playwright-core';
import AxeBuilder from '@axe-core/playwright';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
const base = process.env.PUBLIC_DASHBOARD_URL || 'http://127.0.0.1:4174';
const output = process.env.NHL_PUBLIC_BUILD_OUTPUT || 'dist-public';
const pointer = JSON.parse(await readFile(`${output}/publication/latest.json`, 'utf8'));
const root = `${output}/publication/releases/${pointer.release_id}`;
const players = JSON.parse(await readFile(`${root}/players.json`, 'utf8'));
const fixtures = JSON.parse(await readFile(`${root}/fixtures.json`, 'utf8'));
const player = players[0];
const browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true, args: ['--disable-gpu', '--renderer-process-limit=1'] });
const results = [];
await mkdir('test-results', { recursive: true });
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: 'reduce' });
  const page = await context.newPage();
  for (const route of ['/', `/players/${player.player_id}`, `/teams/${player.last_played_team_id}`, '/models', `/games/${fixtures[0].game_id}`]) {
    await page.goto(base + route);
    await page.getByText('Created by Louis Polly', { exact: true }).waitFor();
    await page.locator('main h1').first().waitFor();
    await page.waitForTimeout(300);
    const axe = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']).analyze();
    results.push({ route, violations: axe.violations.map(v => ({ id: v.id, impact: v.impact, description: v.description, nodes: v.nodes.map(n => ({ target: n.target, summary: n.failureSummary })) })) });
    await page.setViewportSize({ width: 390, height: 844 });
    const overflow = await page.evaluate(() => [...document.querySelectorAll('body *')].filter(e => { const r=e.getBoundingClientRect(); return r.width && (r.right > innerWidth + 1 || r.left < -1) && getComputedStyle(e).position !== 'fixed' && !e.closest('.table-scroll'); }).slice(0, 8).map(e => ({ tag: e.tagName, class: e.className, width: e.getBoundingClientRect().width })));
    results.at(-1).mobileOverflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
    results.at(-1).overflowElements = overflow;
    const mobileAxe = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']).analyze();
    results.at(-1).mobileViolations = mobileAxe.violations.map(v => ({ id: v.id, targets: v.nodes.map(n => n.target) }));
    await page.screenshot({ path: `test-results/accessibility-${route === '/' ? 'overview' : route.split('/')[1]}-mobile.png` });
    await page.setViewportSize({ width: 320, height: 844 });
    results.at(-1).reflow320Overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
    if (route === '/') {
      await page.setViewportSize({ width: 900, height: 1000 });
      const tabletAxe = await new AxeBuilder({ page }).withTags(['wcag2a','wcag2aa','wcag21a','wcag21aa','wcag22aa']).analyze();
      results.at(-1).tabletViolations = tabletAxe.violations.map(v => ({ id: v.id, targets: v.nodes.map(n => n.target) }));
    }
    await page.setViewportSize({ width: 1440, height: 1000 });
  }
  await writeFile('test-results/accessibility.json', JSON.stringify(results, null, 2));
  console.log(JSON.stringify(results));
  assert.ok(results.every(r => !r.violations.length && !r.mobileViolations.length && !r.mobileOverflow && !r.reflow320Overflow && !r.tabletViolations?.length), 'Accessibility or mobile overflow findings: see test-results/accessibility.json');
} finally { await browser.close(); }
