import { Brand, Help } from './SiteIdentity.jsx';
import { useEffect, useState } from 'react';
import { useResource } from './useResource.js';
import { useBundleUpdates } from './useBundleUpdates.js';
import { checkBundle, fetchAllPages, fetchJSON, nameFor, number, queryString, seasonLabel, snapshotDate, TARGETS, teamFor } from './data.js';
import { PlayerHeadshot, TeamLogo } from './TeamIdentity.jsx';
import { playerPageURL, teamPageURL, themeStyle } from './teamBranding.js';
import { ErrorNote, Loading } from './components.jsx';

function HistoryChart({ items, target, nominal, selected, onSelect }) {
  const w = 900, h = 330, left = 65, right = 45, top = 35, bottom = 65;
  const times = items.map(i => Date.parse(i.projection.prediction_at));
  const max = Math.ceil(Math.max(1, ...items.flatMap(i => [i.projection.projected[target], i.projection.observed[target], i.interval?.available ? i.interval.upper : 0])) / 5) * 5;
  const x = i => times.length === 1 ? w / 2 : left + (times[i] - times[0]) / (times.at(-1) - times[0]) * (w - left - right);
  const y = value => h - bottom - value / max * (h - top - bottom);
  const line = field => items.map((item, i) => `${x(i)},${y(item.projection[field][target])}`).join(' ');
  const labelled = new Set(Array.from({ length: Math.min(items.length, 6) }, (_, i) => Math.round(i * (items.length - 1) / Math.max(1, Math.min(items.length, 6) - 1))));
  const dateButtons = items.length <= 14 ? items : items.filter((item, i) => i >= items.length - 7 || item.projection.prediction_at === selected);
  return <figure className="history-chart" aria-labelledby="history-title">
    <div className="chart-legend"><span><i className="key key-projected" />Projected total</span><span><i className="key key-observed" />Recorded total</span><span><i className="key key-interval" />{nominal * 100}% interval</span></div>
    <div className="history-scroll" tabIndex={0} role="region" aria-label="Projection graph; scroll horizontally on small screens"><svg viewBox={`0 0 ${w} ${h}`} role="img" aria-label={`${TARGETS[target].label} projection history: ${items.length} saved snapshots. Exact values in the table below.`}>
      <text x={left} y="17" className="history-axis-title">Season {TARGETS[target].singular}</text>
      {[0, 1, 2, 3, 4].map(t => <g key={t}><line x1={left} x2={w - right} y1={y(max * t / 4)} y2={y(max * t / 4)} className="history-grid" /><text x={left - 12} y={y(max * t / 4) + 4} textAnchor="end">{number(max * t / 4, 0)}</text></g>)}
      {items.slice(1).map((item, i) => item.interval?.available && items[i].interval?.available && <polygon key={i} className="history-band" points={`${x(i)},${y(items[i].interval.upper)} ${x(i + 1)},${y(item.interval.upper)} ${x(i + 1)},${y(item.interval.lower)} ${x(i)},${y(items[i].interval.lower)}`} />)}
      <polyline points={line('observed')} className="history-recorded" /><polyline points={line('projected')} className="history-projected" />
      {items.map((item, i) => <g key={item.projection.prediction_at}>
        <circle cx={x(i)} cy={y(item.projection.observed[target])} r="4" fill="#738774" />
        {item.interval?.available && <line x1={x(i)} x2={x(i)} y1={y(item.interval.lower)} y2={y(item.interval.upper)} className="history-whisker" />}
        <circle cx={x(i)} cy={y(item.projection.projected[target])} r={selected === item.projection.prediction_at ? 7 : 5} className="history-dot" />
        {(labelled.has(i) || selected === item.projection.prediction_at) && <text x={x(i)} y={y(item.projection.projected[target]) - 14} textAnchor="middle" className="history-value">{number(item.projection.projected[target])}</text>}
        {labelled.has(i) && <text x={x(i)} y={h - bottom + 24} textAnchor="middle">{new Intl.DateTimeFormat('en-GB', { month: 'short', day: 'numeric', timeZone: 'UTC' }).format(new Date(item.projection.prediction_at))}</text>}
      </g>)}
      <text x={w / 2} y={h - 8} textAnchor="middle" className="history-axis-title">Saved prediction date (UTC)</text>
    </svg></div>
    <figcaption>{items.length === 1 ? 'One saved snapshot so far. A trend will appear when more forecasts are saved.' : 'Lines connect saved snapshots; no forecasts are inferred between them.'} Select a date below to inspect its recorded totals and outlook.</figcaption>
    {items.length > 14 && <label className="history-picker">All saved dates <select aria-label="Saved prediction date" value={selected} onChange={e => onSelect(e.target.value)}>{items.map(item => <option key={item.projection.prediction_at} value={item.projection.prediction_at}>{snapshotDate(item.projection.prediction_at)}</option>)}</select></label>}
    <div className="history-dates" aria-label="Select saved prediction date">{dateButtons.map(item => <button key={item.projection.prediction_at} aria-pressed={selected === item.projection.prediction_at} onClick={() => onSelect(item.projection.prediction_at)}>{snapshotDate(item.projection.prediction_at)}</button>)}</div>
    <details><summary>View labelled graph data</summary><div className="table-scroll"><table><caption>{TARGETS[target].label} by saved prediction date</caption><thead><tr><th>Date (UTC)</th><th>Recorded</th><th>Projected total</th><th>{nominal * 100}% prediction interval</th></tr></thead><tbody>{items.map(item => <tr key={item.projection.prediction_at}><td>{snapshotDate(item.projection.prediction_at)}</td><td>{item.projection.observed[target]}</td><td>{number(item.projection.projected[target])}</td><td>{item.interval?.available ? `${item.interval.lower}–${item.interval.upper}` : 'Unavailable'}</td></tr>)}</tbody></table></div></details>
  </figure>;
}

