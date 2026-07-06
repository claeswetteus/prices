#!/usr/bin/env python3
"""
aktier — pris-hämtare (Avanza + Frankfurter, ingen Yahoo).

Aktier  -> Avanza market-guide/stock/{id} (kurs) + price-chart/stock/{id} (historik)
Fonder  -> Avanza fund-reference/reference/{id} (NAV) + fund-guide/chart (historik)
Valuta  -> Frankfurter (ECB-dagskurser, ingen nyckel)
Skriver till Supabase-schemat 'aktier': prices, quotes, fx_rates.

Miljövariabler (kan ligga i .env bredvid scriptet):
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, BACKFILL (true=~1 års historik)
"""
import os
import time
import datetime as dt
import requests


def _load_dotenv(path):
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SERVICE_KEY  = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
SCHEMA       = "aktier"
BACKFILL     = os.environ.get("BACKFILL", "false").lower() == "true"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}

AV_STOCK      = "https://www.avanza.se/_api/market-guide/stock/{id}"
AV_STOCK_CH   = "https://www.avanza.se/_api/price-chart/stock/{id}"
AV_FUND       = "https://www.avanza.se/_api/fund-reference/reference/{id}"
AV_FUND_CH    = "https://www.avanza.se/_api/fund-guide/chart/{id}/{period}"
AV_INDEX      = "https://www.avanza.se/_api/market-index/{id}"
FRANKFURTER   = "https://api.frankfurter.app"

# Marknadsindex som visas i appens header (id = Avanza orderbookId)
INDEXES = [("18988", "OMX Stockholm PI")]

STOCK_PERIOD  = "one_year"       # för historik-backfill (aktier)
FUND_PERIOD   = "one_year"
PAUSE         = 0.3
NOW_ISO       = dt.datetime.now(dt.timezone.utc).isoformat()  # när kurserna hämtades


def utc_date(ms_or_s, is_ms=True):
    ts = ms_or_s / 1000 if is_ms else ms_or_s
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat()


# ----------------------------- Supabase ---------------------------------
def sb_headers(write=False):
    h = {"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}"}
    if write:
        h["Content-Type"]    = "application/json"
        h["Content-Profile"] = SCHEMA
        h["Prefer"]          = "resolution=merge-duplicates,return=minimal"
    else:
        h["Accept-Profile"]  = SCHEMA
    return h


def sb_get(table, params):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}",
                     headers=sb_headers(), params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def sb_upsert(table, rows, on_conflict):
    if not rows:
        return
    for i in range(0, len(rows), 500):
        chunk = rows[i:i + 500]
        r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}",
                          headers=sb_headers(write=True),
                          params={"on_conflict": on_conflict},
                          json=chunk, timeout=60)
        if r.status_code >= 300:
            print(f"  ! upsert {table} misslyckades {r.status_code}: {r.text[:300]}")
            r.raise_for_status()


# ----------------------------- Avanza: aktier ----------------------------
def avanza_stock_quote(oid):
    r = requests.get(AV_STOCK.format(id=oid), headers=UA, timeout=30)
    r.raise_for_status()
    q = r.json().get("quote") or {}
    last = q.get("last")
    chg = q.get("changePercent")
    prev = None
    if last is not None and chg is not None:
        prev = round(last / (1 + chg / 100.0), 4)
    return last, prev


def avanza_stock_hist(oid):
    try:
        r = requests.get(AV_STOCK_CH.format(id=oid), headers=UA,
                         params={"timePeriod": STOCK_PERIOD}, timeout=30)
        r.raise_for_status()
        ohlc = r.json().get("ohlc") or []
    except Exception as e:
        print(f"  ! aktiehistorik {oid}: {e}")
        return []
    out = []
    for p in ohlc:
        c = p.get("close")
        t = p.get("timestamp")
        if c is None or t is None:
            continue
        out.append((utc_date(t), round(float(c), 4)))
    return out


# ----------------------------- Avanza: fonder ----------------------------
def avanza_nav(oid):
    r = requests.get(AV_FUND.format(id=oid), headers=UA, timeout=30)
    r.raise_for_status()
    j = r.json()
    return j.get("nav"), (j.get("navDate") or "")[:10]


def avanza_fund_hist(oid, nav_now):
    try:
        r = requests.get(AV_FUND_CH.format(id=oid, period=FUND_PERIOD),
                         headers=UA, timeout=30)
        r.raise_for_status()
        serie = r.json().get("dataSerie") or []
    except Exception as e:
        print(f"  ! fondhistorik {oid}: {e}")
        return []
    if not serie or nav_now is None:
        return []
    y_last = serie[-1].get("y", 0.0)
    base = nav_now / (1 + y_last / 100.0)
    out = []
    for p in serie:
        if p.get("y") is None:
            continue
        out.append((utc_date(p["x"]), round(base * (1 + p["y"] / 100.0), 4)))
    return out


