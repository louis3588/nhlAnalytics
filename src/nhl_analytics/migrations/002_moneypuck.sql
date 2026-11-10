CREATE TABLE analytics.moneypuck_files (
    file_id uuid PRIMARY KEY,
    sha256 text NOT NULL UNIQUE,
    paths jsonb NOT NULL,
    kind text NOT NULL,
    game_type_default smallint NOT NULL CHECK (game_type_default IN (2,3)),
    observed_at timestamptz NOT NULL DEFAULT now(),
    status text NOT NULL CHECK (status IN ('loading','failed','complete')),
    rows_processed bigint NOT NULL DEFAULT 0,
    error text,
    completed_at timestamptz,
    source_url text NOT NULL DEFAULT 'https://moneypuck.com/data.htm',
    attribution text NOT NULL DEFAULT 'MoneyPuck.com'
);

CREATE TABLE analytics.moneypuck_records (
    file_id uuid NOT NULL REFERENCES analytics.moneypuck_files,
    row_number bigint NOT NULL CHECK (row_number > 0),
    observed_at timestamptz NOT NULL,
    kind text NOT NULL,
    record_key text NOT NULL,
    season_id integer NOT NULL REFERENCES analytics.seasons,
    game_type_id smallint NOT NULL CHECK (game_type_id IN (2,3)),
    entity_id text NOT NULL,
    player_id bigint REFERENCES analytics.players,
    goalie_id bigint REFERENCES analytics.players,
    line_player_ids bigint[] NOT NULL,
    team_code text,
    situation text NOT NULL,
    game_id bigint,
    game_date date,
    xgoals_for double precision,
    xgoals_against double precision,
    goals double precision,
    ice_time double precision,
    payload jsonb NOT NULL,
    PRIMARY KEY (file_id, row_number, observed_at),
    UNIQUE (file_id, record_key, observed_at)
);
-- Shots lack calendar dates. Partition by actual import observation time, not invented event times.
SELECT create_hypertable('analytics.moneypuck_records', by_range('observed_at', INTERVAL '1 month'));
CREATE INDEX ON analytics.moneypuck_records (player_id, season_id, game_type_id);
CREATE INDEX ON analytics.moneypuck_records (goalie_id, season_id);
CREATE INDEX ON analytics.moneypuck_records (game_id, team_code, situation);
CREATE INDEX ON analytics.moneypuck_records (kind, season_id, situation);

-- Incomplete file batches are hidden from consumer views.
CREATE VIEW analytics.moneypuck_complete AS
SELECT r.* FROM analytics.moneypuck_records r
JOIN analytics.moneypuck_files f USING (file_id)
WHERE f.status='complete';

CREATE VIEW analytics.moneypuck_latest_season_stats AS
SELECT DISTINCT ON (kind,record_key) * FROM analytics.moneypuck_complete
WHERE kind IN ('skater_season','goalie_season','line_season','team_season')
ORDER BY kind,record_key,observed_at DESC,file_id;

CREATE VIEW analytics.moneypuck_latest_team_games AS
SELECT DISTINCT ON (record_key) * FROM analytics.moneypuck_complete
WHERE kind='team_game' ORDER BY record_key,observed_at DESC,file_id;

CREATE VIEW analytics.moneypuck_latest_shots AS
SELECT DISTINCT ON (record_key) * FROM analytics.moneypuck_complete
WHERE kind='shot' ORDER BY record_key,observed_at DESC,file_id;

CREATE VIEW analytics.moneypuck_line_members AS
SELECT r.file_id,r.row_number,r.observed_at,r.season_id,r.entity_id AS line_id,
       r.team_code,r.situation,unnest(r.line_player_ids) AS player_id
FROM analytics.moneypuck_latest_season_stats r WHERE kind='line_season';
