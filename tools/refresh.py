#!/usr/bin/env python3
"""Rebuild the Sunday Prop Board from live data.

Usage: ODDS_API_KEY=... python3 refresh.py template.html out.html [--site site_dir]
       python3 refresh.py template.html out.html --from saved.json [--site site_dir]   (no API calls)
       python3 refresh.py template.html out.html --reuse-props saved.json [--site site_dir]   (game lines only, 3 credits)

Pulls the week's slate, injuries and player game logs from ESPN, and game lines
and player props from The Odds API, then writes the page with the data embedded.
"""
import json, os, re, statistics, sys, time, unicodedata, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from math import erf, sqrt
from zoneinfo import ZoneInfo

KEY = os.environ.get("ODDS_API_KEY", "")
ODDS = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl"
ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
GAMELOG = "https://site.api.espn.com/apis/common/v3/sports/football/nfl/athletes/{}/gamelog"
ET = ZoneInfo("America/New_York")

# Odds API market -> (label, ESPN game log stat name). Each market costs 1 credit per game.
MARKETS = {
    "player_pass_yds": ("Passing yards", "passingYards"),
    "player_rush_yds": ("Rushing yards", "rushingYards"),
    "player_reception_yds": ("Receiving yards", "receivingYards"),
    "player_receptions": ("Receptions", "receptions"),
}
MIN_CREDITS_LEFT = 15      # never spend the account below this
LOG_WEIGHT = 0.35          # our chance = 65% no-vig market + 35% game-log model; books are sharp
credits = {"remaining": None, "used": None}


def get(url, params=None, tries=3):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "prop-board"}), timeout=30) as r:
                if "x-requests-remaining" in r.headers:
                    credits["remaining"] = int(float(r.headers["x-requests-remaining"]))
                    credits["used"] = int(float(r.headers["x-requests-used"]))
                return json.load(r)
        except Exception as e:
            if i == tries - 1:
                print("FAILED", url.split("apiKey")[0], e, file=sys.stderr)
                return None
            time.sleep(2 ** i)


def norm(name):
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    name = re.sub(r"[.'\-]", "", name)
    return re.sub(r"\s+(jr|sr|ii|iii|iv|v)$", "", name).strip()


def implied(o):
    return -o / (-o + 100) if o < 0 else 100 / (o + 100)


def payout(o):
    return 100 / -o if o < 0 else o / 100


def phi(z):
    return 0.5 * (1 + erf(z / sqrt(2)))


