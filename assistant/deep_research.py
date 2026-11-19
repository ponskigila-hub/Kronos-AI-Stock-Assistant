"""
Medium/long-horizon research signals for Kronos (not trade signals).

Layers:
  1. Earnings revisions / post-earnings drift cues (PEAD-lite)
  2. Relative valuation multiples vs peers and simple history bands
  3. Transparent single-stage / two-stage DCF sanity range
  4. Macro snapshot (rates, USD, equity benchmark)
  5. News/NLP-lite sentiment from the existing news lexicon

Performance:
  - One shared yfinance Ticker.info (+ earnings_history) per ticker, TTL-cached
  - Macro panel cached process-wide (slow to change; longer TTL)
  - Independent blocks run in parallel via ThreadPoolExecutor
  - Full insight response cached briefly per (ticker, peers, macro flag)

All outputs are research scenarios. Yahoo Finance fields can be missing or lag.
"""
from __future__ import annotations

import copy
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Caches (process-local, thread-safe)
# ---------------------------------------------------------------------------
_bundle_lock = threading.Lock()
_bundle_cache: dict[str, tuple[float, dict]] = {}
_BUNDLE_TTL = 300  # 5 minutes — fundamentals rarely need second-level freshness

_macro_lock = threading.Lock()
_macro_cache: tuple[float, dict] | None = None
_MACRO_TTL = 900  # 15 minutes

_insight_lock = threading.Lock()
_insight_cache: dict[str, tuple[float, dict]] = {}
_INSIGHT_TTL = 180  # 3 minutes — interactive re-runs feel instant
_INSIGHT_MAX = 64


def _n(value) -> Optional[float]:
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _pct(a, b) -> Optional[float]:
    a, b = _n(a), _n(b)
    if a is None or b is None or b == 0:
        return None
    return (a - b) / abs(b) * 100.0


def _get_ticker_bundle(ticker: str) -> dict:
    """Fetch Ticker.info + earnings_history once; reuse across earnings/DCF/multiples."""
    key = (ticker or "").strip().upper()
    now = time.time()
    with _bundle_lock:
        hit = _bundle_cache.get(key)
        if hit and (now - hit[0]) < _BUNDLE_TTL:
            return hit[1]

    info: dict = {}
    earnings_history = None
    error = None
    try:
        import yfinance as yf
        t = yf.Ticker(key)
        # .info is the expensive call; do it once
        info = t.info or {}
        try:
            earnings_history = t.earnings_history
        except Exception:
            earnings_history = None
    except Exception as e:
        error = str(e)

    bundle = {
        "ticker": key,
        "info": info,
        "earnings_history": earnings_history,
        "error": error,
        "fetched_at": now,
    }
    with _bundle_lock:
        _bundle_cache[key] = (now, bundle)
        # Soft bound so a long process scanning many names cannot grow forever
        if len(_bundle_cache) > 256:
            oldest = sorted(_bundle_cache.items(), key=lambda kv: kv[1][0])[:64]
            for k, _ in oldest:
                _bundle_cache.pop(k, None)
    return bundle


