#!/usr/bin/env python3
"""
IVY11_GRRM_V1 — Full Risk Snapshot Builder V2

V6 policy:
- diagnostic market-regime output only
- NO regime-based portfolio/cash allocation
- computes diagnostic Risk Pressure 0–100
- exposes the canonical Boom-&-Bust image URL

Risk Pressure V6
----------------
This is a DIAGNOSTIC composite, not a trading rule and not a crash probability.

score =
    45% BASE VP
  + 25% MSP Market
  + 20% MSP Credit CONTROL
  + 10% MSP Volatility

Rationale:
- BASE VP = slow structural vulnerability
- Market/Credit/Vol = current observable stress
- weights preserve the intended emphasis on market trend, credit and stress while
  keeping structural vulnerability visible.

The prior conceptual six-block Risk Pressure was never frozen as a validated
capital-action rule. V6 therefore uses this explicit, reproducible runtime score
for diagnosis only.

Month-end comparison:
- Market/Credit/Vol use the latest frozen action-anchor row on or before the
  previous calendar month-end.
- BASE VP is slow-moving and the current valid BASE VP is held constant for the
  comparison if no historical VP is present in the action anchor.
"""
from __future__ import annotations

from pathlib import Path
import json
import pandas as pd

HERE = Path(__file__).resolve().parents[1]
FAST = HERE / "docs/latest_fast_snapshot.json"
BASE = HERE / "data/IVY11_GRRM_V1-GX_BASE_Diagnostic_Anchor_2026-10-02.csv"
ACTION_ANCHOR = HERE / "data/IVY11_GRRM_V1-GQ_Live_Action_Anchor.csv"
OUT_JSON = HERE / "docs/latest_full_risk_snapshot.json"
OUT_MD = HERE / "docs/latest_full_risk_snapshot.md"

SCHEMA = "IVY11_GRRM_V1_FULL_RISK_V2"
IMAGE_URL = (
    "https://raw.githubusercontent.com/oliverdejong0-sys/"
    "IVY11_GRRM_V1_RUNTIME/main/docs/"
    "IVY11_GRRM_V1-Boom_Bust_7_Regime_V6_FINAL.png"
)

REGIMES = {
    1: "Kapitulation / Bodenbildung",
    2: "Frühe Erholung",
    3: "Gesunder Bullenmarkt",
    4: "Reifer Bullenmarkt",
    5: "Überhitzung / Fragilität",
    6: "Stress / beginnender Bärenmarkt",
    7: "Systemischer Crash",
}


def classify_pressure(score: float) -> str:
    if score <= 25:
        return "NIEDRIG"
    if score <= 45:
        return "NORMAL"
    if score <= 60:
        return "ERHÖHT"
    if score <= 75:
        return "HOCH"
    if score <= 90:
        return "SEHR HOCH"
    return "AKUTER SYSTEMISCHER STRESS"


def risk_pressure(vp: float, market: float, credit: float, vol: float) -> float:
    vals = [vp, market, credit, vol]
    if any(pd.isna(x) for x in vals):
        raise RuntimeError("Risk Pressure inputs contain NaN.")
    score = 0.45 * float(vp) + 0.25 * float(market) + 0.20 * float(credit) + 0.10 * float(vol)
    return max(0.0, min(100.0, float(score)))


