#!/usr/bin/env python3
"""
aktier — pris-hämtare.

Aktier + FX  -> Yahoo v8 chart-endpoint (live-kurs + dagshistorik)
Fonder (NAV) -> Avanza publika fond-API (färskt NAV + historik via % -> absolut)
Skriver till Supabase-schemat 'aktier': prices, quotes, fx_rates.

Miljövariabler:
  SUPABASE_URL                 t.ex. https://ntgqubstkkwqynikhwhc.supabase.co
  SUPABASE_SERVICE_ROLE_KEY    service_role-nyckeln (kringgår RLS) — HEMLIG
  BACKFILL                     "true" = hämta ~1 års historik, annars bara senaste
"""
import os
import sys
import time
import datetime as dt
import requests

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SERVICE_KEY  = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
SCHEMA       = "aktier"
BACKFILL     = os.environ.get("BACKFILL", "false").lower() == "true"

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}

YF_CHART     = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
AVANZA_REF   = "https://www.avanza.se/_api/fund-reference/reference/{id}"
AVANZA_CHART = "https://www.avanza.se/_api/fund-guide/chart/{id}/{period}"

# valuta -> Yahoo FX-symbol (kurs uttryckt i SEK per 1 enhet)
FX_SYMBOL = {"USD": "SEK=X", "NOK": "NOKSEK=X", "EUR": "EURSEK=X"}

STOCK_RANGE   = "1y" if BACKFILL else "5d"
FUND_PERIOD   = "one_year"          # bevisat token; ger ~1 års historik
REQUEST_PAUSE = 0.4                  # sekunder mellan externa anrop


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
    # skicka i lagom stora batchar
    for i in range(0, len(rows), 500):
        chunk = rows[i:i + 500]
        r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}",
                          headers=sb_headers(write=True),
                          params={"on_conflict": on_conflict},
                          json=chunk, timeout=60)
        if r.status_code >= 300:
            print(f"  ! upsert {table} misslyckades {r.status_code}: {r.text[:300]}")
            r.raise_for_status()


# ----------------------------- Yahoo -------------------------------------
def yahoo_fetch(sym, rng):
    """Returnerar dict: price, prev_close, as_of (ISO), hist [(date, close)]."""
    last_err = None
    for attempt in range(2):
        try:
            r = requests.get(YF_CHART.format(sym=sym), headers=UA,
                             params={"interval": "1d", "range": rng}, timeout=30)
            r.raise_for_status()
            res = r.json()["chart"]["result"][0]
            break
        except Exception as e:
            last_err = e
            time.sleep(1.0)
    else:
        raise last_err

    m  = res["meta"]
    ts = res.get("timestamp") or []
    closes = (res.get("indicators", {}).get("quote", [{}])[0].get("close")
              if ts else None) or []
    hist = []
    for t, c in zip(ts, closes):
        if c is None:
            continue
        d = dt.datetime.utcfromtimestamp(t).date().isoformat()
        hist.append((d, round(float(c), 4)))

    as_of = None
    if m.get("regularMarketTime"):
        as_of = dt.datetime.utcfromtimestamp(m["regularMarketTime"]).isoformat() + "Z"

    return {"price": m.get("regularMarketPrice"),
            "prev_close": m.get("chartPreviousClose"),
            "as_of": as_of,
            "hist": hist}


# ----------------------------- Avanza ------------------------------------
def avanza_nav(oid):
    r = requests.get(AVANZA_REF.format(id=oid), headers=UA, timeout=30)
    r.raise_for_status()
    j = r.json()
    nav = j.get("nav")
    navdate = (j.get("navDate") or "")[:10]
    return nav, navdate


