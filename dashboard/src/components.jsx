import { browserTimeZone, chartMaximum, gameTime, nameFor, number, TARGETS, teamFor } from './data.js';
import Icon from './Icon.jsx';
import { PlayerHeadshot, TeamLogo } from './TeamIdentity.jsx';
import { matchupColors, playerPageURL, teamPageURL } from './teamBranding.js';

export function Avatar({ name, player, team, catalog, small = false }) {
  return <PlayerHeadshot name={name} player={player} team={team} catalog={catalog} small={small} />;
}

export function Loading({ text = 'Loading saved forecasts…' }) {
  return <div className="loading-state" role="status"><span className="loader" />{text}</div>;
}

export function Empty({ title, children, action }) {
  return <div className="empty-state"><span className="empty-icon"><Icon name="rink" size={28} /></span><h3>{title}</h3><p>{children}</p>{action}</div>;
}

export function ErrorNote({ message, onRetry }) {
  return <div className="error-note" role="alert"><Icon name="info" /><span>{message}</span><button className="text-button" onClick={onRetry}>Retry <Icon name="refresh" size={14} /></button></div>;
}

export function ProjectionChart({ rows, selectedId, onSelect, catalog, bands, target, nominal, intervalsLoading }) {
  const domain = chartMaximum(rows, bands, target);
  const targetInfo = TARGETS[target];
  const percent = value => `${Math.max(0, Math.min(100, value / domain * 100))}%`;
  return <figure className="projection-chart" aria-label={`Player season ${targetInfo.label.toLowerCase()} projections and nominal ${nominal * 100}% prediction intervals`}>
    <div className="chart-legend"><span><i className="key key-observed" />Recorded</span><span><i className="key key-projected" />Projected total</span><span><i className="key key-interval" />{nominal * 100}% interval</span></div>
    <div className="chart-axis" aria-hidden="true"><span>PLAYER</span><div>{[0, 1, 2, 3, 4].map(i => <span key={i}>{number(domain * i / 4, 0)}</span>)}</div><span>{targetInfo.unit.toUpperCase()}</span></div>
    <div className="chart-rows">
      {rows.map(row => {
        const name = nameFor(catalog, row.player_id);
        const team = teamFor(catalog, row.last_played_team_id);
        const band = bands.get(row.player_id);
        const selected = selectedId === row.player_id;
        const range = band?.available ? `${band.lower}–${band.upper}` : intervalsLoading ? 'Loading range' : 'Range unavailable';
        return <a key={row.player_id} className={`chart-row${selected ? ' selected' : ''}`} href={playerPageURL(row.player_id, row.season_id, { model: row.model, target, nominal })}
          aria-label={`View ${name}: ${number(row.projected[target])} projected ${targetInfo.singular}, ${range}`}>
          <span className="chart-player"><span title={name}>{name}</span><small>{team.abbreviation}</small></span>
          <span className="chart-track" aria-hidden="true">
            <span className="chart-grid"><i /><i /><i /><i /><i /></span>
            <span className="projected-bar" style={{ width: percent(row.projected[target]) }} />
            <span className="observed-bar" style={{ width: percent(row.observed[target]) }} />
            {band?.available && <span className="interval-line" style={{ left: percent(band.lower), width: percent(band.upper - band.lower) }}><i /><i /></span>}
            <span className="forecast-dot" style={{ left: percent(row.projected[target]) }} />
          </span>
          <span className="chart-value"><strong>{number(row.projected[target])}</strong><small>{band?.available ? range : '—'}</small></span>
          <span className="chart-tooltip" role="tooltip"><strong>{name}</strong><span>{row.observed[target]} recorded · {number(row.projected[target])} projected {targetInfo.unit}</span><span>{nominal * 100}% interval: {range}</span></span>
        </a>;
      })}
    </div>
    <figcaption><span>Season {targetInfo.singular} →</span><span>Ranges are marginal, not guarantees.</span></figcaption>
  </figure>;
}

