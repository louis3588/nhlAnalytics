// Representative interface colours taken from the official NHL logo fills.
// These are not a claim to reproduce every historical/alternate jersey palette.
const PALETTES = {
  ANA: ['#CF4520', '#89734C'], ARI: ['#6F263D', '#DDCBA4'], BOS: ['#010101', '#FFB81C'],
  BUF: ['#003087', '#FFB81C'], CAR: ['#CD001A', '#010101'], CBJ: ['#041E42', '#C8102E'],
  CGY: ['#C8102E', '#F1BE48'], CHI: ['#C8102E', '#010101'], COL: ['#862633', '#236192'],
  DAL: ['#00843D', '#010101'], DET: ['#C8102E', '#FFFFFF'], EDM: ['#00205B', '#CF4520'],
  FLA: ['#C8102E', '#041E42'], LAK: ['#010101', '#A2AAAD'], MIN: ['#154734', '#A6192E'],
  MTL: ['#A6192E', '#001E62'], NJD: ['#CD001A', '#010101'], NSH: ['#FFB81C', '#041E42'],
  NYI: ['#003087', '#FC4C02'], NYR: ['#0033A0', '#C8102E'], OTT: ['#C8102E', '#B9975B'],
  PHI: ['#DC4405', '#010101'], PIT: ['#010101', '#FFB81C'], SEA: ['#041C2C', '#9CDBD9'],
  SJS: ['#00778B', '#E57200'], STL: ['#006AC6', '#FFB81C'], TBL: ['#00205B', '#FFFFFF'],
  TOR: ['#00205B', '#FFFFFF'], UTA: ['#6CACE4', '#010101'], VAN: ['#00205B', '#97999B'],
  VGK: ['#B9975B', '#333F48'], WPG: ['#041E42', '#A2AAAD'], WSH: ['#C8102E', '#041E42'],
};
export function paletteFor(team) {
  const [primary, secondary] = PALETTES[team?.abbreviation] || ['#215B48', '#153D35'];
  return { primary, secondary };
}
function rgb(hex) { return [1, 3, 5].map(index => parseInt(hex.slice(index, index + 2), 16)); }
function hex(values) { return '#' + values.map(v => Math.round(v).toString(16).padStart(2, '0')).join(''); }
export function tint(color, amount = 0.93) { return hex(rgb(color).map(c => c + (255 - c) * amount)); }
function luminance(color) {
  const values = rgb(color).map(c => c / 255).map(c => c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  return values.reduce((sum, c, i) => sum + c * [0.2126, 0.7152, 0.0722][i], 0);
}
export function readableColor(color) {
  let result = color;
  while (luminance(result) > 0.15) result = hex(rgb(result).map(c => c * 0.94));
  return result;
}
export function themeStyle(team) {
  const { primary, secondary } = paletteFor(team);
  return { '--team-primary': primary, '--team-secondary': secondary, '--team-soft': tint(primary),
    '--team-ink': readableColor(primary), '--team-on-primary': luminance(primary) > 0.179 ? '#000000' : '#FFFFFF' };
}
export function matchupColors(away, home) {
  const a = paletteFor(away); const h = paletteFor(home);
  return { away: a.primary === h.primary && a.secondary !== '#FFFFFF' ? a.secondary : a.primary, home: h.primary };
}
export function logoSources(team, season) {
  if (!team) return [];
  const official = team.seasonLogos?.[season] || team.logo || `https://assets.nhle.com/logos/nhl/svg/${team.abbreviation}_light.svg`;
  const historical = team.seasonLogos?.[season] && official !== team.logo;
  return [`/team-logos/${team.id}${historical ? `-${season}` : ''}.svg`, official];
}
export function headshotSources(player, team, catalog) {
  if (!player || !team?.abbreviation) return [];
  const patterned = `https://assets.nhle.com/mugs/nhl/${player.season_id}/${team.abbreviation}/${player.player_id}.png`;
  const stored = catalog?.players?.[player.player_id]?.headshot;
  return [...new Set([patterned, stored].filter(Boolean))];
}
export function teamPageURL(id, season) { return `/teams/${id}${season ? `?season=${season}` : ''}`; }
export function playerPageURL(id, season, options = {}) {
  const query = new URLSearchParams({ ...(season ? { season } : {}), ...Object.fromEntries(Object.entries(options).filter(([, value]) => value !== undefined)) });
  return `/players/${id}${query.size ? `?${query}` : ''}`;
}
export function pageRoute(pathname) {
  if (pathname === '/' || pathname === '') return { kind: 'overview' };
  if (/^\/teams\/?$/.test(pathname)) return { kind: 'directory' };
  const game = /^\/games\/([1-9]\d{9})\/?$/.exec(pathname);
  if (game) return { kind: 'game', gameId: Number(game[1]) };
  const player = /^\/players\/([1-9]\d*)\/?$/.exec(pathname);
  if (player) return { kind: 'player', playerId: Number(player[1]) };
  const match = /^\/teams\/([1-9]\d*)\/?$/.exec(pathname);
  return match ? { kind: 'team', teamId: Number(match[1]) } : { kind: 'not-found' };
}