def avanza_hist(oid, nav_now):
    """Avanza ger %-utveckling; ankra mot aktuell NAV -> absolut NAV per dag."""
    try:
        r = requests.get(AVANZA_CHART.format(id=oid, period=FUND_PERIOD),
                         headers=UA, timeout=30)
        r.raise_for_status()
        serie = r.json().get("dataSerie") or []
    except Exception as e:
        print(f"  ! avanza-historik {oid}: {e}")
        return []
    if not serie or nav_now is None:
        return []
    y_last = serie[-1].get("y", 0.0)
    base = nav_now / (1 + y_last / 100.0)
    out = []
    for p in serie:
        if p.get("y") is None:
            continue
        d = dt.datetime.utcfromtimestamp(p["x"] / 1000).date().isoformat()
        out.append((d, round(base * (1 + p["y"] / 100.0), 4)))
    return out


# ----------------------------- Main --------------------------------------
def main():
    print(f"BACKFILL={BACKFILL}  stock_range={STOCK_RANGE}")
    instruments = sb_get("instruments", {
        "select": "id,name,type,currency,price_source,yahoo_ticker,avanza_orderbook_id"
    })
    print(f"{len(instruments)} instrument.")

    prices, quotes = [], []
    currencies = set()

    for ins in instruments:
        currencies.add(ins["currency"])
        try:
            if ins["price_source"] == "yahoo":
                d = yahoo_fetch(ins["yahoo_ticker"], STOCK_RANGE)
                if d["price"] is not None:
                    quotes.append({"instrument_id": ins["id"], "price": d["price"],
                                   "prev_close": d["prev_close"], "as_of": d["as_of"]})
                for date, close in d["hist"]:
                    prices.append({"instrument_id": ins["id"], "date": date, "close": close})
                print(f"  {ins['name']}: {d['price']} ({len(d['hist'])} hist)")

            elif ins["price_source"] == "avanza":
                nav, navdate = avanza_nav(ins["avanza_orderbook_id"])
                hist = avanza_hist(ins["avanza_orderbook_id"], nav) if BACKFILL else []
                prev = None
                earlier = [c for (dd, c) in hist if dd < navdate]
                if earlier:
                    prev = earlier[-1]
                if nav is not None and navdate:
                    prices.append({"instrument_id": ins["id"], "date": navdate, "close": nav})
                    for date, close in hist:
                        prices.append({"instrument_id": ins["id"], "date": date, "close": close})
                    quotes.append({"instrument_id": ins["id"], "price": nav,
                                   "prev_close": prev, "as_of": navdate + "T00:00:00Z"})
                    print(f"  {ins['name']}: NAV {nav} ({navdate}, {len(hist)} hist)")
            time.sleep(REQUEST_PAUSE)
        except Exception as e:
            print(f"  ! {ins['name']} misslyckades: {e}")

    # deduplicera priser på (instrument_id, date) — behåll sista
    pd = {(p["instrument_id"], p["date"]): p for p in prices}
    prices = list(pd.values())

    sb_upsert("prices", prices, "instrument_id,date")
    sb_upsert("quotes", quotes, "instrument_id")
    print(f"Skrev {len(prices)} priser, {len(quotes)} quotes.")

    # ---- FX (endast valutor som faktiskt används) ----
    fx = []
    for ccy in sorted(currencies):
        if ccy == "SEK":
            continue
        sym = FX_SYMBOL.get(ccy)
        if not sym:
            print(f"  ! saknar FX-symbol för {ccy}")
            continue
        try:
            d = yahoo_fetch(sym, STOCK_RANGE)
            pair = f"{ccy}SEK"
            for date, rate in d["hist"]:
                fx.append({"pair": pair, "date": date, "rate": rate})
            if d["price"] is not None:
                fx.append({"pair": pair, "date": dt.date.today().isoformat(),
                           "rate": round(float(d["price"]), 6)})
            print(f"  FX {pair}: {d['price']}")
            time.sleep(REQUEST_PAUSE)
        except Exception as e:
            print(f"  ! FX {ccy} misslyckades: {e}")

    fxd = {(f["pair"], f["date"]): f for f in fx}
    fx = list(fxd.values())
    sb_upsert("fx_rates", fx, "pair,date")
    print(f"Skrev {len(fx)} FX-rader. Klart.")


if __name__ == "__main__":
    main()
