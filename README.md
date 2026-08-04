# Wing Digital — Activity Dashboard

A GitHub-Pages dashboard, same idea as the preliminary website previews: a static
page (`index.html`) that reads a `data.json` file. No server. No secrets in the repo.

- **Master view** — totals across all clients: texts sent, emails sent/opened, calls taken, ad spend, ad leads.
- **Per-client view** — open `?client=<slug>` (or click a client). Shows that client's GHL activity plus Facebook/Meta ad performance.

## How the data gets in

Two halves, because they live in two different places:

| Data | Source | Filled by |
|---|---|---|
| Texts / emails / calls | GHL (per location) | `pull_ghl.py` (standalone Python) |
| Facebook / Meta ad performance | Meta MCP | the **scheduled Claude agent** |

The Meta MCP only works **inside a Claude session** — a plain Windows scheduled
`python` job cannot reach it. So the daily refresh is a scheduled Claude agent that:

1. runs `python pull_ghl.py` (GHL half),
2. calls the Meta MCP (`ads_get_ad_accounts`, then ad insights) and writes each
   client's `ads` block in `data.json`,
3. `git commit && git push` — GitHub Pages serves the new numbers.

## Setup

1. **Secrets** live only in `ghl-cli/.env` (never here). Per client add:
   `GHL_<SLUG>_PIT` and `GHL_<SLUG>_LOCATION_ID`, then reference those names in `clients.json`.
2. **Add clients** — edit `clients.json` (one entry each, plus `adAccountId` when the Meta account is linked).
3. **First pull** — `python pull_ghl.py`
4. **Publish** — push to GitHub, enable Pages (Settings → Pages → deploy from `main` / root).
5. **Schedule** — set up the daily Claude agent (see `AGENT.md` prompt below) at ~6:30am.

## Status / gaps

- Jackson Roofing ad account `1684863519461451` is **live and reporting** via the
  Meta MCP (the `is_queryable:false` flag only affects `ads_get_ad_entities` entity
  listing, not insights). No lead conversion event is configured in Meta, so
  cost-per-lead is blank until one is set up.
- Jackson's own GHL texts/emails/calls need `GHL_JACKSON_PIT` + `GHL_JACKSON_LOCATION_ID`
  added to `ghl-cli/.env` (a Private Integration Token from Jackson's GHL sub-account).
- Email opens: `pull_ghl.py` reads message-level status, which does not carry opens.
  Wire opens to `ghl-cli/email_stats_live.py` (which polls per-email status) for accurate open rates.
- `pull_ghl.py` counts `messageType`/`direction` per message; GHL call events must
  be present as inbound call messages in the location for "calls taken" to register.

## Files

- `index.html` — the dashboard (master + `?client=` views)
- `data.json` — the data the page reads
- `pull_ghl.py` — GHL activity puller
- `clients.json` — client list + env-var pointers (no secrets)
