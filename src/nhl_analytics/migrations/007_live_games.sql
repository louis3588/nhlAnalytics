-- Live feeds remain separate from final game logs and prediction features.
CREATE TABLE analytics.live_games (
    game_id bigint PRIMARY KEY REFERENCES analytics.games,
    game_date date NOT NULL,
    game_state text NOT NULL,
    observed_at timestamptz NOT NULL,
    checked_at timestamptz NOT NULL,
    content_sha256 text NOT NULL,
    payload jsonb NOT NULL
);
CREATE INDEX ON analytics.live_games (checked_at DESC);

CREATE TABLE analytics.live_game_snapshots (
    game_id bigint NOT NULL REFERENCES analytics.live_games,
    observed_at timestamptz NOT NULL,
    content_sha256 text NOT NULL,
    payload jsonb NOT NULL,
    PRIMARY KEY (game_id, observed_at)
);
SELECT create_hypertable('analytics.live_game_snapshots', by_range('observed_at', INTERVAL '1 month'));
-- Final current states and event revision history are retained separately.
SELECT add_retention_policy('analytics.live_game_snapshots', INTERVAL '30 days');

CREATE TABLE analytics.live_game_events (
    game_id bigint NOT NULL REFERENCES analytics.live_games,
    event_id integer NOT NULL,
    player_id bigint REFERENCES analytics.players,
    first_seen_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    content_sha256 text NOT NULL,
    is_deleted boolean NOT NULL DEFAULT false,
    payload jsonb NOT NULL,
    PRIMARY KEY (game_id, event_id)
);
CREATE TABLE analytics.live_game_event_revisions (
    game_id bigint NOT NULL REFERENCES analytics.live_games,
    event_id integer NOT NULL,
    observed_at timestamptz NOT NULL,
    content_sha256 text NOT NULL,
    is_deleted boolean NOT NULL,
    payload jsonb NOT NULL,
    PRIMARY KEY (game_id, event_id, observed_at)
);
SELECT create_hypertable('analytics.live_game_event_revisions', by_range('observed_at', INTERVAL '1 month'));
CREATE INDEX ON analytics.live_game_event_revisions (game_id, event_id, observed_at DESC);

CREATE TABLE analytics.live_game_requests (
    game_id bigint PRIMARY KEY,
    requested_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL
);