export default function PlayerPage({ playerId }) {
  const query = new URLSearchParams(window.location.search);
  const [season, setSeason] = useState(query.get('season') || '');
  const [model, setModel] = useState(query.get('model') === 'pace' ? 'pace' : 'prior_shrinkage');
  const [target, setTarget] = useState(Object.hasOwn(TARGETS, query.get('target')) ? query.get('target') : 'points');
  const [nominal, setNominal] = useState(query.get('nominal') === '0.95' ? 0.95 : 0.8);
  const [date, setDate] = useState(null);
  const [revision, setRevision] = useState(0);
  const retry = () => setRevision(r => r + 1);
  const bootstrap = useResource(`player-bootstrap:${revision}`, signal => Promise.all([fetchJSON('/api/metadata', signal), fetchJSON('/api/catalog', signal)]));
  const [metadata, catalog] = bootstrap.data || [];
  useBundleUpdates(metadata?.bundle_sha256, retry);
  const activeSeason = season || (metadata ? Math.max(...metadata.seasons) : '');
  useEffect(() => {
    if (activeSeason) window.history.replaceState(null, '', playerPageURL(playerId, activeSeason, { model, target, nominal }));
  }, [playerId, activeSeason, model, target, nominal]);
  const key = metadata ? `${revision}:${playerId}:${activeSeason}:${model}:${target}:${nominal}:${metadata.bundle_sha256}` : null;
  const history = useResource(key, signal => fetchAllPages(`/api/predictions/players/${playerId}/history`, { season: activeSeason, model, target, nominal }, signal, metadata.bundle_sha256));
  const items = history.data?.items || [];
  const chosen = items.find(i => i.projection.prediction_at === date) || items.at(-1);
  const player = chosen?.projection;
  const bands = useResource(player ? `${key}:${player.prediction_at}` : null, signal => fetchJSON(`/api/predictions/players/${playerId}/intervals?${queryString({ season: activeSeason, model, prediction_at: player.prediction_at, nominal })}`, signal).then(result => checkBundle(result, metadata.bundle_sha256)));
  const team = player ? teamFor(catalog, player.last_played_team_id) : null;
  const name = nameFor(catalog, playerId);
  function change(setter, value) { setter(value); setDate(null); }
  return <div className="player-workspace" style={team ? themeStyle(team) : {}}>
    <a className="skip-link" href="#player-content">Skip to player forecast</a>
    <header className="player-nav"><Brand href={`/?season=${activeSeason}`} /><nav aria-label="Player navigation"><a href={`/?season=${activeSeason}`}>All projections</a><a href={`/teams?season=${activeSeason}`}>Teams</a>{team && <a href={teamPageURL(team.id, activeSeason)}>{team.name}</a>}</nav></header>
    <main id="player-content" className="player-content">
      {bootstrap.error && <ErrorNote message={bootstrap.error} onRetry={retry} />}
      {bootstrap.loading && <Loading />}
      {metadata && <><div className="filter-bar player-filters">
        <label>Season<select aria-label="Season" value={activeSeason} onChange={e => change(setSeason, e.target.value)}>{[...metadata.seasons].reverse().map(s => <option key={s} value={s}>{seasonLabel(s)}</option>)}</select></label>
        <label>Projection model<select aria-label="Projection model" value={model} onChange={e => change(setModel, e.target.value)}><option value="prior_shrinkage">Prior-season blended</option><option value="pace">Current pace</option></select></label>
        <label>Graph statistic<select aria-label="Graph statistic" value={target} onChange={e => change(setTarget, e.target.value)}>{Object.entries(TARGETS).map(([key, value]) => <option key={key} value={key}>{value.label}</option>)}</select></label>
        <label>Prediction interval<select aria-label="Prediction interval" value={nominal} onChange={e => change(setNominal, Number(e.target.value))}><option value={0.8}>80%</option><option value={0.95}>95%</option></select></label>
      </div><p className="player-provenance">{metadata.data_mode === 'reconstructed_latest_corrected' ? 'Saved reconstructed forecasts' : 'Daily saved forecasts with reconstructed history'} · source snapshot {snapshotDate(metadata.as_of)} · not a live statistics feed</p></>}
      {history.loading && <Loading text="Reading saved player history…" />}
      {history.error && <ErrorNote message={history.error} onRetry={retry} />}
      {player && <>
        <section className="player-hero"><div className="player-portrait"><PlayerHeadshot name={name} player={player} team={team} catalog={catalog} /></div><div><span className="eyebrow">{seasonLabel(player.season_id)} · NHL #{playerId}</span><h1>{name}</h1><a className="player-team" href={teamPageURL(team.id, activeSeason)}><TeamLogo team={team} season={activeSeason} />{team.name} →</a><p>Recorded totals and forecast as of <strong>{snapshotDate(player.prediction_at)}</strong> (UTC).</p><small>Last played team; not a guarantee of current roster membership.</small></div></section>
        <section className="player-stat-grid" aria-label="Recorded stats, projections and prediction intervals" aria-busy={bands.loading}>{Object.entries(TARGETS).map(([key, value]) => {
          const band = bands.data?.items.find(b => b.target === key);
          return <article className="panel player-stat" key={key}><h2>{value.label}</h2><div className="player-stat-values"><span><small>Recorded</small><strong>{player.observed[key]}</strong></span><span><small>Projected total</small><strong>{number(player.projected[key])}</strong></span></div><div className="player-stat-band"><span>{nominal * 100}% prediction interval</span><strong>{band?.available ? `${band.lower}–${band.upper}` : bands.loading ? 'Loading…' : bands.error ? 'Load failed' : 'Unavailable'}</strong></div></article>;
        })}</section>
        {bands.error && <ErrorNote message={bands.error} onRetry={retry} />}
        <section className="panel player-history"><div className="section-heading"><div><span className="eyebrow">THE SEASON IN PERSPECTIVE</span><h2 id="history-title">{TARGETS[target].label} projection over time</h2><p>{items.length} saved {items.length === 1 ? 'snapshot' : 'snapshots'} · {model === 'pace' ? 'Current pace' : 'Prior-season blended'} · {nominal * 100}% marginal prediction intervals</p></div></div><HistoryChart items={items} target={target} nominal={nominal} selected={player.prediction_at} onSelect={setDate} /></section>
        <aside className="panel player-method"><h2>Reading this forecast</h2><p>Recorded stats are what the model knew at the selected date. Projections estimate the final regular-season total. This player had {player.team_games_remaining} team games remaining and {player.prior_games || 0} games in the prior-season record.</p><p>Prediction intervals use earlier historical errors. They are not confidence intervals or guarantees; each statistic has its own marginal range, so their endpoints should not be added together. Unavailable ranges mean insufficient earlier calibration history.</p><p>{player.data_mode === 'observed_daily' ? 'This daily player snapshot uses sources actually observed before its prediction timestamp. Calibration still uses frozen reconstructed historical errors.' : 'This historical player snapshot is reconstructed using latest corrected sources and assumed next-day availability.'} Strict point-in-time training exports remain separate.</p></aside>
      </>}
    </main>
  </div>;
}
