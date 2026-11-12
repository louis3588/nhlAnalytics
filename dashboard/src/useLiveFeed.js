import { useEffect, useState } from 'react';
import { STATIC } from './transport.js';

// Each viewer subscribes to our shared cache, never polling GameCenter directly.
export function useLiveFeed(path) {
  const [revision, setRevision] = useState(0);
  const [state, setState] = useState({ data: null, error: null, connected: false });
  useEffect(() => {
    if (STATIC) { setState({ data: null, error: null, connected: false }); return; }
    let active = true, streamed = false;
    const controller = new AbortController();
    setState({ data: null, error: null, connected: false });
    fetch(path, { signal: controller.signal }).then(async response => {
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || 'Live service unavailable');
      if (active && !streamed) setState(s => ({ ...s, data: body }));
    }).catch(error => {
      if (active && error.name !== 'AbortError') setState(s => ({ ...s, error: error.message }));
    });
    const source = new EventSource(path === '/api/live/games' ? '/api/live/stream' : `${path}/stream`);
    source.addEventListener('game', event => {
      try {
        const data = JSON.parse(event.data);
        if (active) { streamed = true; setState({ data, error: null, connected: true }); }
      } catch { if (active) setState(s => ({ ...s, error: 'An unreadable live update was received.' })); }
    });
    source.onerror = () => { if (active) setState(s => ({ ...s, connected: false, error: s.error || 'Live connection interrupted. Reconnecting automatically…' })); };
    return () => { active = false; controller.abort(); source.close(); };
  }, [path, revision]);
  return { ...state, retry: () => setRevision(r => r + 1) };
}

export function gameStatus(game) {
  if (game.schedule_state === 'PPD') return 'Postponed';
  if (game.schedule_state === 'SUSP') return 'Suspended';
  return ({ LIVE: 'Live', CRIT: 'Live', PRE: 'Pregame', FINAL: 'Final', OFF: 'Final', FUT: 'Scheduled' })[game.game_state] || 'Awaiting update';
}

export function goalieSaves(stats) {
  if (stats.saves !== null && stats.saves !== undefined) return stats.saves;
  const match = /^(\d+)\/(\d+)$/.exec(stats.saveShotsAgainst || '');
  return match ? Number(match[1]) : null;
}

export function teamMetric(players, metric) {
  const skaters = players.filter(p => !p.goalie);
  return skaters.length && skaters.every(p => Number.isFinite(p.stats[metric])) ? skaters.reduce((sum, p) => sum + p.stats[metric], 0) : null;
}