# ---------------------------------------------------------------------------
# Earnings revisions & PEAD-lite
# ---------------------------------------------------------------------------
def _earnings_revision_block(ticker: str, bundle: Optional[dict] = None) -> dict:
    """Best-effort PEAD / revision cues from yfinance (uses shared bundle when given)."""
    out = {
        "available": False,
        "last_report": None,
        "surprise_pct": None,
        "beat": None,
        "eps_trend": None,
        "revision_label": "unavailable",
        "pead_bias": "unknown",
        "notes": [],
    }
    bundle = bundle or _get_ticker_bundle(ticker)
    if bundle.get("error") and not bundle.get("info"):
        out["notes"].append(f"Could not load earnings fields: {bundle['error']}")
        return out

    info = bundle.get("info") or {}
    surprise_pct = None
    try:
        eh = bundle.get("earnings_history")
        if eh is not None and len(eh) > 0:
            row = eh.iloc[0]
            for col in ("surprisePercent", "surprise_pct", "pct_diff"):
                if col in eh.columns:
                    surprise_pct = _n(row.get(col) if hasattr(row, "get") else row[col])
                    break
            if surprise_pct is None:
                act = None
                est = None
                if hasattr(row, "get"):
                    act = _n(row.get("epsActual") or row.get("actual"))
                    est = _n(row.get("epsEstimate") or row.get("estimate"))
                else:
                    act = _n(row["epsActual"]) if "epsActual" in eh.columns else None
                    est = _n(row["epsEstimate"]) if "epsEstimate" in eh.columns else None
                if act is not None and est not in (None, 0):
                    surprise_pct = (act - est) / abs(est) * 100.0
            out["last_report"] = {
                "period": str(row.name) if hasattr(row, "name") else None,
                "surprise_pct": None if surprise_pct is None else round(surprise_pct, 2),
            }
            out["surprise_pct"] = None if surprise_pct is None else round(surprise_pct, 2)
            if surprise_pct is not None:
                out["beat"] = surprise_pct > 0
                out["available"] = True
    except Exception:
        pass

    growth = _n(info.get("earningsGrowth") or info.get("earningsQuarterlyGrowth"))
    rev_growth = _n(info.get("revenueGrowth"))
    target_mean = _n(info.get("targetMeanPrice"))
    price = _n(info.get("currentPrice") or info.get("regularMarketPrice"))
    rec = info.get("recommendationKey") or info.get("recommendationMean")

    trend = {
        "earnings_growth": growth,
        "revenue_growth": rev_growth,
        "recommendation": rec,
        "analyst_upside_pct": _pct(target_mean, price),
    }
    out["eps_trend"] = trend

    score = 0
    if surprise_pct is not None:
        if surprise_pct >= 5:
            score += 2
            out["notes"].append(f"Last reported EPS surprise ≈ {surprise_pct:+.1f}% (beat).")
        elif surprise_pct <= -5:
            score -= 2
            out["notes"].append(f"Last reported EPS surprise ≈ {surprise_pct:+.1f}% (miss).")
        else:
            out["notes"].append(f"Last reported EPS surprise ≈ {surprise_pct:+.1f}% (near consensus).")
    if growth is not None:
        if growth > 0.1:
            score += 1
        elif growth < -0.05:
            score -= 1
    upside = trend.get("analyst_upside_pct")
    if upside is not None:
        if upside > 10:
            score += 1
        elif upside < -10:
            score -= 1

    if score >= 2:
        out["revision_label"] = "constructive"
        out["pead_bias"] = "positive_drift_bias"
    elif score <= -2:
        out["revision_label"] = "deteriorating"
        out["pead_bias"] = "negative_drift_bias"
    elif out["available"] or growth is not None:
        out["revision_label"] = "mixed"
        out["pead_bias"] = "neutral"
        out["available"] = True
    else:
        out["notes"].append("Not enough earnings-revision fields from the data provider.")

    out["notes"].append(
        "PEAD here is a research heuristic (surprise + growth + analyst upside), "
        "not a formal event-study result."
    )
    return out


# ---------------------------------------------------------------------------
# Relative multiples
# ---------------------------------------------------------------------------
def _relative_multiples(
    ticker: str,
    peer_tickers: Optional[list] = None,
    bundle: Optional[dict] = None,
) -> dict:
    peers = peer_tickers or []
    if not peers:
        bundle = bundle or _get_ticker_bundle(ticker)
        info = bundle.get("info") or {}
        if bundle.get("error") and not info:
            return {"available": False, "error": bundle["error"], "company": {}, "peers": []}

        company = {
            "ticker": ticker.upper(),
            "pe_trailing": _n(info.get("trailingPE")),
            "pe_forward": _n(info.get("forwardPE")),
            "ev_ebitda": _n(info.get("enterpriseToEbitda")),
            "price_to_book": _n(info.get("priceToBook")),
            "price_to_sales": _n(info.get("priceToSalesTrailing12Months")),
            "fcf_yield": None,
            "sector": info.get("sector"),
        }
        fcf = _n(info.get("freeCashflow"))
        mcap = _n(info.get("marketCap"))
        if fcf and mcap and mcap > 0:
            company["fcf_yield"] = round(fcf / mcap * 100.0, 2)
        return {
            "available": True,
            "company": company,
            "peers": [],
            "vs_peers": None,
            "notes": [
                "No peers supplied — showing company multiples only. "
                "Add peers on the Valuation page for relative ranges."
            ],
        }

    try:
        from . import valuation as val_mod
        result = val_mod.fetch_relative_valuation(ticker, peers)
        return {
            "available": True,
            "relative_valuation": result,
            "notes": result.get("warnings") or [],
        }
    except Exception as e:
        return {"available": False, "error": str(e), "notes": [str(e)]}


