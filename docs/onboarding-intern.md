# OSB Data — Intern Onboarding

Welcome. This doc tells you everything you need to know about what we're building, why, and what your job is. Read it end-to-end first; bookmark it for reference.

If anything is unclear, ask. There's no penalty for asking — the penalty is for shipping wrong numbers or missing a customer.

---

## 1. What OSB Data is, in one paragraph

**OSB Data is the source of truth for US legal sports betting numbers.** Every state regulator that licenses sportsbooks publishes monthly (or weekly) financial reports — handle, gross gaming revenue (GGR), promo credits, tax paid, by operator. The catch: each state publishes in a different format (PDFs, Excel spreadsheets, Tableau dashboards, press-release PDFs, web tables), with different column names, different definitions, different release cadences. Hedge funds, equity research desks, consultants, journalists, and operator strategy teams need this data in a normalized, machine-readable form — and they need someone to take responsibility for the numbers being right. That's us. We scrape, parse, normalize, and serve the data via an API and a dashboard; clients consume it through Excel, Python, or our website.

## 2. The opportunity — why this exists

- **US legal sports betting handle is ~$140B/year** and growing fast (states keep coming online — Missouri 2026, North Carolina 2024, etc.).
- **Regulators publish good data but in terrible formats.** Examples we've actually dealt with:
  - Iowa: a PDF that looks like a typewriter document
  - New York: a Tableau dashboard that breaks every six months
  - New Jersey: tax-return PDFs and press releases on different cadences (handle from one, GGR from the other)
  - Oregon: only available via the state university's digital library, which is behind aggressive bot detection
  - Arkansas: rotating filename suffixes (`Mar_*.pdf` → `Apr_*.pdf`) that have to be guessed
- **No one cleanly aggregates this.** Companies like LSR (Legal Sports Report) and Sports Handle do journalism on it; investment banks have hand-keyed spreadsheets that get out of date in days. We're the only thing turning the regulatory pipeline into a normalized live data feed.
- **Customers will pay for this** because the alternative is "have an analyst spend 3 hours every month copying numbers out of state PDFs and 50% of the time getting them wrong." We exist because that workflow is broken at scale.

## 3. The product, end-to-end

There are three customer-facing surfaces:

### a) The public dashboard — `https://app.osbdata.com`
A web app showing handle, GGR, hold rate, operator rankings, state comparisons, time series. Anyone can use it. Free; ad-supported in theory, brand-building in practice. Think of it as our shop window — a journalist or analyst lands here, sees clean charts, then asks "can I get this data?"

### b) The API — `https://api.osbdata.com`
This is the actual product we sell. It's a PostgREST endpoint that exposes the underlying database as REST. No auth required (for now). One URL like `https://api.osbdata.com/monthly_data?state_code=eq.NY&period_type=eq.monthly` returns the JSON. Customers pull this into Excel (Power Query), Python (pandas), or their BI tool.

### c) The ops dashboard — `https://app.osbdata.com/ops`
**This is the one you'll live in.** Internal-facing. Shows every state's scrape status, recent runs, anomalies, source-page health. Credentials: ask Nosher.

There's also a docs page at `https://app.osbdata.com` (Docs tab) showing API endpoints and example queries.

### Who pays
We're early. Prospect profile we're targeting:
- **Sell-side analysts** at investment banks covering DKNG, FLUT (FanDuel parent), MGM, CHDN, CZR, PENN — they need monthly market-share tables. Currently they pay a competitor (Eilers & Krejcik, ~$50k/yr) or build their own.
- **Hedge funds** with positions in those names or trading event contracts — they need granular operator-by-state-by-month for backtests and modeling.
- **Consultants** at McKinsey/Bain/EY who do operator engagements — they need data for client decks.
- **Operator strategy teams** — DK/FD's own competitive intelligence teams sometimes want a clean view of their rivals.
- **Journalists** at the trade press (LSR, Action Network, EGR) — they get a discounted/free tier in exchange for citing us.

## 4. How the system works (10-min architecture overview)

You don't need to read Python or touch infrastructure, but you do need to know enough to spot when something's wrong.

