ALTER TABLE analytics.players
    ADD COLUMN first_name_inferred boolean NOT NULL DEFAULT false,
    ADD COLUMN last_name_inferred boolean NOT NULL DEFAULT false;
CREATE TABLE analytics.player_name_inferences (
    player_id bigint NOT NULL REFERENCES analytics.players,
    inferred_at timestamptz NOT NULL,
    original_first_name text,
    original_last_name text,
    original_full_name text NOT NULL,
    inferred_first_name text,
    inferred_last_name text,
    PRIMARY KEY (player_id,inferred_at)
);

CREATE TABLE analytics.team_standings (
    team_id integer NOT NULL REFERENCES analytics.teams,
    season_id integer NOT NULL REFERENCES analytics.seasons,
    standings_date date NOT NULL,
    games_played integer NOT NULL CHECK (games_played>=0),
    wins integer NOT NULL CHECK (wins>=0),
    losses integer NOT NULL CHECK (losses>=0),
    ot_losses integer NOT NULL CHECK (ot_losses>=0),
    ties integer NOT NULL CHECK (ties>=0),
    points integer NOT NULL CHECK (points>=0),
    points_pct double precision,
    goals_for integer NOT NULL,
    goals_against integer NOT NULL,
    goal_differential integer NOT NULL,
    regulation_wins integer,
    regulation_ot_wins integer,
    league_rank integer,
    conference_rank integer,
    division_rank integer,
    wildcard_rank integer,
    conference text,
    division text,
    streak_type text,
    streak_count integer,
    observed_at timestamptz NOT NULL,
    content_sha256 text NOT NULL,
    source_url text NOT NULL,
    raw_path text NOT NULL,
    payload jsonb NOT NULL,
    PRIMARY KEY (team_id,season_id,standings_date)
);
SELECT create_hypertable('analytics.team_standings',by_range('standings_date',INTERVAL '1 month'));
CREATE VIEW analytics.latest_team_standings AS
SELECT DISTINCT ON (team_id,season_id) * FROM analytics.team_standings
ORDER BY team_id,season_id,standings_date DESC;

CREATE TABLE analytics.team_standings_observations (
    team_id integer NOT NULL REFERENCES analytics.teams,
    season_id integer NOT NULL REFERENCES analytics.seasons,
    standings_date date NOT NULL,
    observed_at timestamptz NOT NULL,
    source_url text NOT NULL,
    raw_path text NOT NULL,
    payload jsonb NOT NULL,
    PRIMARY KEY (team_id,season_id,standings_date,observed_at)
);
SELECT create_hypertable('analytics.team_standings_observations',by_range('observed_at',INTERVAL '1 month'));

CREATE TABLE analytics.team_season_edge (
    team_id integer NOT NULL REFERENCES analytics.teams,
    season_id integer NOT NULL REFERENCES analytics.seasons,
    game_type_id smallint NOT NULL CHECK (game_type_id IN (2,3)),
    games_played integer NOT NULL CHECK (games_played>=0),
    skating_speed_max_kmh double precision,
    speed_bursts_over20mph integer,
    speed_bursts_over22mph integer,
    total_distance_km double precision,
    top_shot_speed_kmh double precision,
    shot_attempts_over90mph integer,
    offensive_zone_pct double precision,
    offensive_zone_even_strength_pct double precision,
    neutral_zone_pct double precision,
    defensive_zone_pct double precision,
    shots_on_goal integer,
    high_danger_shots integer,
    high_danger_goals integer,
    observed_at timestamptz NOT NULL,
    content_sha256 text NOT NULL,
    source_url text NOT NULL,
    raw_path text NOT NULL,
    payload jsonb NOT NULL,
    PRIMARY KEY (team_id,season_id,game_type_id)
);
CREATE TABLE analytics.team_edge_observations (
    team_id integer NOT NULL REFERENCES analytics.teams,
    season_id integer NOT NULL REFERENCES analytics.seasons,
    game_type_id smallint NOT NULL,
    observed_at timestamptz NOT NULL,
    source_url text NOT NULL,
    raw_path text NOT NULL,
    payload jsonb NOT NULL,
    PRIMARY KEY (team_id,season_id,game_type_id,observed_at)
);
SELECT create_hypertable('analytics.team_edge_observations',by_range('observed_at',INTERVAL '1 month'));
CREATE VIEW analytics.team_edge_features AS
SELECT e.*,
    total_distance_km/NULLIF(games_played,0) AS distance_per_game_km,
    speed_bursts_over20mph::double precision/NULLIF(games_played,0) AS speed_bursts_over20mph_per_game,
    high_danger_shots::double precision/NULLIF(shots_on_goal,0) AS high_danger_shot_share
FROM analytics.team_season_edge e;
