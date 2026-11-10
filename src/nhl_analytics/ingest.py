"""Fetch NHL data with source preservation and explicit season boundaries."""

import argparse
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

WEB = "https://api-web.nhle.com/v1"
STATS = "https://api.nhle.com/stats/rest/en"
LOG = logging.getLogger(__name__)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


class Client:
    def __init__(self, raw_dir):
        self.raw_dir = raw_dir
        self.sequence = 0
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": "nhl-analytics/0.1"})
        retry = Retry(total=4, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    def get(self, url, params=None):
        time.sleep(0.2)
        response = self.session.get(url, params=params, timeout=30)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"Expected an object from {response.url}")
        self.sequence += 1
        write_json(self.raw_dir / f"{self.sequence:05d}.json", {
            "url": response.url, "retrievedAt": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        })
        return payload


def format_player(player, group):
    return {
        "id": player["id"], "firstName": player["firstName"]["default"],
        "lastName": player["lastName"]["default"], "number": player.get("sweaterNumber"),
        "position": player["positionCode"], "positionGroup": group,
        "shootsCatches": player.get("shootsCatches"), "birthDate": player.get("birthDate"),
        "birthCountry": player.get("birthCountry"), "heightCm": player.get("heightInCentimeters"),
        "weightKg": player.get("weightInKilograms"), "headshot": player.get("headshot"),
    }


def resolve_team_ids(client, teams):
    references = client.get(f"{STATS}/team")["data"]
    team_ids = {team["triCode"]: team["id"] for team in references if team.get("triCode")}
    return [{**team, "id": team_ids[team["abbreviation"]]} for team in teams]


def fetch_rosters(client, season):
    teams = []
    standings = client.get(f"{WEB}/standings/now")["standings"]
    if not standings or any(team.get("seasonId") != season for team in standings):
        # Offseason dates can return no standings; April is within the regular season.
        standings = client.get(f"{WEB}/standings/{str(season)[4:]}-04-01")["standings"]
    if not standings or any(team.get("seasonId") != season for team in standings):
        raise ValueError("Cannot discover teams for the requested season")
    for team in standings:
        abbreviation = team["teamAbbrev"]["default"]
        LOG.info("Fetching roster: %s", abbreviation)
        payload = client.get(f"{WEB}/roster/{abbreviation}/{season}")
        if not any(group in payload for group in ("forwards", "defensemen", "goalies")):
            raise ValueError(f"Missing roster groups for {abbreviation}")
        players = [format_player(player, group) for group in ("forwards", "defensemen", "goalies")
                   for player in payload.get(group, [])]
        teams.append({"abbreviation": abbreviation,
                      "name": team["teamName"]["default"], "conference": team["conferenceName"],
                      "division": team["divisionName"], "rosterCount": len(players), "players": players})
    return resolve_team_ids(client, teams)


def fetch_stats(client, kind, season, game_type):
    rows = []
    total = None
    while total is None or len(rows) < total:
        payload = client.get(f"{STATS}/{kind}/summary", {
            "isAggregate": "false", "isGame": "false", "start": len(rows), "limit": 100,
            "sort": json.dumps([{"property": "playerId", "direction": "ASC"}]),
            "cayenneExp": f"seasonId={season} and gameTypeId={game_type}",
        })
        page = payload["data"]
        total = payload["total"]
        if not isinstance(page, list) or type(total) is not int or total < 0:
            raise ValueError(f"Invalid {kind} summary response")
        if not page and len(rows) < total:
            raise ValueError(f"Incomplete {kind} pagination: {len(rows)} of {total}")
        rows.extend(page)
        if len(rows) > total:
            raise ValueError(f"Unexpected {kind} pagination total")
        if not page:
            break
    seen = set()
    for row in rows:
        # NHL summary rows may combine teams in teamAbbrevs; these are season totals.
        key = (row["playerId"], row["seasonId"])
        if key in seen:
            raise ValueError(f"Duplicate {kind} season record: {key}")
        if row["seasonId"] != season:
            raise ValueError("Unexpected season in statistics response")
        seen.add(key)
        row["gameTypeId"] = game_type
    return rows