```
┌────────────────────┐   scrapers run on    ┌──────────────────┐
│  35 state          │ ───────────────────► │  Our VPS in      │
│  regulators        │   schedules (tier1   │  Chicago         │
│  (NY, NJ, IL,...)  │   every 6h, etc.)    │  (Vultr)         │
└────────────────────┘                      └────────┬─────────┘
                                                     │
                                                     ▼
                                            ┌──────────────────┐
                                            │  Postgres DB     │
                                            │  api.monthly_data│
                                            └────────┬─────────┘
                                                     │
                                  ┌──────────────────┼──────────────────┐
                                  ▼                  ▼                  ▼
                          ┌──────────────┐  ┌───────────────┐  ┌──────────────┐
                          │ Public       │  │ API           │  │ Ops          │
                          │ dashboard    │  │ (PostgREST)   │  │ dashboard    │
                          │ /app         │  │ /api          │  │ /ops         │
                          └──────────────┘  └───────────────┘  └──────────────┘
```

- **Scrapers** (one per state) run every 6 hours (Tier 1: high-volume states like NY/PA/IL), every 12 hours (Tier 2/3), or daily (Tier 4/5). They go grab the latest reports from each regulator's website, parse them, and write rows into Postgres.
- **Normalization** happens during the scrape — every state's data ends up with the same columns: `state_code`, `period_end`, `operator_standard`, `channel`, `handle`, `standard_ggr`, `hold_pct`, etc.
- **Postgres** is the single source of truth. Everything downstream reads from there.
- **Anomaly checks** run after every scrape. Things like "this month's handle is 7x last month's" or "this operator's row is missing" trigger alerts in the ops dashboard.

### Update cadence by state — roughly
- **Weekly publishers** (NY, WV, MT): new data every Tuesday or Friday
- **Monthly publishers** (most): new data 15-45 days after month-end
- **Slow publishers** (AZ, IL, MO, OR, RI, VA): often 45-60 days late, sometimes missing entirely

### States that are flaky
You'll see these names a lot:
- **AR (Arkansas) and OR (Oregon)**: regulator websites are behind anti-bot walls that block our scraper. We work around this manually for now. Both are currently "stuck" needing manual data entry until we set up a residential proxy.
- **NJ (New Jersey)**: two-source merger (tax returns + press releases) — fragile, often half-published.
- **MI (Michigan)**: retail and online published on different cadences; we hide retail until online catches up.
- **IL (Illinois)** and **NJ**: months where the regulator hasn't published yet are common — not a bug, just a wait.

## 5. The ops dashboard — your daily home

Go to `https://app.osbdata.com/ops`, log in. You'll see six tabs at the top:

1. **Overview** — system health (Postgres up, disk space, recent runs)
2. **States** — a card per state with last-run status, latest period, handle/GGR/hold, anomaly badges
3. **Runs** — history of every scrape run, click a row to see what each state produced
4. **Anomalies** — list of unusual values flagged automatically (e.g. handle jumped 7x MoM)
5. **Sources** — health of each regulator's webpage (did they change anything? is it 200 OK?)
6. **Jobs** — manual triggers you (and we) fire to re-run a state or backfill

You'll spend most of your time on **States**, **Anomalies**, and **Runs**.

## 6. Your Job #1 — Data correctness & verification

**The core question you answer every day: "Are our numbers right?"**

If we serve wrong numbers, customers leave. The product is "trustworthy regulatory data" — the moment we lose that trust, we lose the business. Your job is to be the human in the loop that catches what the automated checks miss.

### What you actually do — three rhythms

#### a) Morning sweep (15-20 min, every weekday)
1. Open the ops dashboard → **States** tab.
2. Sort or scroll to find any state showing `failed`, `empty`, or `timeout`. Note them.
3. Click each failed state → drawer opens with the error text and recent run history.
4. **Open Anomalies tab.** Read everything new since yesterday. Most are noise (pre-2022 historical data) — those should already be suppressed. New ones are the signal. Read each one: does it make sense? Is the regulator just behind, or is our scrape genuinely broken?
5. Cross-check 1-2 states' latest numbers manually:
   - Open `app.osbdata.com` → click the state.
   - Open the state regulator's website in another tab. Find their latest published report.
   - Spot-check 2-3 operator rows: does the handle in our dashboard match the regulator's PDF?
6. Log anything off in our internal channel (Slack/Notion — TBD with you).

