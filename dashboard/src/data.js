export const TARGETS = {
  points: { label: 'Points', unit: 'pts', singular: 'points' },
  goals: { label: 'Goals', unit: 'goals', singular: 'goals' },
  assists: { label: 'Assists', unit: 'assists', singular: 'assists' },
  games: { label: 'Games', unit: 'GP', singular: 'games played' },
};

export function queryString(params) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) if (value !== '' && value !== null && value !== undefined) query.set(key, String(value));
  return query.toString();
}

export async function fetchJSON(url, signal) {
  let response;
  try { response = await fetch(url, { signal }); }
  catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new Error('The forecast API could not be reached. Start the Python API on port 8000, then retry.');
  }
  if (!response.ok) {
    let body;
    try { body = await response.json(); } catch { /* A proxy may return a plain-text 502. */ }
    const detail = typeof body?.detail === 'string' ? body.detail : null;
    if (response.status >= 500) throw new Error('The forecast API is unavailable. Check that it is running on port 8000, then retry.');
    throw new Error(detail || `The API returned HTTP ${response.status}. Try another filter or refresh the snapshot.`);
  }
  return response.json();
}

export function checkBundle(result, expected) {
  if (expected && result.metadata?.bundle_sha256 !== expected) throw new Error('The saved snapshot changed while loading. Refresh to load the new snapshot together.');
  return result;
}

export async function fetchAllPages(path, params, signal, expectedBundle) {
  const first = checkBundle(await fetchJSON(`${path}?${queryString({ ...params, limit: 200, offset: 0 })}`, signal), expectedBundle);
  const items = [...first.items];
  for (let offset = 200; offset < first.total; offset += 200) {
    const page = checkBundle(await fetchJSON(`${path}?${queryString({ ...params, limit: 200, offset })}`, signal), first.metadata.bundle_sha256);
    if (page.total !== first.total || page.snapshot_at !== first.snapshot_at || !page.items.length) throw new Error('The saved forecast pages are inconsistent. Refresh and try again.');
    items.push(...page.items);
  }
  if (items.length !== first.total) throw new Error('Some saved forecasts are missing. Refresh and try again.');
  return { ...first, items };
}

export function nameFor(catalog, id) { return catalog?.players?.[id]?.name || `NHL player #${id}`; }
export function teamFor(catalog, id) { return catalog?.teams?.[id] || { id, name: `Team ${id}`, abbreviation: String(id) }; }
export function seasonLabel(value) { return `${String(value).slice(0, 4)}–${String(value).slice(6)}`; }
export function initials(name) { return name.split(/\s+/).filter(Boolean).map(p => p[0]).slice(0, 2).join(''); }
export function number(value, digits = 1) { return new Intl.NumberFormat('en-GB', { maximumFractionDigits: digits, minimumFractionDigits: digits }).format(value); }
export function median(values) {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}
export function chartMaximum(rows, bands, target) {
  const values = rows.flatMap(row => {
    const band = bands.get(row.player_id);
    return [row.observed[target], row.projected[target], ...(band?.available ? [band.upper] : [])];
  });
  const max = Math.max(1, ...values);
  const step = max > 100 ? 20 : max > 40 ? 10 : 5;
  return Math.ceil(max / step) * step;
}
export function snapshotDate(iso) {
  return new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' }).format(new Date(iso));
}
export function snapshotTime(iso) {
  return new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', timeZone: 'UTC' }).format(new Date(iso));
}
export function gameTime(iso) {
  return new Intl.DateTimeFormat('en-US', { weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZone: 'America/New_York' }).format(new Date(iso));
}
