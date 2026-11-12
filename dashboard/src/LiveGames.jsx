import { useLiveFeed, gameStatus } from './useLiveFeed.js';
import { TeamLogo } from './TeamIdentity.jsx';
import { gameTime, teamFor } from './data.js';
import { STATIC } from './transport.js';

export default function LiveGames({ catalog, teamId }) {
  const { data, error, connected, retry } = useLiveFeed('/api/live/games');
  if (STATIC) return null;
  const games = (data?.items || []).filter(g => !teamId || g.home.id === Number(teamId) || g.away.id === Number(teamId));
  return <section id="live-games" className="live-games-section" aria-labelledby="live-games-title">
    <div className="section-heading"><div><span className="eyebrow">AROUND THE RINK</span><h2 id="live-games-title">Live &amp; recent games</h2><p>Today and yesterday on the NHL calendar. Open a game for stats and play-by-play.</p></div><span className={`live-connection ${connected && !data?.collector?.error ? 'healthy' : ''}`}>{data?.collector?.error ? 'Feed delayed' : connected ? 'Updates connected' : 'Connecting…'}</span></div>
    {(error || data?.collector?.error) && <p className="live-warning" role="status">{data?.collector?.error || error} <button className="text-button" onClick={retry}>Reconnect</button></p>}
    {!data ? <p className="muted">Connecting to the shared game feed…</p> : !games.length ? <p className="muted">No games in this view yet.</p> : <div className="live-directory">{games.map(game => <a className="panel live-game-tile" key={game.game_id} href={`/games/${game.game_id}`} aria-label={`Open ${game.away.abbreviation} at ${game.home.abbreviation} game`}>
      <div><span className={`live-state ${['LIVE', 'CRIT'].includes(game.game_state) ? 'is-live' : ''}`}>{gameStatus(game)}</span><time dateTime={game.start_time_utc}>{gameTime(game.start_time_utc)}</time></div>
      {['away', 'home'].map(side => <span className="live-tile-team" key={side}><TeamLogo team={{ ...teamFor(catalog, game[side].id), ...game[side] }} season={game.season_id} /><strong>{game[side].abbreviation}</strong><b>{game[side].score ?? '—'}</b></span>)}<small>Game center <span>→</span></small>
    </a>)}</div>}
  </section>;
}
