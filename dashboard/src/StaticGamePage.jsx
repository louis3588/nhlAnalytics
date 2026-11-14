import { Brand, Help } from './SiteIdentity.jsx';
import { useResource } from './useResource.js';
import { fetchJSON, gameTime, teamFor } from './data.js';
import { TeamLogo } from './TeamIdentity.jsx';
import GameModelComparison from './ModelComparison.jsx';
import { ErrorNote, Loading } from './components.jsx';

export default function StaticGamePage({ gameId }) {
  const resource = useResource(`public-game:${gameId}`, signal => Promise.all([fetchJSON('/api/catalog', signal), fetchJSON(`/api/predictions/games/${gameId}`, signal)]));
  const [catalog, saved] = resource.data || [], game = saved?.item;
  const home = game && teamFor(catalog, game.home_team_id), away = game && teamFor(catalog, game.away_team_id);
  return <div className="player-workspace"><header className="player-nav"><Brand /><nav aria-label="Game navigation"><a href="/">Forecasts</a><a href="/models">Model performance</a></nav></header><main id="main" tabIndex={-1} className="player-content">
    <section className="panel public-notice"><h1>Saved game outlook</h1><p>Public beta · read-only snapshot. Live scores, player game totals and play-by-play are available only in the local GameCenter.</p></section>
    {resource.error && <ErrorNote message={resource.error} onRetry={() => window.location.reload()} />}{!resource.data && !resource.error && <Loading />}
    {resource.data && !game && <p className="panel">This game is not included in the published season.</p>}
    {game && <><section className="panel game-scoreboard"><div className="scoreboard-main"><div className="scoreboard-team"><TeamLogo team={away} season={game.season_id} /><h2>{away.name}</h2></div><div className="scoreboard-center"><strong>{game.result?.is_final ? `${game.result.away_score} – ${game.result.home_score}` : 'at'}</strong><p>{gameTime(game.start_time_utc)}</p><small>{game.result?.is_final ? 'Saved final result' : 'Schedule as published; no live updates'}</small></div><div className="scoreboard-team"><TeamLogo team={home} season={game.season_id} /><h2>{home.name}</h2></div></div></section>
      <GameModelComparison gameId={gameId} catalog={catalog} />
      <section className="panel"><h2>Archived production Elo</h2><p>{away.abbreviation}: {(game.away_win_probability * 100).toFixed(1)}% · {home.abbreviation}: {(game.home_win_probability * 100).toFixed(1)}%</p><p>Generated {gameTime(game.prediction_at)}. Reconstructed training history; separate from sealed prospective forecasts.</p></section></>}
  </main></div>;
}