export function PlayerFocus({ player, catalog, band, target, nominal, loading, error }) {
  if (!player) return <aside className="panel focus-panel" aria-busy={loading}><Empty title="No player selected">Choose a player to see their forecast.</Empty></aside>;
  const name = nameFor(catalog, player.player_id);
  const team = teamFor(catalog, player.last_played_team_id);
  const gain = player.projected[target] - player.observed[target];
  return <aside className="panel focus-panel" aria-label="Selected player forecast" aria-busy={loading} data-testid="player-focus">
    <div className="focus-topline"><span className="eyebrow">IN FOCUS</span><span className="team-pill">{team.abbreviation}</span></div>
    <Avatar name={name} player={player} team={team} catalog={catalog} /><h3><a href={playerPageURL(player.player_id, player.season_id, { model: player.model, target, nominal })}>{name} ↗</a></h3><p className="muted focus-team"><a href={teamPageURL(team.id, player.season_id)}>{team.name} <Icon name="arrow" size={13} /></a></p>
    <div className="focus-total"><strong>{number(player.projected[target])}</strong><span>projected {TARGETS[target].singular}</span></div>
    <div className="gain-note"><Icon name="up" size={15} /> {number(gain)} more than recorded</div>
    <div className="band-card"><div><span className="band-symbol" aria-hidden="true">↔</span><span>{nominal * 100}% prediction interval</span></div>
      <strong>{band?.available ? `${band.lower}–${band.upper}` : loading ? 'Loading…' : error ? 'Couldn’t load interval' : 'Not available'}</strong>
      <small>{band?.available ? `Season ${TARGETS[target].singular} · historical residual range` : loading ? 'Reading saved intervals' : error ? 'Retry to read the saved interval' : 'Not enough earlier calibration history'}</small>
    </div>
    <dl className="focus-facts"><div><dt>Recorded {TARGETS[target].unit}</dt><dd>{player.observed[target]}</dd></div><div><dt>Games played</dt><dd>{player.observed.games}</dd></div><div><dt>Team games remaining</dt><dd>{player.team_games_remaining}</dd></div><div><dt>Projected games</dt><dd>{number(player.projected.games)}</dd></div></dl>
    <span className="focus-footnote"><Icon name="info" size={14} /> Last played team, not a roster guarantee.</span>
  </aside>;
}

export function PlayerTable({ rows, catalog, bands, target, nominal, selectedId, onSelect, page, onPage, sort, onSort, search, onSearch, loading, intervalsLoading, intervalsError }) {
  const pageSize = 10;
  const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
  const safePage = Math.min(page, pageCount - 1);
  const visible = rows.slice(safePage * pageSize, (safePage + 1) * pageSize);
  function heading(key, label) {
    const current = sort.key === key;
    return <th scope="col" aria-sort={current ? (sort.direction === 'desc' ? 'descending' : 'ascending') : 'none'}><button onClick={() => onSort(key)}>{label}<Icon name={current && sort.direction === 'asc' ? 'up' : 'down'} size={13} /></button></th>;
  }
  return <section className="panel table-panel" id="players" aria-labelledby="table-heading" aria-busy={loading || intervalsLoading}>
    <div className="section-heading table-heading"><div><span className="eyebrow">THE FULL PICTURE</span><h2 id="table-heading">Player projections <span className="count-badge">{rows.length}</span></h2></div>
      <label className="search-box"><Icon name="search" size={18} /><input type="search" placeholder="Find a player…" value={search} onChange={e => onSearch(e.target.value)} aria-label="Search players" />{search && <button className="clear-search" onClick={() => onSearch('')} aria-label="Clear player search">×</button>}</label></div>
    {loading ? <Loading /> : !rows.length ? <Empty title="No players match this view">Try another team or clear your search.</Empty> : <>
      <div className="table-scroll"><table><caption className="sr-only">Saved player season forecasts. Select a player's name to inspect their forecast and interval.</caption><thead><tr>{heading('name', 'Player')}{heading('games', 'Games')}{heading('goals', 'Goals')}{heading('assists', 'Assists')}{heading('points', 'Points')}<th scope="col">{nominal * 100}% {TARGETS[target].unit} interval</th><th scope="col"><span className="sr-only">Inspect</span></th></tr></thead>
        <tbody>{visible.map(row => {
          const name = nameFor(catalog, row.player_id);
          const team = teamFor(catalog, row.last_played_team_id);
          const band = bands.get(row.player_id);
          return <tr key={row.player_id} data-selected={row.player_id === selectedId}>
            <td><a className="table-player" href={playerPageURL(row.player_id, row.season_id, { model: row.model, target, nominal })} aria-label={`View ${name} player page`}><Avatar name={name} player={row} team={team} catalog={catalog} small /><span><strong>{name}</strong><small>{team.abbreviation} <span>· NHL #{row.player_id}</span></small></span></a></td>
            {['games', 'goals', 'assists', 'points'].map(t => <td key={t} className={t === target ? 'highlight-number' : ''}>{number(row.projected[t])}</td>)}
            <td><span className={band?.available ? 'table-range' : 'muted'}>{band?.available ? `${band.lower}–${band.upper}` : intervalsLoading ? 'Loading…' : intervalsError ? 'Load failed' : 'Unavailable'}</span></td>
            <td><button className="icon-button row-action" aria-label={`View ${name} details`} onClick={() => onSelect(row.player_id)}><Icon name="arrow" size={17} /></button></td>
          </tr>;
        })}</tbody></table></div>
      <div className="pagination"><span>{safePage * pageSize + 1}–{Math.min((safePage + 1) * pageSize, rows.length)} of {rows.length} players</span><div><button onClick={() => onPage(safePage - 1)} disabled={safePage === 0} aria-label="Previous player page">Previous</button><span>{safePage + 1} / {pageCount}</span><button onClick={() => onPage(safePage + 1)} disabled={safePage + 1 >= pageCount} aria-label="Next player page">Next <Icon name="arrow" size={14} /></button></div></div>
    </>}
  </section>;
}

