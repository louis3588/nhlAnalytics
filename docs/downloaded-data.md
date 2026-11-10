# Initial inspection of downloaded CSV files

Inspected headers and one row per CSV in `data/`; no full scan or import of the
large history/shot files. These are sample findings, not complete data validation.
Original downloads have not been renamed or changed.

| File(s) | Approximate size | Columns | First-row finding |
| --- | --- | --- | --- |
| `Season_Skaters_2025_2026.csv`, `Season_Skaters_2026_2027.csv` | 3.5 / 2.7 MB | 154 | `playerId`, season, team and situation present; sampled seasons 2025 / 2026 |
| `Season_Goalies_2025_2026.csv`, `Season_Goalies_2026_2027.csv` | 93 / 69 KB | 36 | `playerId`, season, team and situation present; sampled seasons 2025 / 2026 |
| `Season_Lines_2025_2026.csv`, `Season_Lines_2026_2027.csv` | 521 KB each | 108 | Both sample rows have `season=2026`; first file may be mislabeled |
| `Season_Teams_2025_2026.csv`, `Season_Teams_2026_2027.csv` | 100 / 91 KB | 107 | Repeated `team` column name; sampled seasons 2025 / 2026 |
| `Game_By_Game_Data.csv` | 128 MB | 111 | Team-game data (`name=NYR`, no player ID); first sample is 2008 |
| `Shots_2007-2025.csv` | 1.22 GB | 124 | Shot events; first sampled season is 2025, IDs can be decimal-form strings |

The historical team-game dataset is useful for game models, but does not replace
individual player game logs. We still need skater/goalie game-level data to build
historical player projections. Check/redownload the prior-season lines file from
the correct season link before importing it. Equal file sizes alone do not prove
files are duplicates; the sampled content is what raised the season-label concern.

MoneyPuck `season=2026` means NHL `seasonId=20262027`. Derive this from the contents
and compare it to the filename. Do not use the filename as the only season source.
Line IDs combine players; retain them as strings rather than NHL player IDs.
For shot IDs such as `8478542.0`, parse with exact decimal/integer validation;
reject fractional values and preserve blanks/nulls. Do not join on raw strings.
The shot `team` sample is `HOME`, not a franchise abbreviation: use the home/away
team columns and validate game-ID mapping before joining.

Future imports should stream/chunk these files, validate row grain and required
fields, and preserve situation/source/season/team context. Repeated CSV headers
need explicit handling with a positional CSV reader (a naive dictionary reader
silently overwrites earlier columns). Validate whether the repeated values agree
before choosing a canonical team column. Keep `all` rows separate from their
strength-state components to avoid double counting.

Files remain under ignored `data/` and must not be committed to Git. Keep source
URLs, download timestamps and MoneyPuck attribution alongside future import runs.
