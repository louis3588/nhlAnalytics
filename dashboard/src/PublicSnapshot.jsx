import { STATIC } from './transport.js';
import { useResource } from './useResource.js';
import { fetchJSON, gameTime, teamFor } from './data.js';
import { TeamLogo } from './TeamIdentity.jsx';
import { Help } from './SiteIdentity.jsx';

export function PublicBetaNotice({ metadata }) {
  if (!STATIC) return null;
  return <section className="panel public-notice" role="status"><strong>Public beta · saved snapshots</strong><p>Forecast source: {gameTime(metadata.as_of)}. Updates are published from our local pipeline. Live scores and play-by-play are not available in this release. <a href="/models">View model evidence →</a></p></section>;
}

export default function PublicSnapshot({ catalog, teamId }) {
  const data = useResource(STATIC ? 'public-standings' : null, signal => fetchJSON('/api/standings', signal));
  if (!STATIC) return null;
  const rows = (data.data?.items || []).filter(r => !teamId || r.team_id === Number(teamId));
  return <section className="panel prospective-directory standings-panel" aria-labelledby="standings-heading"><div className="section-heading"><div><span className="eyebrow">THE SEASON SO FAR</span><h2 id="standings-heading">Around the league</h2><p>Regular-season standings from the latest published snapshot. Choose a team to explore its player outlooks.</p></div></div>{data.loading ? <p role="status" className="standings-message">Loading team standings…</p> : data.error ? <p role="status" className="standings-message">{data.error}</p> : !rows.length ? <p className="standings-message">Standings were unavailable in this publication.</p> : <div className="table-scroll" tabIndex={0} role="region" aria-label="Team standings, scroll horizontally for all statistics"><table><caption>Standings observed {gameTime(rows[0].observed_at)}</caption><thead><tr><th scope="col">Team</th>{[['GP','Games played'],['W','Wins'],['L','Regulation losses'],['OTL','Overtime or shootout losses'],['Points','Standings points: two for a win, one for an overtime or shootout loss'],['Goal difference','Goals scored minus goals conceded']].map(([label,description]) => <th scope="col" key={label}>{label}<Help label={label}>{description}</Help></th>)}</tr></thead><tbody>{rows.map(r => { const team = teamFor(catalog,r.team_id); return <tr key={r.team_id}><th scope="row"><a className="standings-team" href={`/teams/${r.team_id}`}><TeamLogo team={team} season={r.season_id} /><span>{team.name}</span></a></th>{['games_played', 'wins', 'losses', 'ot_losses', 'points', 'goal_differential'].map(key => <td key={key}>{r[key] ?? '—'}</td>)}</tr>; })}</tbody></table></div>}</section>;
}