def previous_month_end_reference(target: pd.Timestamp, vp_current: float):
    if not ACTION_ANCHOR.exists():
        return None

    a = pd.read_csv(ACTION_ANCHOR)
    if "Date" not in a.columns:
        return None

    needed = ["MSP_MARKET", "MSP_CREDIT_CONTROL", "MSP_VOL"]
    if any(c not in a.columns for c in needed):
        return None

    a["Date"] = pd.to_datetime(a["Date"], errors="coerce")
    for c in needed:
        a[c] = pd.to_numeric(a[c], errors="coerce")

    prev_end = target.replace(day=1) - pd.Timedelta(days=1)
    q = a[(a["Date"] <= prev_end) & a[needed].notna().all(axis=1)].sort_values("Date")
    if q.empty:
        return None

    r = q.iloc[-1]
    vp_ref = vp_current
    vp_source = "CURRENT_VALID_BASE_VP_HELD_CONSTANT"
    if "VP" in a.columns:
        v = pd.to_numeric(pd.Series([r.get("VP")]), errors="coerce").iloc[0]
        if pd.notna(v):
            vp_ref = float(v)
            vp_source = "HISTORICAL_ANCHOR_VP"

    score = risk_pressure(
        vp_ref,
        float(r["MSP_MARKET"]),
        float(r["MSP_CREDIT_CONTROL"]),
        float(r["MSP_VOL"]),
    )
    return {
        "reference_date": pd.Timestamp(r["Date"]).date().isoformat(),
        "score": score,
        "classification": classify_pressure(score),
        "vp_source": vp_source,
    }


