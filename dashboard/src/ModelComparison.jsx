import { useEffect, useState } from 'react';
import { fetchJSON, gameTime, queryString, teamFor } from './data.js';
import { TeamLogo } from './TeamIdentity.jsx';
import { matchupColors } from './teamBranding.js';

export const modelName = key => key === 'frozen_elo' ? 'Frozen Elo' : 'Team-context logistic';
export const percent = p => p == null ? '—' : `${(p * 100).toFixed(1)}%`;
export const score = n => n == null ? '—' : n.toFixed(4);
export const label = s => (s || '').replaceAll('_', ' ');

// Reads the shared server cache. Refreshing this component never runs inference.
export function useProspective(path) {
  const [state, setState] = useState({ path, data: null, error: null });
  useEffect(() => {
    const controller = new AbortController();
    let active = true, timer;
    const poll = async () => {
      try {
        const data = await fetchJSON(path, controller.signal);
        if (active) setState({ path, data, error: null });
      } catch (error) {
        if (active && error.name !== 'AbortError') setState(old => ({ path, data: old.path === path ? old.data : null, error: error.message }));
      } finally { if (active) timer = setTimeout(poll, 30000); }
    };
    poll();
    return () => { active = false; clearTimeout(timer); controller.abort(); };
  }, [path]);
  return state.path === path ? state : { data: null, error: null };
}

function HealthNote({ data, error }) {
  const message = error || data?.health?.error;
  return message ? <p role="status" className="live-warning">{message} Last saved forecasts remain frozen.</p> : data?.health?.status === 'published_snapshot' ? <p className="game-note">Published snapshot. Tracking runs locally; new forecasts and results appear after the next publication.</p> : null;
}

export function ForecastPair({ item, catalog }) {
  const home = teamFor(catalog, item.home_team_id), away = teamFor(catalog, item.away_team_id), colors = matchupColors(away, home);
  return <div className="forecast-pair"><p className="forecast-status"><strong>{label(item.status)}</strong>{item.status === 'sealed' && <span>{item.timing === 'early' ? 'Early bootstrap' : 'Standard cutoff window'} · prospective</span>}</p>
    {item.models.map(model => <div className="model-forecast" key={model.model}>
      <div className="model-forecast-title"><strong>{modelName(model.model)}</strong><code title={model.version}>v {model.version.slice(0, 10)}</code></div>
      <div className="pregame-values"><span>{away.abbreviation}<strong>{percent(model.away_win_probability)}</strong></span><span>{home.abbreviation}<strong>{percent(model.home_win_probability)}</strong></span></div>
      <div className="probability-bar team-probabilities" role="img" aria-label={`${modelName(model.model)}: ${away.name} ${percent(model.away_win_probability)}, ${home.name} ${percent(model.home_win_probability)}`}><span style={{ width: percent(model.away_win_probability), background: colors.away }} /><span style={{ width: percent(model.home_win_probability), background: colors.home }} /></div>
      <small>Favourite: {teamFor(catalog, model.favourite_team_id).abbreviation}{model.favourite_won !== null && <> · {model.favourite_won ? 'Won' : 'Lost'} · Brier {score(model.brier)}</>}</small>
    </div>)}
    {!item.models.length && <p className="game-note">{item.reason ? label(item.reason) : 'Both models will be saved in the five minutes before the one-hour cutoff. Missed cutoffs stay missing.'}</p>}
    <dl className="forecast-dates"><div><dt>Cutoff</dt><dd>{gameTime(item.cutoff_at)}</dd></div>{item.generated_at && <div><dt>{item.status === 'sealed' ? 'Sealed' : 'Recorded missing'}</dt><dd>{gameTime(item.generated_at)}</dd></div>}</dl>
    {item.outcome && item.outcome_status === 'validated' && <p>Validated final: {away.abbreviation} {item.outcome.away_score} – {home.abbreviation} {item.outcome.home_score}</p>}
    <p className="game-note">Outcome: {label(item.outcome_status)}. Probabilities include overtime and shootouts and stay frozen during play.</p>
  </div>;
}

export default function GameModelComparison({ gameId, catalog }) {
  const { data, error } = useProspective(`/api/prospective/games/${gameId}`);
  return <aside className="panel game-pregame prospective-game"><span className="eyebrow">LOCKED BEFORE PLAY</span><h2>Model comparison</h2><HealthNote data={data} error={error} />
    {data?.item ? <ForecastPair item={data.item} catalog={catalog} /> : <p className="game-note">{error || data ? 'No prospective pair is available here. Older archived predictions are a separate record.' : 'Checking the prospective ledger…'}</p>}
    {data?.revisions?.some(r => !r.current_fixture) && <details><summary>Earlier schedule revisions</summary>{data.revisions.filter(r => !r.current_fixture).map(r => <p key={r.batch_id}>{gameTime(r.scheduled_start)} · {label(r.status)} · excluded from the current result</p>)}</details>}
    <a className="model-performance-link" href="/models">View model performance →</a>
  </aside>;
}

export function ProspectiveGames({ catalog, season, teamId }) {
  const { data, error } = useProspective(`/api/prospective/games?${queryString({ season, team_id: teamId, upcoming: true, limit: 30 })}`);
  return <section className="panel prospective-directory" aria-label="Prospective model comparisons"><div className="section-heading"><div><span className="eyebrow">THE FORWARD TEST</span><h2>Two models. One fair comparison.</h2><p>Sealed pregame forecasts, tracked against validated final results.</p></div><a href="/models" className="button secondary">Model performance →</a></div>
    <HealthNote data={data} error={error} />
    {data?.items.length ? <div className="table-scroll"><table><caption>Prospective forecasts · both models predict the same games</caption><thead><tr><th>Game · local start</th><th>Frozen Elo<br />Home win</th><th>Logistic<br />Home win</th><th>Forecast status</th></tr></thead><tbody>{data.items.map(item => { const h = teamFor(catalog, item.home_team_id), a = teamFor(catalog, item.away_team_id); return <tr key={item.fixture_id}><td><a className="prospective-matchup" href={`/games/${item.game_id}`}><TeamLogo team={a} season={item.season_id} /><span>{a.abbreviation} @ {h.abbreviation}<small>{gameTime(item.scheduled_start)}</small></span><TeamLogo team={h} season={item.season_id} /></a></td>{['frozen_elo', 'logistic_team_context'].map(name => <td key={name}>{percent(item.models.find(m => m.model === name)?.home_win_probability)}</td>)}<td><span className="forecast-status">{label(item.status)}</span><small>{item.status === 'sealed' ? `${label(item.timing)} · ${gameTime(item.generated_at)}` : item.reason ? label(item.reason) : `Cutoff ${gameTime(item.cutoff_at)}`}</small></td></tr>; })}</tbody></table></div> : <p className="game-note">{error ? 'Prospective tracking is unavailable.' : data ? 'No tracked games match these filters. Historical forecasts are listed separately below.' : 'Loading the forecast ledger…'}</p>}
    {data?.total > 30 && <p className="game-note">Showing the next 30 of {data.total} matching games. Cumulative results include every eligible tracked game.</p>}
  </section>;
}
