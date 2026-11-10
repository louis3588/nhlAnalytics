ALTER TABLE analytics.games
    ADD COLUMN home_team_id integer REFERENCES analytics.teams,
    ADD COLUMN away_team_id integer REFERENCES analytics.teams,
    ADD COLUMN start_time_utc timestamptz,
    ADD COLUMN venue text,
    ADD COLUMN venue_timezone text,
    ADD COLUMN neutral_site boolean,
    ADD COLUMN game_state text,
    ADD COLUMN schedule_state text,
    ADD COLUMN home_score integer CHECK (home_score >= 0),
    ADD COLUMN away_score integer CHECK (away_score >= 0),
    ADD COLUMN is_final boolean,
    ADD COLUMN result_type text CHECK (result_type IN ('REG','OT','SO')),
    ADD COLUMN winner_team_id integer REFERENCES analytics.teams,
    ADD COLUMN schedule_checked_at timestamptz,
    ADD COLUMN schedule_content_sha256 text,
    ADD COLUMN schedule_payload jsonb;
CREATE INDEX ON analytics.games (season_id,game_type_id,game_date);
CREATE INDEX ON analytics.games (start_time_utc) WHERE is_final=false;

CREATE TABLE analytics.schedule_imports (
    source_id uuid PRIMARY KEY,
    season_id integer NOT NULL REFERENCES analytics.seasons,
    game_types smallint[] NOT NULL,
    observed_at timestamptz NOT NULL,
    content_sha256 text NOT NULL,
    raw_path text NOT NULL,
    game_count integer NOT NULL
);

CREATE TABLE analytics.game_schedule_observations (
    game_id bigint NOT NULL REFERENCES analytics.games,
    observed_at timestamptz NOT NULL,
    source_id uuid NOT NULL REFERENCES analytics.schedule_imports,
    payload jsonb NOT NULL,
    PRIMARY KEY (game_id,observed_at)
);
SELECT create_hypertable('analytics.game_schedule_observations',by_range('observed_at',INTERVAL '1 month'));
CREATE INDEX ON analytics.game_schedule_observations (game_id,observed_at DESC);
