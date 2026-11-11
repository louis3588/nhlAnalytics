-- GameCenter includes preseason games; existing training/export queries still
-- explicitly select regular season (2) or playoffs (3).
ALTER TABLE analytics.games DROP CONSTRAINT games_game_type_id_check;
ALTER TABLE analytics.games ADD CONSTRAINT games_game_type_id_check CHECK (game_type_id IN (1,2,3));
