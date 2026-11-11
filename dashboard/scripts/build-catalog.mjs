// Display names only. Predictions always come from FastAPI, never these files.
// Read small saved plans/schedule envelopes, not CSV/shot archives or Timescale.
import { readdir, readFile, mkdir, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = fileURLToPath(new URL('../', import.meta.url));
const output = fileURLToPath(new URL('../public/catalog.json', import.meta.url));
const repo = path.resolve(root, '..');
let previous = {};
try { previous = JSON.parse(await readFile(output, 'utf8')); }
catch (error) { if (error.code !== 'ENOENT') throw error; }
const players = { ...previous.players };
const teams = { ...previous.teams };
const seasons = { ...previous.seasons };
const sources = [];
async function directories(relative) {
  try { return (await readdir(path.join(repo, relative), { withFileTypes: true })).filter(d => d.isDirectory()).map(d => d.name).sort(); }
  catch (error) { if (error.code === 'ENOENT') return []; throw error; }
}
for (const directory of await directories('data/nhl_game_logs')) {
  const relative = `data/nhl_game_logs/${directory}/plan.json`;
  try {
    const plan = JSON.parse(await readFile(path.join(repo, relative), 'utf8'));
    for (const row of plan.participants || []) if (row.fullName) players[row.playerId] = { ...players[row.playerId], name: row.fullName, kind: row.kind };
    sources.push(relative);
  } catch (error) { if (error.code !== 'ENOENT') throw error; }
}
for (const directory of await directories('data/nhl_schedules')) {
  const relative = `data/nhl_schedules/${directory}`;
  for (const file of (await readdir(path.join(repo, relative))).filter(n => /^\d{8}\.json$/.test(n)).sort()) {
    const snapshot = JSON.parse(await readFile(path.join(repo, relative, file), 'utf8'));
    const ids = [];
    for (const row of snapshot.teams || []) {
      const seasonLogos = { ...teams[row.id]?.seasonLogos, [snapshot.seasonId]: row.logo || `https://assets.nhle.com/logos/nhl/svg/${row.abbrev}_light.svg` };
      teams[row.id] = { ...teams[row.id], id: row.id, name: row.name.default, abbreviation: row.abbrev, seasonLogos,
        logo: seasonLogos[Math.max(...Object.keys(seasonLogos).map(Number))] };
      ids.push(row.id);
    }
    if (ids.length) seasons[snapshot.seasonId] = ids;
    sources.push(`${relative}/${file}`);
  }
}
// Optional saved roster strings are display-image fallbacks, not model inputs.
try {
  const roster = JSON.parse(await readFile(path.join(repo, 'nhl-rosters.json'), 'utf8'));
  for (const team of roster.teams || []) for (const player of team.players || []) {
    if (player.headshot && players[player.id]) players[player.id].headshot = player.headshot;
  }
  sources.push('nhl-rosters.json');
} catch (error) { if (error.code !== 'ENOENT') throw error; }
if (!Object.keys(players).length) {
  try {
    const existing = JSON.parse(await readFile(output, 'utf8'));
    console.log(`Using bundled display catalog (${Object.keys(existing.players).length} player names).`);
    process.exit(0);
  } catch { throw new Error('No saved identity metadata found. Run this from the existing NHL project first.'); }
}
await mkdir(path.dirname(output), { recursive: true });
await writeFile(output, `${JSON.stringify({ players, teams, seasons, sources: sources.length ? sources : previous.sources || [] }, null, 2)}\n`);
console.log(`Display catalog: ${Object.keys(players).length} players, ${Object.keys(teams).length} teams. No predictions generated.`);
