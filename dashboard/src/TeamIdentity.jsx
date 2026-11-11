import { useState } from 'react';
import { headshotSources, logoSources, teamPageURL, themeStyle } from './teamBranding.js';
import { seasonLabel } from './data.js';
import Icon from './Icon.jsx';

function Asset({ sources, alt, className, fallback, lazy = true }) {
  const [index, setIndex] = useState(0);
  return <span className={className}>{index < sources.length
    ? <img src={sources[index]} alt={alt} loading={lazy ? 'lazy' : 'eager'} decoding="async" onError={() => setIndex(i => i + 1)} />
    : <span className="image-placeholder" role="img" aria-label={`${alt} unavailable`}>{fallback}</span>}</span>;
}

export function TeamLogo({ team, season, className = '', lazy = true }) {
  const sources = logoSources(team, season);
  return <Asset key={sources.join('|')} sources={sources} alt={`${team.name} logo`} className={`team-logo ${className}`} fallback={<Icon name="rink" size={30} />} lazy={lazy} />;
}

export function PlayerHeadshot({ player, team, catalog, name, small = false }) {
  const sources = headshotSources(player, team, catalog);
  return <Asset key={sources.join('|')} sources={sources} alt={`${name} headshot`} className={`avatar player-headshot${small ? ' avatar-small' : ''}`}
    fallback={<svg viewBox="0 0 48 48" width="28" height="28" aria-hidden="true"><circle cx="24" cy="16" r="9" fill="currentColor" opacity=".4" /><path d="M7 45c0-11 6-17 17-17s17 6 17 17" fill="currentColor" opacity=".4" /></svg>} lazy={small} />;
}

export function TeamDirectory({ teams, season }) {
  return <section className="team-directory" aria-labelledby="teams-heading"><div className="section-heading"><div><span className="eyebrow">CHOOSE YOUR COLOURS</span><h2 id="teams-heading">Every team. Their own perspective.</h2><p>{teams.length} teams · {seasonLabel(season)} player projection pages</p></div></div>
    <div className="team-directory-grid">{teams.map(team => <a key={team.id} href={teamPageURL(team.id, season)} className="panel team-directory-card" style={themeStyle(team)}>
      <TeamLogo team={team} season={season} /><span><small>{team.abbreviation} · {seasonLabel(season)}</small><strong>{team.name}</strong><span>Explore player projections <Icon name="arrow" size={14} /></span></span>
    </a>)}</div></section>;
}

export function TeamHero({ team, season }) {
  return <div className="team-hero" style={themeStyle(team)}><div><a href={`/teams?season=${season}`} className="team-back-link">← All teams</a><span className="eyebrow">{seasonLabel(season)} · PLAYER PROJECTIONS</span><h1>{team.name}</h1><p>Recorded totals, season outlooks and the range of what comes next.</p></div><TeamLogo team={team} season={season} lazy={false} /></div>;
}