#### b) Weekly deep verification (1-2 hours, Fridays)
Pick a different state every week — rotate through all 35. For that week's state:
1. Pull the last 3 months of data from the API.
2. Cross-reference every operator row with the regulator's source.
3. Check that operator names, channel labels, and totals all match.
4. Flag any persistent discrepancies. Even 2% drift across multiple months is worth investigating.

After 35 weeks (8-ish months) you'll have hand-verified every state, and we'll know our data passes manual audit. We'll re-run the cycle.

#### c) Reactive — when something obviously breaks
- A customer emails saying "your March NY DraftKings handle is wrong" → drop everything, verify, fix, respond same day.
- Anomaly dashboard shows a HIGH-severity alert for a state we cover → investigate within an hour.
- A scrape job fails for >24 hours → escalate immediately.

### Common bug patterns (so you know what to look for)

You will see these. Get familiar:

| Pattern | What it looks like | What to do |
|---|---|---|
| **Regulator filename rotation** | We were pulling `Feb_*.pdf`, regulator now publishes `Mar_*.pdf`. State stops updating. | Flag — engineering fixes the URL probe |
| **Bot wall** | State scrape starts failing with 403 errors | Flag as P0 — usually means manual data entry until we proxy through |
| **Partial publish** | One channel (online) has new data but other (retail) doesn't | Flag — engineering decides if the partial should display |
| **Operator rename** | Caesars sportsbook rebrands as something else; numbers split across two operator names | Flag — needs operator_mapping update |
| **Format change** | Regulator publishes data in a new column order; scraper extracts wrong values | Flag fast — handle/GGR can get swapped, looks plausible but is wrong |
| **YoY mismatch** | Handle for March 2026 is suddenly 4x March 2025 for one operator | Investigate — could be a real spike (new market, promo period) or an error |

