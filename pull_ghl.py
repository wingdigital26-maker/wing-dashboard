#!/usr/bin/env python3
"""
pull_ghl.py -- GHL activity puller for the Wing Digital dashboard.

Walks each configured GHL location and counts real activity:
  texts sent   = outbound SMS messages
  emails sent  = outbound email messages   (opened = status in opened/clicked/replied)
  calls taken  = inbound call messages

Writes the GHL half of data.json. The scheduled Claude agent fills the `ads`
blocks afterward from the Meta MCP (a plain cron job can't reach the MCP).

Secrets are read from ghl-cli/.env by env-var NAME listed in clients.json;
no token ever lives in this repo. The master account uses GHL_API_KEY /
GHL_LOCATION_ID (the Wing Digital location).

Usage:
  python pull_ghl.py            # refresh data.json from live GHL
  python pull_ghl.py --days 30  # activity window (default 30)
"""
import argparse, json, os, sys, time, threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, "..", "..", "ghl-cli", ".env")
DATA_PATH = os.path.join(HERE, "data.json")
CLIENTS_PATH = os.path.join(HERE, "clients.json")
S = "https://services.leadconnectorhq.com"

OPENED = {"opened", "clicked", "replied"}


def log(*a): print(*a, file=sys.stderr, flush=True)


def load_env():
    with open(ENV_PATH, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


# throttle: GHL PIT burst limit ~100 req / 10s
_lock = threading.Lock(); _window = []
def _throttle():
    while True:
        with _lock:
            now = time.time()
            while _window and now - _window[0] > 10: _window.pop(0)
            if len(_window) < 80: _window.append(now); return
        time.sleep(0.2)


class Api:
    def __init__(self, token):
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}",
                               "Version": "2021-07-28", "Accept": "application/json"})
    def get(self, path, params=None):
        for i in range(4):
            _throttle()
            try:
                r = self.s.get(f"{S}{path}", params=params, timeout=30)
                if r.status_code == 429: time.sleep(4*(i+1)); continue
                return r.json() if r.status_code == 200 else None
            except Exception:
                time.sleep(2)
        return None


def count_location(token, location_id, since_ms):
    """Return activity counts for one location within the window."""
    api = Api(token)
    convos, params = [], {"locationId": location_id, "limit": 100}
    r = api.get("/conversations/search", params)
    while r and r.get("conversations"):
        convos.extend(r["conversations"])
        last = r["conversations"][-1]
        sort = last.get("sort") or [last.get("lastMessageDate")]
        p2 = dict(params); p2["startAfterDate"] = sort[0] if isinstance(sort, list) else sort
        r = api.get("/conversations/search", p2)
        if len(convos) > 8000: break

    texts = emails = opened = calls = 0

    def scan(c):
        rr = api.get(f"/conversations/{c['id']}/messages", {"limit": 100})
        return ((rr or {}).get("messages") or {}).get("messages", [])

    with ThreadPoolExecutor(8) as ex:
        for msgs in ex.map(scan, convos):
            for m in msgs:
                da = m.get("dateAdded") or ""
                # dateAdded is ISO; compare loosely by parsing
                try:
                    ts = datetime.fromisoformat(da.replace("Z", "+00:00")).timestamp()*1000
                except Exception:
                    ts = since_ms  # keep if unparseable
                if ts < since_ms:
                    continue
                mtype = (m.get("messageType") or m.get("type") or "").upper()
                direction = m.get("direction")
                if "SMS" in mtype and direction == "outbound":
                    texts += 1
                elif "EMAIL" in mtype and direction == "outbound":
                    emails += 1
                    st = (m.get("status") or "").lower()
                    if st in OPENED: opened += 1
                elif "CALL" in mtype and direction == "inbound":
                    calls += 1

    return {"textsSent": texts, "emailsSent": emails,
            "emailsOpened": opened, "callsTaken": calls}


def email_stats_totals():
    """Run the authoritative email puller (ghl-cli/email_stats_live.py) and return
    its totals dict {sent, delivered, opened, ...}, or None on failure."""
    import subprocess
    script = os.path.join(HERE, "..", "..", "ghl-cli", "email_stats_live.py")
    try:
        out = subprocess.run([sys.executable, script], capture_output=True,
                             text=True, timeout=600)
        return json.loads(out.stdout).get("totals")
    except Exception as e:
        log("email_stats_live failed:", e)
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()
    load_env()

    since_ms = (datetime.now(timezone.utc) - timedelta(days=args.days)).timestamp()*1000

    with open(CLIENTS_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    try:
        with open(DATA_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {"master": {}, "clients": [], "notes": []}

    # master = Wing Digital location
    master_token = os.environ.get("GHL_API_KEY")
    master_loc = os.environ.get("GHL_LOCATION_ID")
    if master_token and master_loc:
        log("counting master location...")
        m = count_location(master_token, master_loc, since_ms)
        # Emails: the conversation scan is windowed + can't see opens. The dedicated
        # email_stats_live.py walks every email thread and polls real status, so use
        # its authoritative all-time totals for sent/opened instead.
        em = email_stats_totals()
        if em:
            m["emailsSent"] = em.get("sent", m["emailsSent"])
            m["emailsOpened"] = em.get("opened", m["emailsOpened"])
        data.setdefault("master", {})
        data["master"].update(m)

    # per-client
    out_clients = []
    for c in cfg.get("clients", []):
        token = os.environ.get(c.get("pitEnv", ""))
        loc = os.environ.get(c.get("locationEnv", ""))
        prev = next((x for x in data.get("clients", []) if x["slug"] == c["slug"]), {})
        activity = {"textsSent": 0, "emailsSent": 0, "emailsOpened": 0, "callsTaken": 0}
        if token and loc:
            log(f"counting {c['slug']}...")
            activity = count_location(token, loc, since_ms)
        else:
            log(f"skip {c['slug']}: missing {c.get('pitEnv')}/{c.get('locationEnv')} in .env")
        out_clients.append({
            "slug": c["slug"], "name": c["name"], "trade": c.get("trade", ""),
            "city": c.get("city", ""), "activity": activity,
            "ads": prev.get("ads", {"accountId": c.get("adAccountId"),
                                    "queryable": False,
                                    "note": "Meta ad account not linked yet",
                                    "spend": 0, "impressions": 0, "clicks": 0,
                                    "leads": 0, "cpl": 0, "roas": 0, "days": args.days}),
        })
    if out_clients:
        data["clients"] = out_clients

    # roll master totals from clients where master location had nothing
    data["updatedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    log("wrote", DATA_PATH)


if __name__ == "__main__":
    main()
