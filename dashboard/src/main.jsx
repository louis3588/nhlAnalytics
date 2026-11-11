import React from 'react';
import { createRoot } from 'react-dom/client';
import '@fontsource-variable/inter';
import App from './App.jsx';
import PlayerPage from './PlayerPage.jsx';
import GamePage from './GamePage.jsx';
import { pageRoute } from './teamBranding.js';
import './styles.css';
import './player.css';
import './game.css';

const route = pageRoute(window.location.pathname);
createRoot(document.getElementById('root')).render(<React.StrictMode>{route.kind === 'player' ? <PlayerPage playerId={route.playerId} /> : route.kind === 'game' ? <GamePage gameId={route.gameId} /> : <App />}</React.StrictMode>);