def main():
    fast = json.loads(FAST.read_text(encoding="utf-8"))
    if fast.get("schema") != "IVY11_GRRM_V1_FAST_DAILY_V1":
        raise RuntimeError("Invalid FAST DAILY schema.")
    if "SHADOW" not in fast.get("action_authority", ""):
        raise RuntimeError("FAST DAILY action authority is not SHADOW.")

    values = fast.get("values", {})
    required = [
        "MSP_MARKET", "MSP_VOL", "MSP_CREDIT_CONTROL", "CLEAN3F_VOTE_PCT",
        "CLEAN3F_STRESS", "FAST_SENTINEL_65_70", "SENTINEL_70_75",
        "EQ_MA200_GAP_PCT", "EQ_DRAWDOWN_PCT", "EQ_RET_5D_PCT", "EQ_RET_20D_PCT",
        "D200", "MODE",
    ]
    missing = [x for x in required if x not in values]
    if missing:
        raise RuntimeError(f"FAST DAILY missing fields: {missing}")

    b = pd.read_csv(BASE)
    if len(b) != 1:
        raise RuntimeError("BASE anchor must contain exactly one row.")
    br = b.iloc[0]

    target = pd.Timestamp(fast["target_market_date"]).normalize()
    base_date = pd.Timestamp(br["Date"]).normalize()
    stale_days = int((target - base_date).days)
    mode = str(values["MODE"])
    fast_alert = str(values["FAST_SENTINEL_65_70"]) == "ALERT"
    clean_stress = bool(values["CLEAN3F_STRESS"])
    calm = mode == "NORMAL MODE" and not fast_alert and not clean_stress

    if stale_days < 0:
        raise RuntimeError("BASE date is after FAST target date.")

    if base_date == target:
        base_status = "CURRENT"
        base_valid = True
    elif calm and stale_days <= 7:
        base_status = "VALID_STALE_WITHIN_CALM_POLICY"
        base_valid = True
    else:
        base_status = "BASE_REFRESH_REQUIRED"
        base_valid = False

    regime = int(br["BASE_REGIME"])
    vp = float(br["VP"])
    msp = float(br["MSP"])
    market = float(values["MSP_MARKET"])
    credit = float(values["MSP_CREDIT_CONTROL"])
    vol = float(values["MSP_VOL"])

    rp_now = risk_pressure(vp, market, credit, vol)
    rp_prev = previous_month_end_reference(target, vp)
    rp_delta = None if rp_prev is None else rp_now - float(rp_prev["score"])

    result = {
        "schema": SCHEMA,
        "runtime_revision": "V6.3_CACHE_SAFE",
        "target_market_date": fast["target_market_date"],
        "generated_utc": fast["generated_utc"],
        "authority": "DIAGNOSTIC_ONLY / NO_PORTFOLIO_ACTION",
        "base_policy": {
            "base_data_date": base_date.date().isoformat(),
            "base_staleness_calendar_days": stale_days,
            "calm_mode_max_staleness_days": 7,
            "alert_or_stress_requires_same_date_base": True,
            "status": base_status,
            "base_valid_for_management": base_valid,
        },
        "base": {
            "REGIME": regime,
            "REGIME_NAME": REGIMES[regime],
            "VP": vp,
            "MSP": msp,
            "SOURCE_NOTE": str(br.get("SOURCE_NOTE", "")),
        },
        "fast_action": values,
        "risk_pressure": {
            "score": rp_now,
            "classification": classify_pressure(rp_now),
            "month_end_reference": rp_prev,
            "delta_vs_month_end": rp_delta,
            "weights": {
                "BASE_VP": 0.45,
                "MSP_MARKET": 0.25,
                "MSP_CREDIT_CONTROL": 0.20,
                "MSP_VOL": 0.10,
            },
            "method": "V6_DIAGNOSTIC_COMPOSITE",
            "action_authority": "NONE",
            "note": (
                "Diagnostic regime-stability score only; not a crash probability, "
                "not a market-timing rule and never used to set IVY/cash weights."
            ),
        },
        "assets": {
            "boom_bust_image_url": IMAGE_URL,
        },
        "clean3f_vs_sentinel_70_75": fast.get("clean3f_vs_sentinel_70_75"),
        "operating_mode": mode if base_valid else "FULL BASE REFRESH REQUIRED",
        "source_checks": fast.get("source_checks", {}),
    }

    OUT_JSON.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    if base_valid:
        base_line = (
            f"{regime}/7 – {REGIMES[regime]} "
            f"(BASE-Datenstand {base_date.date()}, {stale_days} Kalendertage alt)"
        )
    else:
        base_line = (
            "NICHT AKTUELL FREIGEBBAR – BASE-Refresh erforderlich "
            f"(letzter BASE-Stand {base_date.date()})"
        )

    prev_line = "nicht verfügbar"
    delta_line = "nicht verfügbar"
    if rp_prev is not None:
        prev_line = f"{float(rp_prev['score']):.1f}/100 ({rp_prev['reference_date']})"
        delta_line = f"{rp_delta:+.1f} Punkte"

    md = f"""# IVY11 – Markt-Regime-Diagnose Runtime Snapshot

**Marktdatum Action Layer:** {fast['target_market_date']}  
**BASE-Datenstand:** {base_date.date().isoformat()}  
**BASE-Freshness:** {base_status}  
**Authority:** DIAGNOSTIC ONLY / keine Portfolio-Cashsteuerung

## BASE
- Regime: **{base_line}**
- VP: **{vp:.2f}**
- MSP: **{msp:.2f}**

## Risikoindikatoren
- MSP Market: **{market:.2f}**
- MSP Credit: **{credit:.2f}**
- MSP Volatility: **{vol:.2f}**
- Fast Sentinel 65/70: **{values['FAST_SENTINEL_65_70']}**
- CLEAN3F Vote: **{float(values['CLEAN3F_VOTE_PCT']):.1f}%**
- Sentinel 70/75: **{values['SENTINEL_70_75']}**
- 200D-Abstand: **{float(values['EQ_MA200_GAP_PCT']):.2f}%**
- Drawdown vom ATH: **{float(values['EQ_DRAWDOWN_PCT']):.2f}%**
- 5D-Bewegung: **{float(values['EQ_RET_5D_PCT']):.2f}%**
- 20D-Bewegung: **{float(values['EQ_RET_20D_PCT']):.2f}%**

## Risk Pressure
- aktuell: **{rp_now:.1f}/100 – {classify_pressure(rp_now)}**
- letzter Monatsultimo: **{prev_line}**
- Veränderung: **{delta_line}**
- Formel: **45% BASE VP + 25% MSP Market + 20% MSP Credit + 10% MSP Volatility**
- Verwendung: **nur diagnostisch; keine Cash-/IVY-Steuerung**

## Boom-&-Bust-Grafik
{IMAGE_URL}

## Operating mode
**{result['operating_mode']}**
"""
    OUT_MD.write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
