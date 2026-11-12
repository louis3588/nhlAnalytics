// One response adapter: existing components keep their API-shaped data contract.
export const STATIC = import.meta.env?.VITE_DATA_MODE === 'static';
const ROOT = '/publication';
let pinned, pinning;
const files = new Map();

export async function requestJSON(url, signal) {
  if (STATIC && url.startsWith('/api/')) return staticRequest(url, signal);
  const response = await fetch(url, { signal });
  if (!response.ok) {
    let body; try { body = await response.json(); } catch { /* Proxy errors may be text. */ }
    throw new Error(typeof body?.detail === 'string' ? body.detail : 'The forecast service is unavailable. Please retry later.');
  }
  return response.json();
}

async function readJSON(url, noCache = false) {
  const response = await fetch(url, { cache: noCache ? 'no-store' : 'default' });
  if (!response.ok) throw new Error('This saved data release is unavailable. Reload the page or try again later.');
  const text = await response.text();
  let value; try { value = JSON.parse(text); } catch { throw new Error('The published snapshot is missing or invalid. Please reload later.'); }
  return { value, text };
}

async function pin() {
  if (!pinning) pinning = (async () => {
    const { value: pointer } = await readJSON(`${ROOT}/latest.json`, true);
    if (pointer.schema !== 1 || !/^[a-f0-9]{64}$/.test(pointer.release_id)) throw new Error('Unsupported data publication.');
    const { value: manifest } = await readJSON(`${ROOT}/releases/${pointer.release_id}/manifest.json`);
    if (manifest.schema !== 1 || manifest.release_id !== pointer.release_id) throw new Error('Mixed data publication. Please reload.');
    pinned = manifest;
    return manifest;
  })().catch(error => { pinning = null; throw error; });
  return pinning;
}

async function file(name) {
  const manifest = await pin();
  if (!manifest.files[name]) throw new Error('This player or dataset is unavailable in the public snapshot.');
  if (!files.has(name)) files.set(name, (async () => {
    const { value, text } = await readJSON(`${ROOT}/releases/${manifest.release_id}/${name}`);
    const bytes = new TextEncoder().encode(text);
    const digest = await crypto.subtle.digest('SHA-256', bytes);
    const hash = [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
    if (hash !== manifest.files[name].sha256) throw new Error('Published data failed its integrity check. Reload before continuing.');
    return value;
  })().catch(error => { files.delete(name); throw error; }));
  return files.get(name);
}

export async function publicationChanged() {
  if (!pinned) return false;
  const { value } = await readJSON(`${ROOT}/latest.json`, true);
  return value.schema === 1 && value.release_id !== pinned.release_id;
}

export function dynamicMetadata(metadata, now = Date.now()) {
  const age = Math.max(0, (now - Date.parse(metadata.as_of)) / 3600000);
  return { ...metadata, age_hours: age, stale: age > metadata.stale_after_hours };
}

export async function staticRequest(raw, signal) {
  if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
  const url = new URL(raw, 'https://snapshot.invalid'), path = url.pathname, q = url.searchParams;
  const manifest = await pin();
  if (path === '/api/catalog') return file('catalog.json');
  const metadata = dynamicMetadata(await file('metadata.json'));
  if (path === '/api/metadata') return { ...metadata, publication_id: manifest.release_id };
  if (path === '/api/standings') return { items: await file('standings.json'), metadata };
  const page = (items, snapshot_at) => {
    const limit = Math.min(200, Math.max(1, Number(q.get('limit') || 50))), offset = Math.max(0, Number(q.get('offset') || 0));
    return { metadata, snapshot_at, total: items.length, limit, offset, items: items.slice(offset, offset + limit) };
  };
  const season = Number(q.get('season') || manifest.season_id), model = q.get('model') || 'prior_shrinkage';
  const team = q.get('team_id'), target = q.get('target'), nominal = q.get('nominal');
  const playerMatch = /^\/api\/predictions\/players\/(\d+)(?:\/(history|intervals))?$/.exec(path);
  const filter = r => r.season_id === season && (!team || r.last_played_team_id === Number(team)) && r.model === model;
  if (path === '/api/predictions/players' || path === '/api/predictions/intervals' || playerMatch) {
    let projections, intervals;
    if (playerMatch) ({ projections, intervals } = await file(`players/${playerMatch[1]}.json`));
    else if (path.endsWith('/intervals')) intervals = await file('intervals.json');
    else projections = await file('players.json');
    const cutoff = q.get('prediction_at') || metadata.snapshots.filter(s => s.season_id === season).at(-1)?.prediction_at;
    const timeEqual = value => Date.parse(value) === Date.parse(cutoff);
    const bands = (intervals || []).filter(r => r.season_id === season && r.model === model && (!q.get('player_id') || r.player_id === Number(q.get('player_id'))) && (!target || r.target === target) && (!nominal || r.nominal === Number(nominal)));
    if (playerMatch?.[2] === 'history') {
      const points = (projections || []).filter(filter).map(projection => ({ projection, interval: bands.find(b => b.prediction_at === projection.prediction_at && b.target === (target || 'points') && b.nominal === Number(nominal || .8)) || null }));
      return page(points);
    }
    if (path.endsWith('/intervals')) return page(bands.filter(r => timeEqual(r.prediction_at) && (!team || r.last_played_team_id === Number(team))), cutoff);
    const items = projections.filter(r => filter(r) && timeEqual(r.prediction_at) && (!q.get('player_id') || r.player_id === Number(q.get('player_id'))));
    return playerMatch ? { metadata, item: items[0] || null } : page(items, cutoff);
  }
  if (path.startsWith('/api/predictions/games')) {
    let items = (await file('fixtures.json')).map(r => ({ ...r, snapshot_status: r.result?.is_final || r.snapshot_status === 'final' ? 'final' : Date.parse(r.start_time_utc) >= Date.now() ? 'scheduled' : 'unresolved' }));
    const id = /^\/api\/predictions\/games\/(\d+)$/.exec(path);
    if (id) return { metadata, item: items.find(r => r.game_id === Number(id[1])) || null };
    const status = q.get('status') || 'upcoming';
    items = items.filter(r => r.season_id === season && (!team || [r.home_team_id, r.away_team_id].includes(Number(team))) &&
      (!q.get('date_from') || r.game_date >= q.get('date_from')) && (!q.get('date_to') || r.game_date <= q.get('date_to')) &&
      (status === 'all' || status === 'final' && r.snapshot_status === 'final' || status === 'upcoming' && r.snapshot_status === 'scheduled'));
    return page(items);
  }
  if (path.startsWith('/api/prospective/')) {
    const data = await file('prospective.json'), health = { status: 'published_snapshot', error: null, checked_at: data.report?.generated_at };
    if (path.endsWith('/performance')) {
      if (!data.report) throw new Error('Prospective evaluation was unavailable when this snapshot was published.');
      return { health, report: data.report };
    }
    const id = /^\/api\/prospective\/games\/(\d+)$/.exec(path);
    if (id) return { health, item: data.items.find(r => r.game_id === Number(id[1])) || null, revisions: data.revisions.filter(r => r.game_id === Number(id[1])) };
    const items = data.items.filter(r => (!q.has('season') || r.season_id === season) && (!team || [r.home_team_id, r.away_team_id].includes(Number(team))) && (q.get('upcoming') !== 'true' || Date.parse(r.scheduled_start) >= Date.now()));
    return { ...page(items), health };
  }
  throw new Error('This feature is available only in the local application.');
}