# ----------------------------- Avanza: index -----------------------------
def avanza_index(oid):
    r = requests.get(AV_INDEX.format(id=oid), headers=UA, timeout=30)
    r.raise_for_status()
    j = r.json()
    q = j.get("quote") or {}
    return j.get("name"), q.get("last"), q.get("changePercent")


# ----------------------------- Frankfurter (FX) --------------------------
def fx_latest(ccy):
    r = requests.get(f"{FRANKFURTER}/latest", params={"from": ccy, "to": "SEK"}, timeout=30)
    r.raise_for_status()
    return r.json().get("rates", {}).get("SEK")


def fx_hist(ccy, start):
    r = requests.get(f"{FRANKFURTER}/{start}..", params={"from": ccy, "to": "SEK"}, timeout=30)
    r.raise_for_status()
    rates = r.json().get("rates", {})
    return [(d, v.get("SEK")) for d, v in sorted(rates.items()) if v.get("SEK") is not None]


# ----------------------------- Main --------------------------------------
def main():
    print(f"BACKFILL={BACKFILL}")
    instruments = sb_get("instruments", {
        "select": "id,name,type,currency,price_source,yahoo_ticker,avanza_orderbook_id"
    })
    print(f"{len(instruments)} instrument.")

    prices, quotes = [], []
    currencies = set()

    for ins in instruments:
        currencies.add(ins["currency"])
        oid = ins.get("avanza_orderbook_id")
        try:
            if ins["type"] == "aktie":
                if not oid:
                    print(f"  ! {ins['name']}: saknar avanza_orderbook_id — hoppar")
                    continue
                last, prev = avanza_stock_quote(oid)
                today = dt.date.today().isoformat()
                if last is not None:
                    quotes.append({"instrument_id": ins["id"], "price": last,
                                   "prev_close": prev, "as_of": NOW_ISO})
                    prices.append({"instrument_id": ins["id"], "date": today, "close": last})
                if BACKFILL:
                    for d, c in avanza_stock_hist(oid):
                        prices.append({"instrument_id": ins["id"], "date": d, "close": c})
                print(f"  {ins['name']}: {last}")

            elif ins["type"] == "fond":
                nav, navdate = avanza_nav(oid)
                hist = avanza_fund_hist(oid, nav) if BACKFILL else []
                prev = None
                earlier = [c for (dd, c) in hist if dd < navdate]
                if earlier:
                    prev = earlier[-1]
                if nav is not None and navdate:
                    prices.append({"instrument_id": ins["id"], "date": navdate, "close": nav})
                    for d, c in hist:
                        prices.append({"instrument_id": ins["id"], "date": d, "close": c})
                    quotes.append({"instrument_id": ins["id"], "price": nav,
                                   "prev_close": prev, "as_of": NOW_ISO})
                    print(f"  {ins['name']}: NAV {nav} ({navdate})")
            time.sleep(PAUSE)
        except Exception as e:
            print(f"  ! {ins['name']} misslyckades: {e}")

    dedup = {(p["instrument_id"], p["date"]): p for p in prices}
    prices = list(dedup.values())
    sb_upsert("prices", prices, "instrument_id,date")
    sb_upsert("quotes", quotes, "instrument_id")
    print(f"Skrev {len(prices)} priser, {len(quotes)} quotes.")

    # ---- FX via Frankfurter ----
    fx = []
    start = (dt.date.today() - dt.timedelta(days=370)).isoformat()
    for ccy in sorted(currencies):
        if ccy == "SEK":
            continue
        try:
            pair = f"{ccy}SEK"
            if BACKFILL:
                for d, rate in fx_hist(ccy, start):
                    fx.append({"pair": pair, "date": d, "rate": round(float(rate), 6)})
            latest = fx_latest(ccy)
            if latest is not None:
                fx.append({"pair": pair, "date": dt.date.today().isoformat(),
                           "rate": round(float(latest), 6)})
            print(f"  FX {pair}: {latest}")
            time.sleep(PAUSE)
        except Exception as e:
            print(f"  ! FX {ccy} misslyckades: {e}")

    dedup = {(f["pair"], f["date"]): f for f in fx}
    fx = list(dedup.values())
    sb_upsert("fx_rates", fx, "pair,date")
    print(f"Skrev {len(fx)} FX-rader. Klart.")

    # ---- Marknadsindex (OMXSPI m.fl.) ----
    idx_rows = []
    for oid, fallback in INDEXES:
        try:
            name, last, chg = avanza_index(oid)
            if last is not None:
                idx_rows.append({"id": oid, "name": name or fallback,
                                 "price": last, "change_pct": chg, "as_of": NOW_ISO})
                print(f"  Index {name or fallback}: {last} ({chg}%)")
            time.sleep(PAUSE)
        except Exception as e:
            print(f"  ! index {oid} misslyckades: {e}")
    sb_upsert("market_index", idx_rows, "id")
    print(f"Skrev {len(idx_rows)} index.")


if __name__ == "__main__":
    main()
