CREATE TABLE analytics.seasons (
    season_id integer PRIMARY KEY,
    CHECK (season_id % 10000 = season_id / 10000 + 1)
);

CREATE TABLE analytics.players (
    player_id bigint PRIMARY KEY CHECK (player_id > 0),
    first_name text,
    last_name text,
    full_name text,
    birth_date date,
    birth_country text,
    updated_at timestamptz NOT NULL
);

CREATE TABLE analytics.teams (
    team_id integer PRIMARY KEY,
    abbreviation text NOT NULL,
    name text NOT NULL,
    conference text,
    division text,
    updated_at timestamptz NOT NULL
);

CREATE TABLE analytics.import_runs (
    run_id uuid PRIMARY KEY,
    source text NOT NULL,
    season_id integer NOT NULL REFERENCES analytics.seasons,
    game_type_id smallint NOT NULL CHECK (game_type_id IN (2,3)),
    collected_at timestamptz NOT NULL,
    roster_observed_at timestamptz NOT NULL,
    content_sha256 text NOT NULL,
    snapshot_path text NOT NULL,
    manifest jsonb NOT NULL,
    loaded_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source, season_id, game_type_id, collected_at)
);

-- Observation history: not inferred trade dates or current roster assertions.
CREATE TABLE analytics.roster_observations (
    run_id uuid NOT NULL REFERENCES analytics.import_runs,
    observed_at timestamptz NOT NULL,
    season_id integer NOT NULL REFERENCES analytics.seasons,
    player_id bigint NOT NULL REFERENCES analytics.players,
    team_id integer NOT NULL REFERENCES analytics.teams,
    jersey_number integer,
    position text,
    position_group text,
    shoots_catches text,
    height_cm integer,
    weight_kg integer,
    headshot text,
    payload jsonb NOT NULL,
    PRIMARY KEY (run_id, player_id, team_id, observed_at)
);
SELECT create_hypertable('analytics.roster_observations', by_range('observed_at', INTERVAL '1 month'));
CREATE INDEX ON analytics.roster_observations (player_id, observed_at DESC);
CREATE INDEX ON analytics.roster_observations (team_id, season_id, observed_at DESC);

CREATE TABLE analytics.skater_season_stats (
    run_id uuid NOT NULL REFERENCES analytics.import_runs,
    collected_at timestamptz NOT NULL,
    player_id bigint NOT NULL REFERENCES analytics.players,
    season_id integer NOT NULL REFERENCES analytics.seasons,
    game_type_id smallint NOT NULL CHECK (game_type_id IN (2,3)),
    team_abbrevs text,
    games_played integer CHECK (games_played >= 0),
    goals integer CHECK (goals >= 0),
    assists integer CHECK (assists >= 0),
    points integer CHECK (points >= 0),
    shots integer CHECK (shots >= 0),
    shooting_pct double precision,
    time_on_ice_per_game double precision,
    payload jsonb NOT NULL,
    PRIMARY KEY (run_id, player_id, collected_at)
);
SELECT create_hypertable('analytics.skater_season_stats', by_range('collected_at', INTERVAL '1 month'));
CREATE INDEX ON analytics.skater_season_stats (player_id, season_id, game_type_id, collected_at DESC);

CREATE TABLE analytics.goalie_season_stats (
    run_id uuid NOT NULL REFERENCES analytics.import_runs,
    collected_at timestamptz NOT NULL,
    player_id bigint NOT NULL REFERENCES analytics.players,
    season_id integer NOT NULL REFERENCES analytics.seasons,
    game_type_id smallint NOT NULL CHECK (game_type_id IN (2,3)),
    team_abbrevs text,
    games_played integer CHECK (games_played >= 0),
    games_started integer CHECK (games_started >= 0),
    wins integer CHECK (wins >= 0),
    losses integer CHECK (losses >= 0),
    saves integer CHECK (saves >= 0),
    shots_against integer CHECK (shots_against >= 0),
    goals_against integer CHECK (goals_against >= 0),
    save_pct double precision,
    goals_against_average double precision,
    shutouts integer CHECK (shutouts >= 0),
    time_on_ice double precision,
    payload jsonb NOT NULL,
    PRIMARY KEY (run_id, player_id, collected_at)
);
SELECT create_hypertable('analytics.goalie_season_stats', by_range('collected_at', INTERVAL '1 month'));
CREATE INDEX ON analytics.goalie_season_stats (player_id, season_id, game_type_id, collected_at DESC);

CREATE VIEW analytics.latest_skater_season_stats AS
SELECT DISTINCT ON (player_id, season_id, game_type_id) *
FROM analytics.skater_season_stats
ORDER BY player_id, season_id, game_type_id, collected_at DESC;

CREATE VIEW analytics.latest_goalie_season_stats AS
SELECT DISTINCT ON (player_id, season_id, game_type_id) *
FROM analytics.goalie_season_stats
ORDER BY player_id, season_id, game_type_id, collected_at DESC;
