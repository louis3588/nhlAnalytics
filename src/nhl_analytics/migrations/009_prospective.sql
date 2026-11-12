-- A separate prospective ledger. Neither historical nor daily forecasts move here.
CREATE TABLE analytics.prospective_campaigns (
    campaign text PRIMARY KEY,
    model_set_id text NOT NULL UNIQUE,
    registered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    tracking_date date NOT NULL,
    season_id integer NOT NULL REFERENCES analytics.seasons,
    elo_version text NOT NULL,
    logistic_version text NOT NULL,
    bundle jsonb NOT NULL,
    policy jsonb NOT NULL
);
CREATE TABLE analytics.prospective_fixtures (
    fixture_id text PRIMARY KEY,
    game_id bigint NOT NULL REFERENCES analytics.games,
    season_id integer NOT NULL REFERENCES analytics.seasons,
    home_team_id integer NOT NULL REFERENCES analytics.teams,
    away_team_id integer NOT NULL REFERENCES analytics.teams,
    scheduled_start timestamptz NOT NULL,
    cutoff_at timestamptz NOT NULL,
    game_date date NOT NULL,
    schedule_state text NOT NULL,
    first_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    source_observed_at timestamptz NOT NULL,
    CHECK(home_team_id<>away_team_id),
    CHECK(cutoff_at=scheduled_start-INTERVAL '1 hour')
);
CREATE INDEX ON analytics.prospective_fixtures(game_id,first_seen_at DESC);
-- Mutable discovery heads are not forecasts or feature snapshots.
CREATE TABLE analytics.prospective_fixture_heads (
    game_id bigint PRIMARY KEY REFERENCES analytics.games,
    fixture_id text NOT NULL REFERENCES analytics.prospective_fixtures,
    provider_checked_at timestamptz NOT NULL,
    game_state text NOT NULL,
    checked_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE analytics.prospective_batches (
    batch_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign text NOT NULL REFERENCES analytics.prospective_campaigns,
    fixture_id text NOT NULL REFERENCES analytics.prospective_fixtures,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    status text NOT NULL CHECK(status IN ('sealed','missing')),
    timing text NOT NULL CHECK(timing IN ('standard','early','none')),
    reason text,
    feature_as_of timestamptz,
    max_source_available_at timestamptz,
    feature_sha256 text,
    features jsonb,
    provenance jsonb NOT NULL,
    elo_home_probability double precision CHECK(elo_home_probability BETWEEN 0 AND 1),
    elo_away_probability double precision GENERATED ALWAYS AS (1-elo_home_probability) STORED,
    logistic_home_probability double precision CHECK(logistic_home_probability BETWEEN 0 AND 1),
    logistic_away_probability double precision GENERATED ALWAYS AS (1-logistic_home_probability) STORED,
    UNIQUE(campaign,fixture_id),
    CHECK((status='sealed' AND elo_home_probability IS NOT NULL AND logistic_home_probability IS NOT NULL
        AND features IS NOT NULL AND feature_sha256 IS NOT NULL AND feature_as_of IS NOT NULL
        AND max_source_available_at IS NOT NULL AND max_source_available_at<feature_as_of
        AND feature_as_of<=recorded_at AND timing IN ('standard','early'))
        OR (status='missing' AND elo_home_probability IS NULL AND logistic_home_probability IS NULL
        AND timing='none' AND reason IS NOT NULL))
);
CREATE TABLE analytics.prospective_attempts (
    campaign text NOT NULL REFERENCES analytics.prospective_campaigns,
    fixture_id text NOT NULL REFERENCES analytics.prospective_fixtures,
    attempted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    reason text NOT NULL,
    PRIMARY KEY(campaign,fixture_id,attempted_at)
);
CREATE TABLE analytics.prospective_result_samples (
    game_id bigint NOT NULL REFERENCES analytics.games,
    checked_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    source_sha256 text NOT NULL,
    status text NOT NULL CHECK(status IN ('candidate_final','conflict','nonfinal')),
    scheduled_start timestamptz NOT NULL,
    home_team_id integer NOT NULL,
    away_team_id integer NOT NULL,
    home_score integer,
    away_score integer,
    winner_team_id integer,
    validation jsonb NOT NULL,
    PRIMARY KEY(game_id,checked_at)
);
CREATE TABLE analytics.prospective_validated_results (
    result_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    game_id bigint NOT NULL REFERENCES analytics.games,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    checked_at timestamptz NOT NULL,
    first_confirmed_at timestamptz NOT NULL,
    source_sha256 text NOT NULL,
    scheduled_start timestamptz NOT NULL,
    home_team_id integer NOT NULL,
    away_team_id integer NOT NULL,
    home_score integer NOT NULL CHECK(home_score>=0),
    away_score integer NOT NULL CHECK(away_score>=0),
    winner_team_id integer NOT NULL,
    validation jsonb NOT NULL,
    UNIQUE(game_id,checked_at),
    CHECK(home_score<>away_score),
    CHECK(winner_team_id=CASE WHEN home_score>away_score THEN home_team_id ELSE away_team_id END),
    CHECK(checked_at>=first_confirmed_at+INTERVAL '10 minutes')
);
CREATE INDEX ON analytics.prospective_validated_results(game_id,checked_at DESC);

CREATE FUNCTION analytics.prospective_no_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Prospective ledger rows are immutable; append a new revision instead'; END $$;
DO $$ DECLARE t text; BEGIN
    FOREACH t IN ARRAY ARRAY['prospective_campaigns','prospective_fixtures','prospective_batches',
        'prospective_attempts','prospective_result_samples','prospective_validated_results'] LOOP
        EXECUTE format('CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON analytics.%I FOR EACH ROW EXECUTE FUNCTION analytics.prospective_no_mutation()',t);
        EXECUTE format('CREATE TRIGGER immutable_table BEFORE TRUNCATE ON analytics.%I FOR EACH STATEMENT EXECUTE FUNCTION analytics.prospective_no_mutation()',t);
    END LOOP;
END $$;
CREATE FUNCTION analytics.prospective_seal_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE f analytics.prospective_fixtures; c analytics.prospective_campaigns;
BEGIN
    NEW.recorded_at:=clock_timestamp();
    SELECT * INTO STRICT f FROM analytics.prospective_fixtures WHERE fixture_id=NEW.fixture_id;
    SELECT * INTO STRICT c FROM analytics.prospective_campaigns WHERE campaign=NEW.campaign;
    IF NEW.status='sealed' THEN
        IF NEW.recorded_at>=f.cutoff_at THEN RAISE EXCEPTION 'Pregame cutoff already passed'; END IF;
        IF NEW.feature_as_of>NEW.recorded_at OR NEW.max_source_available_at>=NEW.feature_as_of
            OR c.registered_at>=NEW.feature_as_of OR f.source_observed_at>=NEW.feature_as_of
            OR f.first_seen_at>=f.cutoff_at OR c.season_id<>f.season_id THEN
            RAISE EXCEPTION 'Invalid prospective feature/model availability';
        END IF;
        IF f.schedule_state<>'OK' THEN RAISE EXCEPTION 'Schedule is not confirmed'; END IF;
        IF NEW.timing='standard' AND NEW.recorded_at<f.cutoff_at-INTERVAL '5 minutes' THEN
            RAISE EXCEPTION 'Standard forecast must be sealed within five minutes before cutoff';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER seal_guard BEFORE INSERT ON analytics.prospective_batches
FOR EACH ROW EXECUTE FUNCTION analytics.prospective_seal_guard();
