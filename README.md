# Public Record Signals crawler

Reads the places where US school districts publish board agendas, bond projects, federal awards and
open grant windows, filters the text for classroom-security language, and writes one feed file that
a separate review step reads and grades. No AI runs here; it is plain Python.

Everything in this repo is public information: the federal NCES district list and text from public board agendas, bond pages, federal award records and grant notices. No customer, contact or contract data is stored here.

## What it does

| Command | What |
| --- | --- |
| `python run.py classify` | For each ideal-size district (6,902), open its website once and find its board platform: BoardDocs, BoardBook, Simbli, Legistar, or the district's own board page. Cached in `data/platforms.json`; resumable. |
| `python run.py agendas` | Read the last 45 days of agendas for the next batch of districts (largest first, each re-read weekly), keyword-filter, and append matches to `data/matches.json`. |
| `python run.py money` | USAspending awards to districts under the school-safety assistance listings, and Grants.gov open or forecasted opportunities about school safety. |
| `python run.py all` | What the daily GitHub Action runs. |

`data/matches.json`: `{"generated": "...", "matches": [ {id, district, state, nces_id, students, source_type (agenda|bond|award|grant_window), platform, url, page, title, date, matched[], strong, snippet, found, amount?, close_date?} ]}`. Matches older than 90 days roll off. `data/report.json` lists blocked hosts and errors. When a portal refuses automated reading, the crawler records it and falls back to the district's own board page.

## Manners
One request per second per host, `robots.txt` honored, the User-Agent names the project and a contact address, 401/403/429 and robots denials are recorded as blocked and never retried around. Portals that block automated reading are left alone; a person checks those by hand.

## Run it locally
```
pip install -r requirements.txt
python run.py classify --limit 50
python run.py agendas --limit 20
python run.py money
```

## Hosting
`.github/workflows/crawl.yml` runs daily at 02:15 UTC on GitHub's free tier (about 2.5 hours a day; the free allowance is 2,000 minutes a month for private repos and unlimited for public ones) and commits the feed. Run it by hand from the Actions tab with "Run workflow".