### How to flag something
For now, until we have a proper ticketing system:
- **Loom video** + 1-2 sentence description, posted in our shared channel
- Include: state, period, what looks wrong, what you'd expect
- Tag Nosher (or whoever's on engineering rotation) if it's urgent

### Tools you'll use
- **Ops dashboard** for daily checks
- **Excel** for cross-referencing (we'll set you up with the demo workbook)
- **Browser** for visiting regulator sites
- **PDF viewer** for reading regulator reports
- **Slack/Notion** for communication and logging

## 7. Your Job #2 — Client outreach

The other half of your job: get more customers. We have a product; we need users.

### Who to target — start narrow

Don't spray. Quality over volume. The right outreach is researched, personalized, and short.

**Priority tiers:**

**Tier 1 — sell-side equity research analysts covering gaming.** ~15-25 of these exist at MS/JPM/Citi/UBS/Macquarie/Wells/Stifel/JMP/B.Riley/Deutsche/Roth/Truist etc. They publish quarterly notes on DKNG, FLUT, MGM, CZR, CHDN, PENN. They need monthly data faster than they currently get it. Find them via:
- Their bank's research page
- LinkedIn ("sell-side analyst gaming")
- Bloomberg if you have access (probably no for an intern; ignore)
- Reading recent equity research notes (you may be able to find via search)

**Tier 2 — hedge funds with gaming exposure.** Harder to find, harder to reach, but bigger contracts. Names that publicly hold gaming: Sachem Head, Engine Capital, Marathon Asset, Cantor Fitzgerald, Coatue. Look at 13F filings for fund holdings of DKNG/FLUT/MGM.

**Tier 3 — consultants & specialty firms.** McKinsey gaming practice, Eilers & Krejcik competitors (Vixio, JMP Group market intelligence), strategy consultancies serving operators.

**Tier 4 — journalists.** Lowest revenue but highest brand value. If LSR cites us in 3 articles, we get organic inbound.

### Channels — in order of effectiveness

1. **Personalized LinkedIn outreach.** Most effective for early-stage B2B.
2. **Cold email.** Better for senior contacts; needs a clear subject and 3-sentence pitch.
3. **Inbound from the dashboard.** Already happening passively; track in spreadsheet.
4. **Conferences / events.** Future — SBC Summit, G2E, ICE. We're not there yet.

### The pitch — three sentences

> "Hi [Name] — I work on OSB Data, a normalized feed of every US state's monthly sports betting financials. We aggregate all 35 regulators' reports into one API + dashboard so analyst teams stop wasting hours every month. If you'd like, I can send a 30-second demo (link below) or set up 15 minutes to show you live."

Adjust tone per recipient. Don't write essays. The dashboard sells itself.

### The demo workflow

When someone says "show me":

1. **Live demo via Zoom/Meet** — share screen, walk through `app.osbdata.com` for 2-3 minutes.
2. **Excel demo** — open Excel, paste a Power Query URL, show data populate live. Most-impressive part. Use the demo workbook (we'll provide it).
3. **API demo** — show the URL in browser, JSON response, "this is what your team pulls into Python."
4. **Pricing conversation** — defer to Nosher. Don't quote prices unless authorized.

A demo doc with the exact script lives at `experiments/api_to_excel/README.md` in our repo.

### Tracking — what to log

Set up a simple Google Sheet (or Notion DB):
- Date contacted
- Name, title, firm
- Channel (LinkedIn / email / inbound)
- Status (waiting, replied, demo-booked, demo-done, decision)
- Follow-up date
- Notes

Update daily. Review weekly.

### Cadence expectations

- **Outreach volume**: 10-15 personalized messages/week to start. Quality > quantity.
- **Follow-up**: anyone who replies gets a response within 24h. Anyone who doesn't reply gets ONE follow-up after 5 days, then drops.
- **Pipeline review**: 30 minutes every Monday with Nosher to go through who's hot.

## 8. Tools & access you'll need on day 1

- **Laptop**: yours. Mac or Windows fine.
- **Excel** (with Power Query — comes with Excel 365). If you're on free / older Excel, tell us — we'll get you a license.
- **Slack** access (workspace invite incoming)
- **Notion / Google Drive** for shared docs (invite incoming)
- **Ops dashboard** login: username `khimor`, password (ask Nosher)
- **Gitea** (our code repo) — only needed if you want to read code; not required for either of your jobs
- **A spreadsheet for tracking outreach** (template provided)
- **LinkedIn Premium** (we'll cover it after the first month if outreach is working)

## 9. Communication — how we work together

- **Daily**: short Slack message at end-of-day with what you did, what you found, what's blocked.
- **Weekly**: 30-min sync Monday morning to review last week + plan the coming week.
- **As-it-happens**: anything urgent (data wrong, customer reply) — Slack DM immediately.

**Don't sit on uncertainty.** If you're not sure whether a number is wrong or how to phrase an outreach message, ask. Asking is faster than spending an hour second-guessing.

## 10. Quick-reference cheat sheet (print this)

- **Public site**: https://app.osbdata.com
- **API**: https://api.osbdata.com (try `/monthly_data?state_code=eq.NY&limit=10`)
- **Ops dashboard**: https://app.osbdata.com/ops
- **API docs**: https://app.osbdata.com (Docs tab)
- **Repo**: https://git.osbdata.com (read-only access)

**States flaky right now (May 2026):**
- AR, OR — bot-walled, manual data only
- AZ, IL, MO, OR, RI, VA — slow publishers, often 45+ days late
- NJ — half-publishes (tax returns vs press releases)
- MI — retail published ahead of online

**Daily checklist:**
- [ ] Ops dashboard → States tab (any red?)
- [ ] Anomaly inbox (any new HIGH severity?)
- [ ] Pick one state, manually spot-check 2 operator rows vs regulator source
- [ ] Outreach: send 2-3 personalized messages
- [ ] Outreach: follow up on yesterday's replies

**Weekly checklist:**
- [ ] Deep-verify one state (rotate through all 35)
- [ ] Update outreach tracker
- [ ] Sync with Nosher
- [ ] Read one regulator's latest published report end-to-end (helps build intuition)

## 11. What we're NOT covering in this doc

- **Prediction markets** (Kalshi, Polymarket): we're building a parallel product around prediction-market data. You may help with parts of this later, but for now ignore it — focus on the sports betting data.
- **Engineering / scrapers**: you don't need to read or modify Python code. If a scraper breaks, you flag it and engineering fixes it.
- **Infrastructure** (Postgres, the VPS, DNS): not your problem.

## 12. Closing — what success looks like

After 30 days you should:
- Know every state's quirks by heart
- Have spot-checked all 35 states at least once
- Have flagged at least 3-5 real data issues (this will happen — we have bugs)
- Have started 20-30 outreach conversations and booked 2-3 demos
- Feel like you understand the product well enough to demo it solo if asked

After 90 days you should be the person who knows the data better than anyone, including me, and be running outreach largely autonomously.

Welcome aboard.

— Nosher
