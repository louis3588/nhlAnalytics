import { useEffect, useMemo, useState } from 'react';
import { checkBundle, fetchAllPages, fetchJSON, median, nameFor, number, queryString, seasonLabel, snapshotDate, snapshotTime, TARGETS, teamFor } from './data.js';
import { useResource } from './useResource.js';
import { useBundleUpdates, useMinuteTick } from './useBundleUpdates.js';
import { Empty, ErrorNote, GameGrid, Loading, PlayerFocus, PlayerTable, ProjectionChart } from './components.jsx';
import Icon from './Icon.jsx';
import LiveGames from './LiveGames.jsx';
import { TeamDirectory, TeamHero } from './TeamIdentity.jsx';
import { pageRoute, teamPageURL, themeStyle } from './teamBranding.js';

export default function App() {
  const route = pageRoute(window.location.pathname);
  const isDirectory = route.kind === 'directory';
  const isTeamPage = route.kind === 'team';
  const routeTeam = isTeamPage ? String(route.teamId) : '';
  const [revision, setRevision] = useState(0);
  const gameClock = useMinuteTick();
  const [season, setSeason] = useState(() => new URLSearchParams(window.location.search).get('season') || '');
  const [team, setTeam] = useState('');
  const [model, setModel] = useState('prior_shrinkage');
  const [target, setTarget] = useState('points');
  const [nominal, setNominal] = useState(0.8);
  const [selectedId, setSelectedId] = useState(null);
  const [search, setSearch] = useState('');
  const [playerPage, setPlayerPage] = useState(0);
  const [sort, setSort] = useState({ key: 'points', direction: 'desc' });
  const [gameStatus, setGameStatus] = useState('upcoming');
  const [gamePage, setGamePage] = useState(0);
  const [activeNav, setActiveNav] = useState('overview');
  const refresh = () => { setRevision(v => v + 1); setGamePage(0); };

  const bootstrap = useResource(`bootstrap:${revision}`, async signal => {
    const [metadata, catalog] = await Promise.all([fetchJSON('/api/metadata', signal), fetchJSON('/api/catalog', signal)]);
    return { metadata, catalog };
  });
  const metadata = bootstrap.data?.metadata;
  const catalog = bootstrap.data?.catalog;
  const activeSeason = season || (metadata ? Math.max(...metadata.seasons) : '');
  const hash = metadata?.bundle_sha256;
  const activeTeam = routeTeam || team;
  const pageTeam = routeTeam && catalog?.teams?.[routeTeam];
  useEffect(() => { document.title = pageTeam ? `${pageTeam.name} player projections | Rinkside` : isDirectory ? 'Team pages | Rinkside' : 'Rinkside | NHL forecast workspace'; }, [pageTeam?.name, isDirectory]);
  const showForecasts = metadata && !isDirectory && route.kind !== 'not-found' && (!isTeamPage || pageTeam);
  const params = { season: activeSeason, team_id: activeTeam || undefined, model };
  const playerKey = showForecasts ? `${revision}:${hash}:players:${queryString(params)}` : null;
  const players = useResource(playerKey, signal => fetchAllPages('/api/predictions/players', params, signal, hash));
  const intervalParams = { ...params, target, nominal };
  const intervals = useResource(showForecasts ? `${revision}:${hash}:intervals:${queryString(intervalParams)}` : null,
    signal => fetchAllPages('/api/predictions/intervals', intervalParams, signal, hash));
  const gameParams = { season: activeSeason, team_id: activeTeam || undefined, status: gameStatus, limit: 6, offset: gamePage * 6 };
  useBundleUpdates(hash, () => setRevision(r => r + 1));
  const games = useResource(showForecasts ? `${revision}:${hash}:${gameClock}:games:${queryString(gameParams)}` : null,
    async signal => checkBundle(await fetchJSON(`/api/predictions/games?${queryString(gameParams)}`, signal), hash));

  const bands = useMemo(() => new Map((intervals.data?.items || []).map(b => [b.player_id, b])), [intervals.data]);
  const filtered = useMemo(() => {
    const term = search.trim().toLocaleLowerCase();
    return (players.data?.items || []).filter(p => !term || `${nameFor(catalog, p.player_id)} ${p.player_id} ${teamFor(catalog, p.last_played_team_id).name}`.toLocaleLowerCase().includes(term));
  }, [players.data, catalog, search]);
  const leaders = useMemo(() => [...filtered].sort((a, b) => b.projected[target] - a.projected[target] || a.player_id - b.player_id), [filtered, target]);
  const selected = filtered.find(p => p.player_id === selectedId) || leaders[0];
  const comparison = leaders.slice(0, 8);
  if (selected && !comparison.some(p => p.player_id === selected.player_id)) comparison.splice(7, 1, selected);
  const tableRows = useMemo(() => [...filtered].sort((a, b) => {
    const diff = sort.key === 'name' ? nameFor(catalog, a.player_id).localeCompare(nameFor(catalog, b.player_id)) : a.projected[sort.key] - b.projected[sort.key];
    return (sort.direction === 'asc' ? diff : -diff) || a.player_id - b.player_id;
  }), [filtered, sort, catalog]);
  const teamOptions = useMemo(() => (catalog?.seasons?.[activeSeason] || []).map(id => teamFor(catalog, id)).sort((a, b) => a.name.localeCompare(b.name)), [catalog, activeSeason]);
  const mid = median(filtered.map(p => p.projected[target]));
  const snapshot = players.data?.snapshot_at;
  const updateSeason = value => { window.history.replaceState(null, '', `${window.location.pathname}?season=${value}`); setSeason(value); setTeam(''); setSearch(''); setPlayerPage(0); setGamePage(0); setSelectedId(null); };
  const updateTeam = value => { if (isTeamPage || isDirectory) { window.location.assign(value ? teamPageURL(value, activeSeason) : `/?season=${activeSeason}`); return; } setTeam(value); setPlayerPage(0); setGamePage(0); setSelectedId(null); };
  const changeTarget = value => { setTarget(value); setSort({ key: value, direction: 'desc' }); setPlayerPage(0); };
  const sortTable = key => { setSort(s => ({ key, direction: s.key === key && s.direction === 'desc' ? 'asc' : 'desc' })); setPlayerPage(0); };

  return <div className="workspace" data-team-page={pageTeam ? pageTeam.abbreviation : undefined} style={pageTeam ? themeStyle(pageTeam) : undefined}><a href="#main" className="skip-link">Skip to forecasts</a>
    <aside className="sidebar"><a className="brand" href="/" aria-label="Rinkside overview"><span className="brand-mark"><Icon name="rink" size={27} /></span><span>rinkside<span className="brand-dot">.</span><small>THE FORECAST DESK</small></span></a>
      <div className="sidebar-label">WORKSPACE</div><nav aria-label="Main navigation">{[['overview', 'grid', 'Overview'], ['players', 'players', 'Player projections'], ['games', 'rink', 'Game predictions']].map(([id, icon, label]) => <a key={id} aria-label={label} href={isDirectory || id === 'overview' && isTeamPage ? `/?season=${activeSeason}#${id}` : `#${id}`} className={!isDirectory && !isTeamPage && activeNav === id ? 'active' : ''} onClick={() => setActiveNav(id)}><Icon name={icon} /><span>{label}</span>{!isDirectory && !isTeamPage && activeNav === id && <i />}</a>)}<a href={`/teams${activeSeason ? `?season=${activeSeason}` : ''}`} className={isDirectory || isTeamPage ? 'active' : ''} aria-label="Team pages"><Icon name="rink" /><span>Team pages</span>{(isDirectory || isTeamPage) && <i />}</a></nav>
      <div className="sidebar-note"><span className="little-rink" aria-hidden="true"><i /></span><strong>A little more<br />perspective on the ice.</strong><p>Explore the numbers.<br />Keep the uncertainty in view.</p><span className="sidebar-note-tag">NHL · REGULAR SEASON</span></div>
      <div className="sidebar-bottom"><span className={`connection-dot ${bootstrap.error ? 'offline' : ''}`} /><span>{bootstrap.error ? 'API disconnected' : bootstrap.loading ? 'Connecting to API' : 'API connected'}<small>Saved forecast workspace</small></span></div>
    </aside>

    <main id="main"><header className="topbar"><span className="breadcrumb"><Icon name="grid" size={16} /><span>Workspace</span><span>/</span><strong>{isDirectory ? 'Team pages' : pageTeam ? pageTeam.name : 'NHL forecasts'}</strong></span><div className="topbar-right"><a className="mobile-team-link" href={`/teams${activeSeason ? `?season=${activeSeason}` : ''}`} aria-label="Browse team pages"><Icon name="rink" size={17} /><span>Teams</span></a><span className="season-tag">REGULAR SEASON</span><a href="/api/metadata" target="_blank" rel="noreferrer" className="icon-button" aria-label="Open saved forecast metadata"><Icon name="external" size={17} /></a></div></header>
      <section id="overview" className="overview">{pageTeam ? <TeamHero team={pageTeam} season={activeSeason} /> : <div className="hero"><div><span className="eyebrow"><span className="tiny-line" /> A BETTER VIEW OF THE GAME</span><h1>{isDirectory ? <>Find your <em>home ice.</em></> : <>Hockey, with <em>perspective.</em></>}</h1><p>Player outlooks. The next matchup. A little less guesswork.</p></div><div className="hero-aside"><span className="snapshot-icon"><Icon name="clock" size={21} /></span><span>Saved snapshot<strong>{metadata ? snapshotDate(metadata.as_of) : 'Connecting…'}</strong><small>{metadata ? `${snapshotTime(metadata.as_of)} UTC · ${metadata.stale ? 'Refresh recommended' : 'Read-only forecasts'}` : 'Reading forecast metadata'}</small></span></div></div>}
        {bootstrap.error ? <section className="panel connection-error"><Empty title="Let’s reconnect to your forecasts">{bootstrap.error}</Empty><button className="button primary" onClick={refresh}><Icon name="refresh" size={17} /> Retry connection</button></section> : bootstrap.loading ? <div className="panel"><Loading text="Opening your forecast workspace…" /></div> : <>
          {metadata.stale && <div className="stale-note" role="status"><Icon name="info" size={17} /><span>These forecasts are from {snapshotDate(metadata.as_of)}. Games and player totals may have moved on since this saved snapshot.</span></div>}
          <div className="filter-bar"><div className="filter-field"><label htmlFor="season">Season</label><select id="season" value={activeSeason} onChange={e => updateSeason(e.target.value)}>{[...metadata.seasons].sort((a, b) => b - a).map(s => <option key={s} value={s}>{seasonLabel(s)} season</option>)}</select></div><span className="filter-divider" /><div className="filter-field team-filter"><label htmlFor="team">Team</label><select id="team" value={activeTeam} onChange={e => updateTeam(e.target.value)}><option value="">All {teamOptions.length || ''} teams</option>{pageTeam && !teamOptions.some(t => t.id === pageTeam.id) && <option value={pageTeam.id}>{pageTeam.name} (unavailable this season)</option>}{teamOptions.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}</select></div><span className="filter-spacer" /><span className="saved-label">{isTeamPage ? <a href={`/teams?season=${activeSeason}`}>Browse all teams <Icon name="arrow" size={14} /></a> : activeTeam ? <a href={teamPageURL(activeTeam, activeSeason)}>Open team page <Icon name="arrow" size={14} /></a> : <><Icon name="check" size={15} /> Saved data, thoughtfully explored</>}</span><button className="button refresh-button" onClick={refresh} aria-label="Refresh saved forecasts" title="Reload the saved snapshot; this does not train models"><Icon name="refresh" size={17} /> Refresh</button></div>
          {isDirectory ? <TeamDirectory teams={teamOptions} season={activeSeason} /> : route.kind === 'not-found' || isTeamPage && !pageTeam ? <Empty title="Team page not found">Choose a team from <a href="/teams">the team directory</a>.</Empty> : <>
          <div className="stat-grid"><article className="stat-card"><span className="stat-icon"><Icon name="players" /></span><span className="stat-label">Players in view</span><strong>{players.loading || players.error ? '—' : number(filtered.length, 0)}</strong><span className="stat-caption">Saved skater projections</span></article><article className="stat-card"><span className="stat-icon mint"><Icon name="up" /></span><span className="stat-label">Median projected {TARGETS[target].label.toLowerCase()}</span><strong>{players.loading || players.error || mid === null ? '—' : number(mid)}<small>{TARGETS[target].unit}</small></strong><span className="stat-caption">Across players in this view</span></article><article className="stat-card"><span className="stat-icon peach"><Icon name="rink" /></span><span className="stat-label">{gameStatus === 'upcoming' ? 'Games ahead' : 'Saved games'}</span><strong>{games.loading || games.error ? '—' : number(games.data?.total || 0, 0)}</strong><span className="stat-caption">{activeTeam ? teamFor(catalog, activeTeam).name : 'All teams'} · {seasonLabel(activeSeason)}</span></article></div>

          <div className="projection-layout"><section className="panel comparison-panel" aria-labelledby="comparison-heading" aria-busy={players.loading || intervals.loading}><div className="section-heading"><div><span className="eyebrow">PLAYER OUTLOOK</span><h2 id="comparison-heading">A total. And the space around it.</h2><p>{snapshot ? `Forecasts made ${snapshotDate(snapshot)} · Regular-season totals` : 'Reading the saved player snapshot'}</p></div><span className="panel-index">01 /</span></div>
            <div className="chart-controls"><div className="segmented target-control" aria-label="Scoring target">{Object.entries(TARGETS).map(([value, info]) => <button key={value} onClick={() => changeTarget(value)} aria-pressed={target === value}>{info.label}</button>)}</div><div className="segmented confidence-control" aria-label="Prediction interval level">{[0.8, 0.95].map(n => <button key={n} aria-pressed={nominal === n} onClick={() => setNominal(n)}>{n * 100}%</button>)}</div></div>
            <div className="chart-subcontrols"><div className="model-picker"><label htmlFor="model">Projection model</label><select id="model" value={model} onChange={e => setModel(e.target.value)}><option value="prior_shrinkage">Prior-season blend</option><option value="pace">Current pace</option></select></div><span>Click a player to explore <Icon name="arrow" size={13} /></span></div>
            {players.error ? <ErrorNote message={players.error} onRetry={refresh} /> : players.loading ? <div className="chart-loading"><Loading text="Reading player projections…" /></div> : !comparison.length ? <Empty title="No projections in this view">Try another team or clear the player search.</Empty> : <ProjectionChart rows={comparison} selectedId={selected?.player_id} onSelect={setSelectedId} catalog={catalog} bands={bands} target={target} nominal={nominal} intervalsLoading={intervals.loading} intervalsError={intervals.error} />}
            {intervals.error && <ErrorNote message={`Intervals: ${intervals.error}`} onRetry={refresh} />}
          </section><PlayerFocus player={selected} catalog={catalog} band={bands.get(selected?.player_id)} target={target} nominal={nominal} loading={intervals.loading || players.loading} error={intervals.error} /></div>

          <PlayerTable rows={tableRows} catalog={catalog} bands={bands} target={target} nominal={nominal} selectedId={selected?.player_id} onSelect={id => { setSelectedId(id); document.getElementById('comparison-heading')?.scrollIntoView({ behavior: 'smooth', block: 'center' }); }} page={playerPage} onPage={setPlayerPage} sort={sort} onSort={sortTable} search={search} onSearch={value => { setSearch(value); setPlayerPage(0); }} loading={players.loading} intervalsLoading={intervals.loading} intervalsError={intervals.error} />
          <LiveGames catalog={catalog} teamId={activeTeam} />
          <GameGrid data={games.data} loading={games.loading} error={games.error} catalog={catalog} page={gamePage} onPage={setGamePage} status={gameStatus} onStatus={value => { setGameStatus(value); setGamePage(0); }} onRetry={refresh} />
          <details className="method-note"><summary><span><Icon name="info" size={17} /> A note on these forecasts</span><Icon name="down" size={16} /></summary><div><p>Player totals use current scoring pace or a blend with the previous season. Games use Elo win probabilities, including overtime and shootouts. Team filters refer to a player’s last recorded team.</p><p>Prediction intervals describe possible season totals, not guarantees or confidence intervals for an average. They can be wide. Goals, assists and points have separate ranges that do not add together. Missing historical calibration is shown as unavailable.</p><p>New daily forecasts use observed player data and frozen historical calibration. Older snapshots and Elo history are reconstructed. These are saved predictions, not live results. Upcoming games are filtered against the current time; probabilities retain the ratings from their saved forecast.</p></div></details>
          </>}
          <footer className="footer"><span><img src="/rink.svg" width="18" height="18" alt="" /> rinkside. <span>A clearer view of the ice.</span></span><span>Snapshot {metadata.bundle_id} <i /> {seasonLabel(activeSeason)}</span></footer>
        </>}
      </section>
    </main>
  </div>;
}