def main(template, out, reuse=None):
    now = datetime.now(timezone.utc)

    # ---- slate from ESPN ----
    sb = get(ESPN + "/scoreboard")
    week = sb["week"]["number"]
    season = sb["season"]["year"]
    games, name_to_abbr = [], {}
    for e in sb["events"]:
        c = e["competitions"][0]
        if c["status"]["type"]["state"] != "pre":
            continue
        home = next(t for t in c["competitors"] if t["homeAway"] == "home")["team"]
        away = next(t for t in c["competitors"] if t["homeAway"] == "away")["team"]
        for t in (home, away):
            name_to_abbr[t["displayName"]] = t["abbreviation"]
        kick = datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
        games.append({"id": f'{away["abbreviation"]}-{home["abbreviation"]}', "away": away["abbreviation"], "home": home["abbreviation"],
                      "awayName": away["displayName"], "homeName": home["displayName"], "kickUtc": kick.isoformat(),
                      "kick": kick.astimezone(ET).strftime("%a %-I:%M%p ET").replace("AM", "a").replace("PM", "p"),
                      "venue": c.get("venue", {}).get("fullName", ""), "note": c.get("notes", [{}])[0].get("headline", "") if c.get("notes") else ""})
    if not games:
        sys.exit("No upcoming games on the ESPN scoreboard.")
    by_names = {(g["awayName"], g["homeName"]): g for g in games}

    # ---- game lines (3 credits) ----
    lines = get(ODDS + "/odds", {"apiKey": KEY, "regions": "us", "markets": "h2h,spreads,totals", "oddsFormat": "american"}) or []
    for ev in lines:
        g = by_names.get((ev["away_team"], ev["home_team"]))
        if not g:
            continue
        g["oddsId"] = ev["id"]
        q = {"spreads": [], "totals": [], "h2h": []}
        for b in ev["bookmakers"]:
            for m in b["markets"]:
                for o in m["outcomes"]:
                    if m["key"] in q:
                        q[m["key"]].append({"book": b["title"], "name": o["name"], "point": o.get("point"), "price": o["price"]})
        g["books"] = len(ev["bookmakers"])
        lines_out = {}

        def best(rows):
            return max(rows, key=lambda x: payout(x["price"])) if rows else None

        def fair_pair(a, b):  # no-vig chance of side a from average implied prices
            ia, ib = statistics.mean(implied(x["price"]) for x in a), statistics.mean(implied(x["price"]) for x in b)
            return ia / (ia + ib)

        home_sp = [x for x in q["spreads"] if x["name"] == ev["home_team"]]
        if home_sp:
            hs = statistics.median(x["point"] for x in home_sp)
            hs = min((x["point"] for x in home_sp), key=lambda p: abs(p - hs))  # a line a book actually offers
            g["fav"], g["spread"] = (g["home"], -hs) if hs <= 0 else (g["away"], hs)
            h = [x for x in home_sp if x["point"] == hs]
            a = [x for x in q["spreads"] if x["name"] == ev["away_team"] and x["point"] == -hs]
            if h and a:
                bh, ba = best(h), best(a)
                lines_out["spread"] = {"homeLine": hs, "home": {"odds": bh["price"], "book": bh["book"]},
                                       "away": {"odds": ba["price"], "book": ba["book"]}, "fairHome": round(fair_pair(h, a), 4)}
        overs = [x for x in q["totals"] if x["name"] == "Over"]
        if overs:
            t = statistics.median(x["point"] for x in overs)
            t = min((x["point"] for x in overs), key=lambda p: abs(p - t))
            g["total"] = t
            o = [x for x in overs if x["point"] == t]
            u = [x for x in q["totals"] if x["name"] == "Under" and x["point"] == t]
            if o and u:
                bo, bu = best(o), best(u)
                lines_out["total"] = {"line": t, "over": {"odds": bo["price"], "book": bo["book"]},
                                      "under": {"odds": bu["price"], "book": bu["book"]}, "fairOver": round(fair_pair(o, u), 4)}
        mh = [x for x in q["h2h"] if x["name"] == ev["home_team"]]
        ma = [x for x in q["h2h"] if x["name"] == ev["away_team"]]
        if mh and ma:
            bh, ba = best(mh), best(ma)
            g["mlHome"], g["mlAway"] = bh["price"], ba["price"]
            lines_out["ml"] = {"home": {"odds": bh["price"], "book": bh["book"]}, "away": {"odds": ba["price"], "book": ba["book"]},
                               "fairHome": round(fair_pair(mh, ma), 4)}
        g["lines"] = lines_out
        if "total" in g and "spread" in g:
            half, adj = g["total"] / 2, g["spread"] / 2
            fav_pts = half + adj
            g["homeImp"] = round(fav_pts if g["fav"] == g["home"] else g["total"] - fav_pts, 1)
            g["awayImp"] = round(g["total"] - g["homeImp"], 1)

    # ---- team form: every completed 2026 regular-season game ----
    teams_form = {}
    abbrs = sorted({g["home"] for g in games} | {g["away"] for g in games})
    with ThreadPoolExecutor(8) as ex:
        for abbr, s in zip(abbrs, ex.map(lambda t: get(f"{ESPN}/teams/{t.lower()}/schedule", {"season": season, "seasontype": 2}), abbrs)):
            rows = []
            for e in (s or {}).get("events", []):
                c = e["competitions"][0]
                if not c["status"]["type"].get("completed"):
                    continue
                me = next((t for t in c["competitors"] if t["team"]["abbreviation"] == abbr), None)
                op = next((t for t in c["competitors"] if t["team"]["abbreviation"] != abbr), None)
                if not me or not op:
                    continue
                sc = lambda t: float(t["score"]["value"] if isinstance(t.get("score"), dict) else t.get("score") or 0)
                rows.append({"wk": e.get("week", {}).get("number"), "opp": ("vs " if me["homeAway"] == "home" else "@") + op["team"]["abbreviation"],
                             "pf": sc(me), "pa": sc(op)})
            teams_form[abbr] = rows

    def pull_props():
        # ---- injuries and rosters ----
        injuries = {}
        for team in (get(ESPN + "/injuries") or {}).get("injuries", []):
            for i in team.get("injuries", []):
                a = i.get("athlete", {})
                injuries[norm(a.get("displayName", ""))] = {"status": i.get("status", ""), "comment": i.get("shortComment", "")}
        teams = sorted({g["home"] for g in games} | {g["away"] for g in games})
        roster = {}  # normalized name -> {id, team, pos}
        with ThreadPoolExecutor(8) as ex:
            for abbr, r in zip(teams, ex.map(lambda t: get(f"{ESPN}/teams/{t.lower()}/roster"), teams)):
                for grp in (r or {}).get("athletes", []):
                    for a in grp["items"]:
                        roster[norm(a["fullName"])] = {"id": a["id"], "team": abbr, "pos": a.get("position", {}).get("abbreviation", "")}

        # ---- player props (1 credit per market per game) ----
        props = {}  # (player, market) -> data
        prop_games = [g for g in games if g.get("oddsId")]
        for g in prop_games:
            need = len(MARKETS)
            if credits["remaining"] is not None and credits["remaining"] - need < MIN_CREDITS_LEFT:
                print("Stopping props: credit floor reached", file=sys.stderr)
                break
            ev = get(f'{ODDS}/events/{g["oddsId"]}/odds', {"apiKey": KEY, "regions": "us", "markets": ",".join(MARKETS), "oddsFormat": "american"})
            if not ev:
                continue
            g["hasProps"] = True
            for b in ev.get("bookmakers", []):
                for m in b["markets"]:
                    for o in m["outcomes"]:
                        if "point" not in o or "description" not in o:
                            continue
                        k = (o["description"], m["key"])
                        p = props.setdefault(k, {"player": o["description"], "market": m["key"], "game": g["id"], "quotes": []})
                        p["quotes"].append({"book": b["title"], "side": o["name"].lower(), "point": o["point"], "price": o["price"]})

        # ---- game logs ----
        def player_info(name):
            return roster.get(norm(name))
        ids = sorted({player_info(p["player"])["id"] for p in props.values() if player_info(p["player"])})
        logs = {}
        with ThreadPoolExecutor(8) as ex:
            for pid, gl in zip(ids, ex.map(lambda i: get(GAMELOG.format(i)), ids)):
                if not gl:
                    continue
                names, evmeta = gl.get("names", []), gl.get("events", {})
                rows = []
                for st in gl.get("seasonTypes", []):
                    if "Regular" not in st.get("displayName", "") or str(season) not in st.get("displayName", ""):
                        continue
                    for cat in st.get("categories", []):
                        for e in cat.get("events", []):
                            meta = evmeta.get(e["eventId"], {})
                            vals = {}
                            for n, v in zip(names, e["stats"]):
                                try:
                                    vals[n] = float(str(v).replace(",", ""))
                                except ValueError:
                                    pass
                            rows.append({"week": meta.get("week"), "opp": meta.get("opponent", {}).get("abbreviation", ""),
                                         "at": meta.get("atVs", ""), "date": meta.get("gameDate", ""), **vals})
                rows.sort(key=lambda r: r["date"])
                logs[pid] = rows

        # ---- model ----
        out_props = []
        for (player, mkey), p in props.items():
            info = player_info(player)
            label, stat = MARKETS[mkey]
            points = sorted(q["point"] for q in p["quotes"])
            line = statistics.median(points)
            line = min(points, key=lambda x: abs(x - line))  # snap to a line a book actually offers
            at = [q for q in p["quotes"] if q["point"] == line]
            over = [q for q in at if q["side"] == "over"]
            under = [q for q in at if q["side"] == "under"]
            if not over or not under:
                continue
            # no-vig market probability from the average implied prices at the consensus line
            io, iu = statistics.mean(implied(q["price"]) for q in over), statistics.mean(implied(q["price"]) for q in under)
            fair_over = io / (io + iu)
            log = logs.get(info["id"], []) if info else []
            vals = [r[stat] for r in log if stat in r]
            g = next(x for x in games if x["id"] == p["game"])
            team = info["team"] if info else ""
            opp = (g["home"] if team == g["away"] else g["away"]) if team else ""
            inj = injuries.get(norm(player))
            base = {"player": player, "team": team, "pos": info["pos"] if info else "", "opp": opp, "game": p["game"], "market": label,
                    "line": line, "books": len({q["book"] for q in p["quotes"]}), "fairOver": round(fair_over, 4),
                    "injury": inj if inj and inj["status"] not in ("Active", "") else None,
                    "log": [{"wk": r["week"], "opp": ("@" if r["at"] == "@" else "vs ") + r["opp"], "v": r[stat]} for r in log if stat in r][-8:]}
            for side, quotes in (("over", over), ("under", under)):
                best = max(quotes, key=lambda q: payout(q["price"]))
                fair = fair_over if side == "over" else 1 - fair_over
                row = dict(base, side=side, odds=best["price"], book=best["book"], fair=round(fair, 4), implied=round(implied(best["price"]), 4))
                if len(vals) >= 3:
                    n = len(vals)
                    w = [0.85 ** (n - 1 - i) for i in range(n)]  # recent games count more
                    mean = sum(a * b for a, b in zip(w, vals)) / sum(w)
                    sd = max(statistics.stdev(vals), 1.0 if stat == "receptions" else 8.0)
                    p_over = 1 - phi((line - mean) / sd)
                    hits = sum(1 for v in vals if (v > line if side == "over" else v < line))
                    p_model = (p_over if side == "over" else 1 - p_over) * 0.5 + ((hits + 3) / (n + 6)) * 0.5
                    # blend with the no-vig market, then measure against the price you'd actually pay
                    est = min(max(fair + LOG_WEIGHT * (p_model - fair), 0.02), 0.98)
                    last5 = vals[-5:]
                    row.update(est=round(est, 4), edge=round(est - implied(best["price"]), 4),
                               ev=round(100 * (est * payout(best["price"]) - (1 - est)), 2),
                               avg=round(statistics.mean(vals), 1), wavg=round(mean, 1), med=statistics.median(vals),
                               hits=hits, games=n, hits5=sum(1 for v in last5 if (v > line if side == "over" else v < line)), n5=len(last5),
                               last3=round(statistics.mean(vals[-3:]), 1))
                out_props.append(row)
        return out_props

    if reuse:   # keep the saved props and game logs; only game lines and team form are fresh
        out_props = reuse["props"]
        for g in games:
            g["hasProps"] = any(p["game"] == g["id"] for p in out_props)
    else:
        out_props = pull_props()
    prop_games = [g for g in games if g.get("hasProps")]

    now_et = now.astimezone(ET).strftime("%a %b %-d, %-I:%M %p ET")
    props_et = (reuse.get("propsEt") or reuse.get("generatedEt")) if reuse else now_et
    data = {"generated": now.isoformat(), "generatedEt": now_et, "propsEt": props_et,
            "week": week, "season": season, "games": games, "props": out_props, "teams": teams_form,
            "credits": credits, "markets": [v[0] for v in MARKETS.values()]}
    json.dump(data, open(os.path.splitext(out)[0] + ".json", "w"))
    build(template, out, data)
    priced = [r for r in out_props if "edge" in r]
    print(f"Week {week}: {len(games)} games, {len(prop_games)} with lines, {len(out_props)} prop sides, {len(priced)} modeled, "
          f"{sum(1 for r in priced if r['edge'] > 0)} with positive edge. Credits left: {credits['remaining']}")


