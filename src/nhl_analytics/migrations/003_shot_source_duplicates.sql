-- The historical archive repeats some NHL events under different CSV shotIDs.
-- Retain all source rows while exposing one canonical event in the latest view.
ALTER TABLE analytics.moneypuck_records
    DROP CONSTRAINT moneypuck_records_file_id_record_key_observed_at_key;
CREATE UNIQUE INDEX moneypuck_nonshot_event_key
    ON analytics.moneypuck_records (file_id,record_key,observed_at)
    WHERE kind <> 'shot';

CREATE OR REPLACE VIEW analytics.moneypuck_latest_shots AS
SELECT DISTINCT ON (record_key) * FROM analytics.moneypuck_complete
WHERE kind='shot'
ORDER BY record_key,observed_at DESC,file_id,row_number ASC;
