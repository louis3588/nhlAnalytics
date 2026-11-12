import React from 'react';
import { createRoot } from 'react-dom/client';
import '@fontsource-variable/inter';
import App from './App.jsx';
import PlayerPage from './PlayerPage.jsx';
import GamePage from './GamePage.jsx';
import ModelPerformance from './ModelPerformance.jsx';
import StaticGamePage from './StaticGamePage.jsx';
import { STATIC } from './transport.js';
import { useBundleUpdates } from './useBundleUpdates.js';
import './models.css';
import { pageRoute } from './teamBranding.js';
import './styles.css';
import './player.css';
import './game.css';

const route = pageRoute(window.location.pathname);
const Game = STATIC ? StaticGamePage : GamePage;
function PublicationUpdates() { useBundleUpdates(STATIC ? 'public' : null, () => {}, true); return null; }
createRoot(document.getElementById('root')).render(<React.StrictMode><PublicationUpdates />{route.kind === 'models' ? <ModelPerformance /> : route.kind === 'player' ? <PlayerPage playerId={route.playerId} /> : route.kind === 'game' ? <Game gameId={route.gameId} /> : <App />}</React.StrictMode>);
