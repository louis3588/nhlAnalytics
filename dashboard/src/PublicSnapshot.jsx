import { STATIC } from './transport.js';
import { useResource } from './useResource.js';
import { fetchJSON, gameTime, teamFor } from './data.js';

export function PublicBetaNotice({ metadata }) {
  if (!STATIC) return null;
  return <section className="panel public-notice" role="status"><strong>Public beta · saved snapshots</strong><p>Forecast source: {gameTime(metadata.as_of)}. Updates are published from our local pipeline. Live scores and play-by-play are not available in this release. <a href="/models">View model evidence →</a></p></section>;
}

export default function PublicSnapshot({ catalog, teamId }) {
  const data = useResource(STATIC ? 'public-standings' : null, signal => fetchJSON('/api/standings', signal));
  if (!STATIC) return null;
  const rows = (data.data?.items || []).filter(r => !teamId || r.team_id === Number(teamId));
  return <section className="panel prospective-directory"><h2>Saved team standings</h2>{data.error ? <p role="status">{data.error}</p> : !rows.length ? <p>Standings were unavailable in this publication.</p> : <div className="table-scroll"><table><caption>Latest exported standings · observed {gameTime(rows[0].observed_at)}</caption><thead><tr><th>Team</th><th>GP</th><th>W</th><th>L</th><th>OTL</th><th>Points</th><th>Goal difference</th></tr></thead><tbody>{rows.map(r => <tr key={r.team_id}><td><a href={`/teams/${r.team_id}`}>{teamFor(catalog, r.team_id).name}</a></td>{['games_played', 'wins', 'losses', 'ot_losses', 'points', 'goal_differential'].map(key => <td key={key}>{r[key]}</td>)}</tr>)}</tbody></table></div>}</section>;
}