# ---------------------------------------------------------------------------
# Transparent DCF (sanity range, not a bank model)
# ---------------------------------------------------------------------------
def simple_dcf(
    ticker: str,
    *,
    fcf: Optional[float] = None,
    shares: Optional[float] = None,
    growth_years: int = 5,
    growth_rate: Optional[float] = None,
    terminal_growth: float = 0.025,
    wacc: Optional[float] = None,
    net_debt: Optional[float] = None,
    bundle: Optional[dict] = None,
) -> dict:
    """Two-stage FCF DCF with explicit assumptions and a sensitivity band."""
    notes = []
    if bundle is None:
        bundle = _get_ticker_bundle(ticker)
    info = bundle.get("info") or {}
    if bundle.get("error") and not info:
        return {"available": False, "error": bundle["error"], "notes": [bundle["error"]]}

    fcf = _n(fcf) or _n(info.get("freeCashflow"))
    shares = _n(shares) or _n(info.get("sharesOutstanding"))
    price = _n(info.get("currentPrice") or info.get("regularMarketPrice"))
    debt = _n(info.get("totalDebt")) or 0.0
    cash = _n(info.get("totalCash")) or 0.0
    if net_debt is None:
        net_debt = debt - cash

    if growth_rate is None:
        growth_rate = _n(info.get("earningsGrowth")) or _n(info.get("revenueGrowth")) or 0.06
        growth_rate = max(-0.05, min(0.25, growth_rate))
        notes.append(f"Growth defaulted from provider fields / 6% floor-cap → {growth_rate:.1%}.")
    if wacc is None:
        beta = _n(info.get("beta")) or 1.0
        wacc = 0.04 + beta * 0.05
        wacc = max(0.06, min(0.14, wacc))
        notes.append(f"WACC approximated from beta ({beta:.2f}) → {wacc:.1%} (not company-specific CoD).")

    if not fcf or fcf <= 0 or not shares or shares <= 0:
        return {
            "available": False,
            "notes": notes + ["Need positive free cash flow and shares outstanding for DCF."],
            "assumptions": {
                "growth_rate": growth_rate,
                "terminal_growth": terminal_growth,
                "wacc": wacc,
            },
        }

    if terminal_growth >= wacc:
        terminal_growth = max(0.0, wacc - 0.01)
        notes.append("Terminal growth capped below WACC.")

    def _value(g, w, tg):
        pv = 0.0
        cf = fcf
        for yr in range(1, growth_years + 1):
            cf *= 1.0 + g
            pv += cf / ((1.0 + w) ** yr)
        terminal_cf = cf * (1.0 + tg)
        terminal_val = terminal_cf / (w - tg)
        pv += terminal_val / ((1.0 + w) ** growth_years)
        equity = pv - (net_debt or 0.0)
        return equity / shares

    base = _value(growth_rate, wacc, terminal_growth)
    low = _value(max(-0.02, growth_rate - 0.03), wacc + 0.015, max(0.0, terminal_growth - 0.005))
    high = _value(
        min(0.28, growth_rate + 0.03),
        max(0.055, wacc - 0.01),
        min(wacc - 0.005, terminal_growth + 0.005),
    )

    upside = _pct(base, price) if price else None
    return {
        "available": True,
        "per_share": {
            "base": round(base, 2),
            "low": round(min(low, high), 2),
            "high": round(max(low, high), 2),
        },
        "current_price": price,
        "upside_vs_price_pct": None if upside is None else round(upside, 1),
        "assumptions": {
            "fcf": fcf,
            "shares": shares,
            "net_debt": net_debt,
            "growth_years": growth_years,
            "growth_rate": round(growth_rate, 4),
            "terminal_growth": round(terminal_growth, 4),
            "wacc": round(wacc, 4),
        },
        "notes": notes + [
            "DCF is a transparent sanity range, not a bank-grade intrinsic value. "
            "Small changes in WACC/growth move results a lot."
        ],
    }