def link_players(teams, skaters, goalies):
    players = {}
    for team in teams:
        for player in team["players"]:
            record = players.setdefault(player["id"], {**player, "rosterTeamIds": []})
            record["rosterTeamIds"].append(team["id"])
    for row in [*skaters, *goalies]:
        if row["playerId"] not in players:
            # Do not guess given/family names from full names or invent biographical data.
            players[row["playerId"]] = {"id": row["playerId"], "fullName": row.get("skaterFullName")
                                       or row.get("goalieFullName"), "rosterTeamIds": []}
    return sorted(players.values(), key=lambda player: player["id"])


def season_argument(value):
    if len(value) != 8 or not value.isdigit() or int(value[4:]) != int(value[:4]) + 1:
        raise argparse.ArgumentTypeError("Use consecutive years, e.g. 20262027")
    return int(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", type=season_argument, required=True)
    parser.add_argument("--game-type", type=int, choices=[2, 3], default=2, help="2: regular season; 3: playoffs")
    parser.add_argument("--output", type=Path, default=Path("data"))
    parser.add_argument("--roster-file", type=Path, help="Use an existing roster export instead of fetching rosters")
    parser.add_argument("--database", action="store_true", help="Also load the completed snapshot into PostgreSQL")
    parser.add_argument("--env-file", type=Path, default=Path('.env'), help="Database connection settings")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    now = datetime.now(timezone.utc)
    run = args.output / str(args.season) / str(args.game_type) / now.strftime("%Y%m%dT%H%M%S%fZ")
    client = Client(run / "raw")
    try:
        if args.roster_file:
            roster = json.loads(args.roster_file.read_text(encoding="utf-8"))
            if str(roster["season"]).replace("-", "") != str(args.season):
                raise ValueError("Roster file season does not match --season")
            teams = resolve_team_ids(client, roster["teams"])
            write_json(run / "raw" / "roster-input.json", roster)
            roster_observed = roster["lastUpdated"]
        else:
            teams = fetch_rosters(client, args.season)
            roster_observed = now.isoformat()
        skaters = fetch_stats(client, "skater", args.season, args.game_type)
        goalies = fetch_stats(client, "goalie", args.season, args.game_type)
        if not skaters or not goalies:
            raise ValueError("No season statistics returned; check the season and game type. No completed snapshot published.")
        metadata = {"schemaVersion": 1, "seasonId": args.season, "gameTypeId": args.game_type,
                    "collectedAt": now.isoformat(), "source": "NHL", "rosterObservedAt": roster_observed}
        players = link_players(teams, skaters, goalies)
        write_json(run / "nhl-rosters.json", {**metadata, "teams": teams})
        write_json(run / "players.json", {**metadata, "players": players})
        write_json(run / "player_season_stats.json", {**metadata, "skaters": skaters, "goalies": goalies})
        write_json(run / "manifest.json", {**metadata, "status": "complete", "teamCount": len(teams),
                                          "playerCount": len(players), "skaterCount": len(skaters), "goalieCount": len(goalies)})
        LOG.info("Saved %s players, %s skater records, %s goalie records to %s", len(players), len(skaters), len(goalies), run)
        if args.database:
            from nhl_analytics.database import connect, load_snapshot, migrate
            import psycopg
            try:
                with connect(args.env_file) as connection:
                    migrate(connection)
                    load_snapshot(connection, run)
                LOG.info("Database snapshot loaded successfully")
            except psycopg.Error:
                parser.exit(1, f"Database load failed; JSON snapshot is complete at {run}. Retry with nhl-db load. Credentials are not logged.\n")
    except (requests.RequestException, ValueError, KeyError, OSError) as error:
        parser.exit(1, f"Import failed: {error}\nPartial raw responses, if any, are in {run}\n")
    finally:
        client.session.close()


if __name__ == "__main__":
    main()
