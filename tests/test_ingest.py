import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nhl_analytics.ingest import (
    fetch_rosters, fetch_stats, link_players, main, resolve_team_ids,
    season_argument, write_json,
)


class FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.session = self

    def get(self, url, params=None):
        self.calls.append((url, params))
        return next(self.responses)

    def close(self):
        pass


class IngestionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Path(".work/tmp").mkdir(parents=True, exist_ok=True)

    def test_pagination_and_traded_player_totals(self):
        client = FakeClient([
            {"data": [{"playerId": 1, "seasonId": 20262027, "teamAbbrevs": "CGY,EDM"}], "total": 2},
            {"data": [{"playerId": 2, "seasonId": 20262027}], "total": 2},
        ])
        rows = fetch_stats(client, "skater", 20262027, 2)
        self.assertEqual([call[1]["start"] for call in client.calls], [0, 1])
        self.assertEqual(rows[0]["teamAbbrevs"], "CGY,EDM")
        self.assertEqual(rows[1]["gameTypeId"], 2)

    def test_duplicate_stats_are_rejected(self):
        row = {"playerId": 1, "seasonId": 20262027}
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            fetch_stats(FakeClient([{"data": [row, row.copy()], "total": 2}]), "skater", 20262027, 2)

    def test_incomplete_page_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            fetch_stats(FakeClient([{"data": [], "total": 2}]), "goalie", 20262027, 2)

    def test_wrong_season_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unexpected season"):
            fetch_stats(FakeClient([{"data": [{"playerId": 1, "seasonId": 20252026}], "total": 1}]),
                        "skater", 20262027, 2)

    def test_linking_keeps_non_roster_players(self):
        result = link_players([{"id": 20, "players": [{"id": 1, "firstName": "A"}]}],
                              [{"playerId": 2, "skaterFullName": "B"}],
                              [{"playerId": 3, "goalieFullName": "C"}])
        self.assertEqual([player["id"] for player in result], [1, 2, 3])
        self.assertEqual(result[0]["rosterTeamIds"], [20])
        self.assertEqual(result[1]["rosterTeamIds"], [])

    def test_existing_roster_gets_team_ids(self):
        teams = resolve_team_ids(FakeClient([{"data": [{"triCode": "CGY", "id": 20}]}]),
                                 [{"abbreviation": "CGY", "players": []}])
        self.assertEqual(teams[0]["id"], 20)

    def test_historical_team_discovery(self):
        historical = {"seasonId": 20232024, "teamAbbrev": {"default": "ARI"},
                      "teamName": {"default": "Arizona Coyotes"}, "conferenceName": "Western",
                      "divisionName": "Central"}
        client = FakeClient([{"standings": [{"seasonId": 20262027}]},
                             {"standings": [historical]}, {"forwards": [], "goalies": [], "defensemen": []},
                             {"data": [{"triCode": "ARI", "id": 53}]}])
        teams = fetch_rosters(client, 20232024)
        self.assertTrue(client.calls[1][0].endswith("2024-04-01"))
        self.assertTrue(client.calls[2][0].endswith("roster/ARI/20232024"))
        self.assertEqual(teams[0]["id"], 53)

    def test_invalid_season(self):
        for value in ("2026-2027", "20262028", "abcdefgh"):
            with self.assertRaises(argparse.ArgumentTypeError):
                season_argument(value)

    def test_completed_snapshot_has_all_stat_links(self):
        with tempfile.TemporaryDirectory(dir=".work/tmp") as directory:
            root = Path(directory)
            roster = root / "input.json"
            write_json(roster, {"season": "2026-2027", "lastUpdated": "2026-11-09T00:00:00Z",
                                "teams": [{"abbreviation": "CGY", "players": [{"id": 1}]}]})
            client = FakeClient([
                {"data": [{"triCode": "CGY", "id": 20}]},
                {"data": [{"playerId": 1, "seasonId": 20262027}], "total": 1},
                {"data": [{"playerId": 2, "seasonId": 20262027}], "total": 1},
            ])
            with patch("nhl_analytics.ingest.Client", return_value=client), patch("sys.argv", [
                "nhl-fetch", "--season", "20262027", "--roster-file", str(roster), "--output", str(root),
            ]):
                main()
            manifest_path = next(root.glob("20262027/2/*/manifest.json"))
            manifest = json.loads(manifest_path.read_text())
            players = json.loads((manifest_path.parent / "players.json").read_text())["players"]
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual({p["id"] for p in players}, {1, 2})

    def test_empty_stats_do_not_publish_a_manifest(self):
        with tempfile.TemporaryDirectory(dir=".work/tmp") as directory:
            root = Path(directory)
            client = FakeClient([{"data": [], "total": 0}, {"data": [], "total": 0}])
            with patch("nhl_analytics.ingest.Client", return_value=client), \
                    patch("nhl_analytics.ingest.fetch_rosters", return_value=[]), \
                    patch("sys.argv", ["nhl-fetch", "--season", "20262027", "--output", str(root)]):
                with self.assertRaises(SystemExit) as failure:
                    main()
            self.assertEqual(failure.exception.code, 1)
            self.assertEqual(list(root.rglob("manifest.json")), [])


if __name__ == "__main__":
    Path(".work/tmp").mkdir(parents=True, exist_ok=True)
    unittest.main()
