# Sunday Prop Board

Live NFL player prop picks for the week's slate, ranked by edge. Odds come from every major US sportsbook (The Odds API), and game logs, rosters and injuries come from ESPN.

**Site:** https://southp98.github.io/nfl-prop-board/

Add it to your phone's home screen to use it like an app.

## How it updates

`tools/refresh.py` pulls the slate, prices and game logs and builds the page from `tools/template.html`. It runs every Wednesday evening and Saturday morning (Eastern time):

```
ODDS_API_KEY=... python3 tools/refresh.py tools/template.html prop-board.html --site .
```

Each run costs about 55 Odds API credits.

Picks are estimates, not guarantees. Check your sportsbook's current line before betting.
