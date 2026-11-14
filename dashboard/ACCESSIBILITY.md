# Interface and accessibility checks

The shared Rinkside identity now appears on overview, team, player, game and model
pages. The general palette is blue/slate; team colours remain in team accents and
win-probability bars. All routes show the Louis Polly footer. The top workspace
breadcrumb and metadata/export link were removed. The internal publication
`latest.json` pointer is still required for consistent snapshot loading.

Standings have a padded heading and description, team logos and names, column and
row headers, a timestamped caption, keyboard-scrollable overflow, and definitions
for abbreviated statistics. Tooltips support hover, keyboard focus, touch/click
and Escape dismissal; they are linked to their triggers with accessible names and
descriptions. GameCenter metrics, prediction intervals and model/cutoff context
have explanatory help. Visible link text is preserved in accessible names.

All page types have a skip link and a main landmark. Focus indicators, reduced
motion preferences, labelled graphs and explicit missing-data text remain in use.
Probability values and team names accompany colours.

```powershell
npm.cmd test
$env:NHL_PUBLICATION_SOURCE = (Resolve-Path ../data/publication_automation/publication).Path
$env:NHL_PUBLIC_BUILD_OUTPUT = Join-Path $PWD 'dist-public'
npm.cmd run build:public
# In another terminal, from dashboard:
npm.cmd run preview:public
npm.cmd run test:public
npm.cmd run test:a11y
```

The tests use Playwright/Edge and axe with WCAG A/AA tags through 2.2 on five public
page types, at desktop and 390px phone widths, plus a 900px collapsed-navigation check
and 320px reflow/overflow checks. Results are saved locally under
ignored `test-results/`. Keyboard tooltip behaviour, removal of the export link,
team logos, persistent branding and author credit are also checked in the public
browser test. Browser checks close their own browser processes.

Automated passes are not WCAG certification. Screen-reader usability, touch
devices, 200–400% zoom/reflow, all team-colour combinations and the full range of
live GameCenter states still require broader manual assessment. The live API/SSE
experience remains local; public game pages are read-only snapshots.

The local GameCenter browser test also checks a saved final-game state with axe
and exercises keyboard tooltip opening/dismissal. Run `npm.cmd run test:live`
with the local API and Vite development server available. This does not cover
every live, delayed, postponed or correction state.