export function GameGrid({ data, loading, error, catalog, page, onPage, status, onStatus, onRetry }) {
  return <section id="games" className="games-section" aria-labelledby="games-heading" aria-busy={loading}><div className="section-heading"><div><span className="eyebrow">THE NEXT FACEOFF</span><h2 id="games-heading">{status === 'upcoming' ? 'Upcoming games' : 'Saved game forecasts'} <span className="count-badge">{data?.total ?? '—'}</span></h2><p>Archived production Elo / reconstructed history. Win probabilities include overtime and shootouts. Times in your timezone: {browserTimeZone()}.</p></div>
    <div className="segmented" aria-label="Game view">{[['upcoming', 'Upcoming'], ['all', 'All saved']].map(([value, label]) => <button key={value} aria-pressed={status === value} onClick={() => onStatus(value)}>{label}</button>)}</div></div>
    {error ? <ErrorNote message={error} onRetry={onRetry} /> : loading ? <div className="panel"><Loading text="Loading game forecasts…" /></div> : !data?.items.length ? <div className="panel"><Empty title="No upcoming games in this view">This season may be complete, or no saved fixtures match the team filter.<br />You can still explore its historical game forecasts.<br /><button className="button secondary empty-action" onClick={() => onStatus('all')}>View all saved games <Icon name="arrow" size={16} /></button></Empty></div> : <>
      <div className="game-grid">{data.items.map(game => {
        const away = teamFor(catalog, game.away_team_id);const home = teamFor(catalog, game.home_team_id);
        const colors = matchupColors(away, home);
        const a = game.away_win_probability * 100;const h = game.home_win_probability * 100;
        return <article className="game-card panel" key={game.game_id}><div className="game-card-top"><span><Icon name="clock" size={14} /><time dateTime={game.start_time_utc}>{gameTime(game.start_time_utc)}</time></span><span className={`game-status ${game.snapshot_status}`}>{game.snapshot_status === 'scheduled' ? 'Scheduled' : game.snapshot_status === 'final' ? 'Completed' : 'Unresolved'}</span></div>
          <div className="matchup"><div><a className="game-team-link" href={teamPageURL(away.id, game.season_id)} aria-label={`Explore ${away.name} projections`}><TeamLogo team={away} season={game.season_id} /><strong>{away.name}</strong></a><small>Away</small></div><span className="matchup-at">@</span><div><a className="game-team-link" href={teamPageURL(home.id, game.season_id)} aria-label={`Explore ${home.name} projections`}><TeamLogo team={home} season={game.season_id} /><strong>{home.name}</strong></a><small>Home</small></div></div>
          <div className="probability-labels"><span style={{ '--probability-color': colors.away }}><strong>{number(a)}%</strong> {away.abbreviation}</span><span style={{ '--probability-color': colors.home }}>{home.abbreviation} <strong>{number(h)}%</strong></span></div>
          <div className="probability-bar team-probabilities" role="img" aria-label={`${away.name} ${number(a)}% win probability, ${home.name} ${number(h)}% win probability`}><span style={{ width: `${a}%`, backgroundColor: colors.away }} /><span style={{ width: `${h}%`, backgroundColor: colors.home }} /></div>
          <div className="game-card-footer"><a href={`/games/${game.game_id}`} aria-label={`Open ${away.abbreviation} at ${home.abbreviation} game`}>Open game center <Icon name="arrow" size={13} /></a><span>{Math.abs(a - h) < 0.1 ? 'Even matchup' : `${a > h ? away.abbreviation : home.abbreviation} favoured`}</span></div>
        </article>;
      })}</div>
      <div className="pagination games-pagination"><span>{page * 6 + 1}–{Math.min(page * 6 + data.items.length, data.total)} of {data.total} saved games</span><div><button disabled={page === 0} onClick={() => onPage(page - 1)} aria-label="Previous game page">Previous</button><button disabled={(page + 1) * 6 >= data.total} onClick={() => onPage(page + 1)} aria-label="Next game page">Next <Icon name="arrow" size={14} /></button></div></div>
    </>}
  </section>;
}