def build(template, out, data):
    html = open(template).read().replace("/*__DATA__*/null", json.dumps(data, separators=(",", ":")).replace("</", "<\\/"))
    open(out, "w").write(html)


SITE_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#ffd400">
<meta name="description" content="Live NFL player prop picks ranked by edge, with odds from every major sportsbook, game logs and hit rates.">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Prop Board">
<link rel="manifest" href="manifest.webmanifest">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<link rel="icon" href="icon-192.png">
<style>:root{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}body{margin:0;font:14px system-ui,sans-serif}img{max-width:100%}[hidden]{display:none!important}</style>
"""

MANIFEST = {"name": "Sunday Prop Board", "short_name": "Prop Board",
            "description": "Live NFL player prop picks ranked by edge.", "start_url": "./", "scope": "./",
            "display": "standalone", "background_color": "#f5f5f7", "theme_color": "#ffd400",
            "icons": [{"src": "icon-192.png", "sizes": "192x192", "type": "image/png"},
                      {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}]}

SW = """// App shell cached for offline; network first so each weekly refresh shows up right away.
const CACHE = "prop-board-v1";
const SHELL = ["./", "./index.html", "./manifest.webmanifest", "./icon-192.png", "./icon-512.png", "./apple-touch-icon.png"];
self.addEventListener("install", e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())); });
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  if (e.request.method !== "GET") return;
  const url = new URL(e.request.url);
  const shell = url.origin === location.origin, fonts = url.host === "fonts.googleapis.com" || url.host === "fonts.gstatic.com";
  if (!shell && !fonts) return;
  if (shell) {
    e.respondWith(fetch(e.request).then(r => { const copy = r.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)); return r; })
      .catch(() => caches.match(e.request).then(r => r || caches.match("./index.html"))));
  } else {
    e.respondWith(caches.match(e.request).then(hit => hit || fetch(e.request).then(r => { const copy = r.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)); return r; })));
  }
});
"""


def build_site(page, site_dir):
    """Turn the built artifact page into a standalone GitHub Pages site (index.html, manifest, service worker)."""
    body = open(page).read()
    cut = body.index("<style>")  # title, meta and font links go in <head>
    html = (SITE_HEAD + body[:cut] + "</head>\n<body>\n" + body[cut:] +
            '\n<script>if("serviceWorker" in navigator&&location.protocol.startsWith("http"))'
            'addEventListener("load",()=>navigator.serviceWorker.register("sw.js").catch(()=>{}));</script>\n</body>\n</html>\n')
    os.makedirs(site_dir, exist_ok=True)
    open(os.path.join(site_dir, "index.html"), "w").write(html)
    open(os.path.join(site_dir, "manifest.webmanifest"), "w").write(json.dumps(MANIFEST, indent=2) + "\n")
    open(os.path.join(site_dir, "sw.js"), "w").write(SW)


if __name__ == "__main__":
    args = sys.argv[1:]
    site = None
    if "--site" in args:   # also write a GitHub Pages copy into this folder
        i = args.index("--site"); site = args[i + 1]; del args[i:i + 2]
    if len(args) > 3 and args[2] == "--from":   # rebuild the page from a saved pull, no API calls
        build(args[0], args[1], json.load(open(args[3])))
    elif len(args) > 3 and args[2] == "--reuse-props":   # fresh game lines (3 credits), props from a saved pull
        main(args[0], args[1], json.load(open(args[3])))
    else:
        main(args[0], args[1])
    if site:
        build_site(args[1], site)