# ---------------------------------------------------------------------------
# Macro snapshot (shared cache — same for every ticker)
# ---------------------------------------------------------------------------
def macro_snapshot() -> dict:
    """Lightweight macro panel via public Yahoo symbols (TTL-cached)."""
    global _macro_cache
    now = time.time()
    with _macro_lock:
        if _macro_cache and (now - _macro_cache[0]) < _MACRO_TTL:
            return copy.deepcopy(_macro_cache[1])

    symbols = {
        "us_10y": "^TNX",
        "us_equity": "SPY",
        "usd": "DX-Y.NYB",
        "gold": "GC=F",
        "oil": "CL=F",
    }
    rows = {}
    notes = []
    try:
        import yfinance as yf
        # 3mo is enough for 1m/3m changes and is faster than 6mo
        data = yf.download(
            list(symbols.values()),
            period="3mo",
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as e:
        result = {"available": False, "rows": {}, "notes": [str(e)]}
        with _macro_lock:
            _macro_cache = (now, result)
        return copy.deepcopy(result)

    for name, sym in symbols.items():
        try:
            close = None
            if hasattr(data.columns, "levels"):
                try:
                    close = data[sym]["Close"].dropna()
                except Exception:
                    try:
                        close = data["Close"][sym].dropna()
                    except Exception:
                        close = None
            else:
                close = data["Close"].dropna() if "Close" in data else None
            if close is None or len(close) < 25:
                continue
            last = float(close.iloc[-1])
            d1 = float(close.pct_change().iloc[-1]) * 100
            d21 = float(close.iloc[-1] / close.iloc[-22] - 1) * 100 if len(close) >= 22 else None
            d63 = float(close.iloc[-1] / close.iloc[-64] - 1) * 100 if len(close) >= 64 else None
            rows[name] = {
                "symbol": sym,
                "last": round(last, 4),
                "chg_1d_pct": round(d1, 2),
                "chg_1m_pct": None if d21 is None else round(d21, 2),
                "chg_3m_pct": None if d63 is None else round(d63, 2),
            }
        except Exception:
            continue

    if not rows:
        notes.append("Macro series unavailable from provider right now.")
        result = {"available": False, "rows": {}, "notes": notes}
    else:
        tags = []
        eq = rows.get("us_equity")
        if eq and eq.get("chg_1m_pct") is not None:
            tags.append("equity_risk_on" if eq["chg_1m_pct"] > 0 else "equity_soft")
        yld = rows.get("us_10y")
        if yld and yld.get("chg_1m_pct") is not None:
            tags.append("yields_up" if yld["chg_1m_pct"] > 0 else "yields_down")
        notes.append(
            "Macro panel is a desk snapshot (SPY, USD, 10Y proxy, commodities) — "
            "not a full sector-rotation model."
        )
        result = {"available": True, "rows": rows, "tags": tags, "notes": notes}

    with _macro_lock:
        _macro_cache = (now, result)
    return copy.deepcopy(result)


# ---------------------------------------------------------------------------
# Sentiment (reuses news lexicon)
# ---------------------------------------------------------------------------
def sentiment_block(ticker: str) -> dict:
    try:
        from . import news as news_mod
        items, summary = news_mod.get_news(ticker)
    except Exception as e:
        return {"available": False, "notes": [str(e)], "summary": None, "headlines": []}

    headlines = []
    for it in (items or [])[:6]:
        headlines.append({
            "title": it.get("title") or it.get("headline"),
            "publisher": it.get("publisher"),
            "score": it.get("score"),
        })
    return {
        "available": summary is not None,
        "summary": summary,
        "headlines": headlines,
        "notes": [
            "Sentiment is lexicon/NLP-lite over recent headlines (not FinBERT on full transcripts). "
            "Tone can diverge from price."
        ],
    }


# ---------------------------------------------------------------------------
# Orchestrator (parallel + result cache)
# ---------------------------------------------------------------------------
def _insight_key(ticker: str, peers: list, include_macro: bool) -> str:
    peer_part = ",".join(sorted(peers))
    return f"{ticker}|{peer_part}|{int(bool(include_macro))}"


def build_fundamental_insight(
    ticker: str,
    peer_tickers: Optional[list] = None,
    include_macro: bool = True,
) -> dict:
    ticker = (ticker or "").strip().upper()
    if not ticker:
        raise ValueError("ticker is required")

    peers = []
    for p in peer_tickers or []:
        s = str(p).strip().upper()
        if s and s != ticker and s not in peers:
            peers.append(s)

    cache_key = _insight_key(ticker, peers, include_macro)
    now = time.time()
    with _insight_lock:
        hit = _insight_cache.get(cache_key)
        if hit and (now - hit[0]) < _INSIGHT_TTL:
            return copy.deepcopy(hit[1])

    # Shared fundamentals payload — one network round-trip for info
    bundle = _get_ticker_bundle(ticker)

    earnings = dcf = relative = sentiment = None
    macro = {"available": False, "rows": {}, "notes": []}

    # Parallelize independent I/O: relative (peers), sentiment (news), macro
    # Earnings + DCF are pure CPU on the shared bundle (fast) — run inline.
    earnings = _earnings_revision_block(ticker, bundle=bundle)
    dcf = simple_dcf(ticker, bundle=bundle)

    futures = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures[pool.submit(_relative_multiples, ticker, peers, bundle if not peers else None)] = "relative"
        futures[pool.submit(sentiment_block, ticker)] = "sentiment"
        if include_macro:
            futures[pool.submit(macro_snapshot)] = "macro"

        relative = {"available": False, "notes": ["relative multiples failed"]}
        sentiment = {"available": False, "summary": None, "headlines": [], "notes": []}
        for fut in as_completed(futures):
            label = futures[fut]
            try:
                val = fut.result()
            except Exception as e:
                val = {"available": False, "notes": [str(e)]}
            if label == "relative":
                relative = val
            elif label == "sentiment":
                sentiment = val
            elif label == "macro":
                macro = val

    score = 0.0
    weights_used = []
    if earnings.get("revision_label") == "constructive":
        score += 1
        weights_used.append("earnings+")
    elif earnings.get("revision_label") == "deteriorating":
        score -= 1
        weights_used.append("earnings-")
    if dcf.get("available") and dcf.get("upside_vs_price_pct") is not None:
        u = dcf["upside_vs_price_pct"]
        if u > 15:
            score += 1
            weights_used.append("dcf+")
        elif u < -15:
            score -= 1
            weights_used.append("dcf-")
    lab = (sentiment.get("summary") or {}).get("label")
    if lab == "mostly positive":
        score += 0.5
        weights_used.append("news+")
    elif lab == "mostly negative":
        score -= 0.5
        weights_used.append("news-")

    if score >= 1.5:
        stance = "fundamentally constructive"
    elif score <= -1.5:
        stance = "fundamentally cautious"
    else:
        stance = "mixed / needs judgment"

    result = {
        "ticker": ticker,
        "retrieved_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "stance": stance,
        "stance_drivers": weights_used,
        "earnings": earnings,
        "relative": relative,
        "dcf": dcf,
        "sentiment": sentiment,
        "macro": macro,
        "disclaimer": (
            "Research synthesis only — not a recommendation to buy or sell. "
            "DCF and PEAD heuristics are simplified; provider data may be incomplete."
        ),
        "perf": {
            "bundle_cached": (time.time() - bundle.get("fetched_at", now)) > 0.05
            and (now - bundle.get("fetched_at", now)) < _BUNDLE_TTL,
            "insight_cache_ttl_sec": _INSIGHT_TTL,
            "macro_cache_ttl_sec": _MACRO_TTL if include_macro else 0,
        },
    }

    with _insight_lock:
        _insight_cache[cache_key] = (time.time(), result)
        if len(_insight_cache) > _INSIGHT_MAX:
            oldest = sorted(_insight_cache.items(), key=lambda kv: kv[1][0])[:16]
            for k, _ in oldest:
                _insight_cache.pop(k, None)

    return copy.deepcopy(result)


def clear_caches() -> None:
    """Test helper / manual flush."""
    global _macro_cache
    with _bundle_lock:
        _bundle_cache.clear()
    with _macro_lock:
        _macro_cache = None
    with _insight_lock:
        _insight_cache.clear()
