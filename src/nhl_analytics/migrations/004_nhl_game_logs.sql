CREATE TABLE analytics.games (
    game_id bigint PRIMARY KEY,
    season_id integer NOT NULL REFERENCES analytics.seasons,
    game_type_id smallint NOT NULL CHECK (game_type_id IN (2,3)),
    game_date date NOT NULL,
    home_team_abbrev text NOT NULL,
    away_team_abbrev text NOT NULL,
    updated_at timestamptz NOT NULL,
    UNIQUE (game_id,season_id,game_type_id)
);

CREATE TABLE analytics.game_log_sources (
    source_id uuid PRIMARY KEY,
    player_id bigint NOT NULL REFERENCES analytics.players,
    season_id integer NOT NULL REFERENCES analytics.seasons,
    game_type_id smallint NOT NULL CHECK (game_type_id IN (2,3)),
    kind text NOT NULL CHECK (kind IN ('skater','goalie')),
    observed_at timestamptz NOT NULL,
    content_sha256 text NOT NULL,
    source_url text NOT NULL,
    raw_path text NOT NULL,
    game_count integer NOT NULL,
    expected_games integer NOT NULL
);

CREATE TABLE analytics.game_log_state (
    player_id bigint NOT NULL REFERENCES analytics.players,
    season_id integer NOT NULL REFERENCES analytics.seasons,
    game_type_id smallint NOT NULL,
    kind text NOT NULL,
    content_sha256 text NOT NULL,
    source_id uuid NOT NULL REFERENCES analytics.game_log_sources,
    last_checked_at timestamptz NOT NULL,
    PRIMARY KEY (player_id,season_id,game_type_id,kind)
);

-- Canonical tables enforce exactly one row per NHL player/game.
CREATE TABLE analytics.skater_game_stats (
    player_id bigint NOT NULL REFERENCES analytics.players,
    game_id bigint NOT NULL,
    season_id integer NOT NULL,
    game_type_id smallint NOT NULL,
    team_abbrev text NOT NULL,
    opponent_abbrev text NOT NULL,
    home_road text NOT NULL CHECK (home_road IN ('H','R')),
    goals integer CHECK (goals >= 0),
    assists integer CHECK (assists >= 0),
    points integer CHECK (points >= 0),
    plus_minus integer,
    penalty_minutes integer CHECK (penalty_minutes >= 0),
    power_play_goals integer,
    power_play_points integer,
    shorthanded_goals integer,
    shorthanded_points integer,
    game_winning_goals integer,
    overtime_goals integer,
    shots integer CHECK (shots >= 0),
    shifts integer CHECK (shifts >= 0),
    toi_seconds integer CHECK (toi_seconds >= 0),
    source_id uuid NOT NULL REFERENCES analytics.game_log_sources,
    payload jsonb NOT NULL,
    PRIMARY KEY (player_id,game_id),
    FOREIGN KEY (game_id,season_id,game_type_id) REFERENCES analytics.games(game_id,season_id,game_type_id)
);
CREATE INDEX ON analytics.skater_game_stats (season_id,game_type_id,game_id);

CREATE TABLE analytics.goalie_game_stats (
    player_id bigint NOT NULL REFERENCES analytics.players,
    game_id bigint NOT NULL,
    season_id integer NOT NULL,
    game_type_id smallint NOT NULL,
    team_abbrev text NOT NULL,
    opponent_abbrev text NOT NULL,
    home_road text NOT NULL CHECK (home_road IN ('H','R')),
    games_started integer CHECK (games_started IN (0,1)),
    decision text,
    shots_against integer CHECK (shots_against >= 0),
    goals_against integer CHECK (goals_against >= 0),
    saves integer CHECK (saves >= 0),
    save_pct double precision,
    shutouts integer,
    goals integer,
    assists integer,
    penalty_minutes integer,
    toi_seconds integer CHECK (toi_seconds >= 0),
    source_id uuid NOT NULL REFERENCES analytics.game_log_sources,
    payload jsonb NOT NULL,
    PRIMARY KEY (player_id,game_id),
    FOREIGN KEY (game_id,season_id,game_type_id) REFERENCES analytics.games(game_id,season_id,game_type_id)
);
CREATE INDEX ON analytics.goalie_game_stats (season_id,game_type_id,game_id);

-- Immutable changed-response history, separate from canonical upserts.
CREATE TABLE analytics.player_game_observations (
    source_id uuid NOT NULL REFERENCES analytics.game_log_sources,
    player_id bigint NOT NULL REFERENCES analytics.players,
    game_id bigint NOT NULL REFERENCES analytics.games,
    kind text NOT NULL,
    observed_at timestamptz NOT NULL,
    payload jsonb NOT NULL,
    PRIMARY KEY (source_id,game_id,observed_at)
);
SELECT create_hypertable('analytics.player_game_observations', by_range('observed_at',INTERVAL '1 month'));
CREATE INDEX ON analytics.player_game_observations (player_id,game_id,observed_at DESC);
