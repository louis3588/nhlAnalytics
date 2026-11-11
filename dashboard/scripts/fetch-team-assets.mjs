// Refresh official logo assets explicitly; normal dev/build stays offline.
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
const directory = new URL('../public/team-logos/', import.meta.url);
await mkdir(directory, { recursive: true });
const catalog = JSON.parse(await readFile(new URL('../public/catalog.json', import.meta.url), 'utf8'));
const teams = Object.values(catalog.teams).flatMap(team => [
  { ...team, file: `${team.id}.svg`, primary: true },
  ...Object.entries(team.seasonLogos || {}).filter(([, url]) => url !== team.logo).map(([season, logo]) => ({ ...team, logo, file: `${team.id}-${season}.svg`, primary: false })),
]);
const colors = {};
const downloads = new Map();
let next = 0;
async function worker() {
  while (next < teams.length) {
    const team = teams[next++];
    const url = new URL(team.logo || `https://assets.nhle.com/logos/nhl/svg/${team.abbreviation}_light.svg`);
    if (url.hostname !== 'assets.nhle.com' || url.protocol !== 'https:') throw new Error('Expected an official NHL asset URL');
    if (!downloads.has(url.href)) downloads.set(url.href, fetch(url, { signal: AbortSignal.timeout(20000) }).then(async response => {
      if (!response.ok) throw new Error(`Logo ${team.abbreviation}/${team.id}: HTTP ${response.status}`);
      return response.text();
    }));
    const svg = await downloads.get(url.href);
    if (!svg.includes('<svg')) throw new Error(`Invalid SVG for ${team.abbreviation}`);
    await writeFile(new URL(team.file, directory), svg);
    if (team.primary) colors[team.abbreviation] = [...new Set((svg.match(/#[\da-f]{6}\b/gi) || []).map(c => c.toUpperCase()))];
    console.log(`${team.file} ${team.abbreviation}`);
  }
}
await Promise.all([worker(), worker()]);
await writeFile(new URL('palette-sources.json', directory), JSON.stringify({ source: 'Official NHL SVG logo fills; interface palettes use representative team colours.', colors }, null, 2) + '\n');
console.log(`Saved ${teams.length} official logos to ${fileURLToPath(directory)}`);
