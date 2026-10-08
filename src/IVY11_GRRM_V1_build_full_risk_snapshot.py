#!/usr/bin/env python3
"""
IVY11_GRRM_V1 — Full Risk Snapshot Builder V1

Combines:
- canonical FAST DAILY runtime snapshot, and
- last valid canonical BASE diagnostic snapshot

into one canonical management snapshot used by:
- the daily market-regime/risk quick check, and
- the monthly IVY4 update incl. market-regime/risk management.

Safety / freshness rule
-----------------------
BASE may be up to 7 calendar days old ONLY while the fast layer is calm.
If Fast Sentinel is ALERT or CLEAN3F is in stress / DAILY STRESS WATCH,
same-date BASE is required. Otherwise the full snapshot explicitly returns
BASE_REFRESH_REQUIRED and must fail closed for a current BASE regime claim.

This script does not authorize live Strategic Cash.
"""
from __future__ import annotations
from pathlib import Path
import json, pandas as pd

HERE = Path(__file__).resolve().parents[1]
FAST = HERE/"docs/latest_fast_snapshot.json"
BASE = HERE/"data/IVY11_GRRM_V1-GX_BASE_Diagnostic_Anchor_2026-10-02.csv"
OUT_JSON = HERE/"docs/latest_full_risk_snapshot.json"
OUT_MD = HERE/"docs/latest_full_risk_snapshot.md"

POLICY_VERSION = "V5.0"

REGIMES = {
    1:"Kapitulation/Bodenbildung",
    2:"Frühe Erholung",
    3:"Gesunder Bullenmarkt",
    4:"Reifer Bullenmarkt",
    5:"Überhitzung/Fragilität",
    6:"Stress/Bärenmarkt",
    7:"Systemischer Crash",
}


POLICY = {
    1:{"strategic_cash":20.0,"ivy4_budget":47.5},
    2:{"strategic_cash":10.0,"ivy4_budget":57.5},
    3:{"strategic_cash":0.0,"ivy4_budget":67.5},
    4:{"strategic_cash":20.0,"ivy4_budget":47.5},
    5:{"strategic_cash":35.0,"ivy4_budget":32.5},
    6:{"strategic_cash":55.0,"ivy4_budget":12.5},
    7:{"strategic_cash":45.0,"ivy4_budget":22.5},
}

FIXED_SLEEVE = {
    "gold":{
        "name":"EUWAX Gold II",
        "isin":"DE000EWG2LD7",
        "portfolio_pct":7.5,
    },
    "broad_commodities":{
        "name":"iShares Diversified Commodity Swap UCITS ETF",
        "isin":"IE00BDFL4P12",
        "portfolio_pct":10.0,
    },
    "global_mining":{
        "name":"VanEck S&P Global Mining UCITS ETF",
        "isin":"IE00BDFBTQ78",
        "portfolio_pct":5.0,
    },
    "global_core":{
        "name":"State Street SPDR MSCI ACWI IMI UCITS ETF (Acc)",
        "isin":"IE00B3YLTY66",
        "portfolio_pct":10.0,
    },
}

FIXED_SLEEVE_TOTAL = sum(v["portfolio_pct"] for v in FIXED_SLEEVE.values())
if abs(FIXED_SLEEVE_TOTAL - 32.5) > 1e-9:
    raise RuntimeError("Fixed strategic sleeve must total 32.5%.")

for _regime, _policy in POLICY.items():
    if abs(_policy["strategic_cash"] + _policy["ivy4_budget"] + FIXED_SLEEVE_TOTAL - 100.0) > 1e-9:
        raise RuntimeError(f"R{_regime} strategic allocation does not sum to 100%.")
def classify_crash_phase(regime, values):
    """Advisory diagnostic only; not a backtest-validated trading rule."""
    dd=float(values["EQ_DRAWDOWN_PCT"])
    r5=float(values["EQ_RET_5D_PCT"])
    r20=float(values["EQ_RET_20D_PCT"])
    gap=float(values["EQ_MA200_GAP_PCT"])
    market=float(values["MSP_MARKET"])
    vol=float(values["MSP_VOL"])
    credit=float(values["MSP_CREDIT_CONTROL"])
    confirmed=bool(values["CLEAN3F_STRESS"]) or str(values["SENTINEL_70_75"])=="STRESS"

    # D requires a very advanced sell-off plus either BASE capitulation or visible stabilization.
    stabilizing=(r5>=0 and r20>-8) or (r5>-2 and market<75 and vol<80)
    if regime==1 or (dd<=-35 and stabilizing):
        return "D", "Kapitulation/Bodenbildungszone", "PREPARE RE-RISKING"
    # C = much of the drawdown already realized.
    if dd<=-20 or (dd<=-15 and confirmed and gap<0):
        return "C", "Fortgeschrittener Crash", "HOLD / NO CATCH-UP SELLING"
    # B = clear stress with meaningful but not yet advanced drawdown.
    if regime>=6 or (dd<=-7 and (confirmed or market>=65 or vol>=70 or gap<0)):
        return "B", "Frühe Crashphase", "DE-RISK NOW"
    # A = warning state; no forced action.
    if regime>=5 or market>=55 or vol>=60 or credit>=60 or dd<=-5 or gap<=2:
        return "A", "Frühwarnung / Pre-Crash", "DE-RISK PARTIAL"
    return "NONE", "Keine Crashphase", "NO ACTION"

def main():
    fast=json.loads(FAST.read_text(encoding="utf-8"))
    if fast.get("schema")!="IVY11_GRRM_V1_FAST_DAILY_V1":
        raise RuntimeError("Invalid FAST DAILY schema.")
    if "SHADOW" not in fast.get("action_authority",""):
        raise RuntimeError("FAST DAILY action authority is not SHADOW.")

    values=fast.get("values",{})
    required=[
        "MSP_MARKET","MSP_VOL","MSP_CREDIT_CONTROL","CLEAN3F_VOTE_PCT",
        "CLEAN3F_STRESS","FAST_SENTINEL_65_70","SENTINEL_70_75",
        "EQ_MA200_GAP_PCT","EQ_DRAWDOWN_PCT","EQ_RET_5D_PCT","EQ_RET_20D_PCT",
        "D200","MODE"
    ]
    missing=[x for x in required if x not in values]
    if missing:
        raise RuntimeError(f"FAST DAILY missing fields: {missing}")

    b=pd.read_csv(BASE)
    if len(b)!=1:
        raise RuntimeError("BASE anchor must contain exactly one row.")
    br=b.iloc[0]

    target=pd.Timestamp(fast["target_market_date"]).normalize()
    base_date=pd.Timestamp(br["Date"]).normalize()
    stale_days=int((target-base_date).days)
    mode=str(values["MODE"])
    fast_alert=str(values["FAST_SENTINEL_65_70"])=="ALERT"
    clean_stress=bool(values["CLEAN3F_STRESS"])
    calm=(mode=="NORMAL MODE" and not fast_alert and not clean_stress)

    if stale_days < 0:
        raise RuntimeError("BASE date is after FAST target date.")

    if base_date==target:
        base_status="CURRENT"
        base_valid_for_management=True
    elif calm and stale_days<=7:
        base_status="VALID_STALE_WITHIN_CALM_POLICY"
        base_valid_for_management=True
    else:
        base_status="BASE_REFRESH_REQUIRED"
        base_valid_for_management=False

    regime=int(br["BASE_REGIME"])
    phase_code,phase_name,phase_action=classify_crash_phase(regime,values)
    result={
        "schema":"IVY11_GRRM_V1_FULL_RISK_V1",
        "target_market_date":fast["target_market_date"],
        "generated_utc":fast["generated_utc"],
        "action_authority":"SHADOW / NO_AUTOMATIC_REAL_CASH_ACTION",
        "base_policy":{
            "base_data_date":base_date.date().isoformat(),
            "base_staleness_calendar_days":stale_days,
            "calm_mode_max_staleness_days":7,
            "alert_or_stress_requires_same_date_base":True,
            "status":base_status,
            "base_valid_for_management":base_valid_for_management,
        },
        "base":{
            "REGIME":regime,
            "REGIME_NAME":REGIMES[regime],
            "VP":float(br["VP"]),
            "MSP":float(br["MSP"]),
            "SOURCE_NOTE":str(br.get("SOURCE_NOTE","")),
        },
        "fast_action":values,
        "crash_phase":{
            "code":phase_code,
            "name":phase_name,
            "decision_status":phase_action,
            "drawdown_pct":float(values["EQ_DRAWDOWN_PCT"]),
            "ret_5d_pct":float(values["EQ_RET_5D_PCT"]),
            "ret_20d_pct":float(values["EQ_RET_20D_PCT"]),
            "ma200_gap_pct":float(values["EQ_MA200_GAP_PCT"]),
            "method_note":"Advisory diagnostic; phase uses regime, market/credit/volatility stress, 200D, drawdown and 5D/20D velocity. Not a backtest-validated automatic action rule.",
        },
        "strategic_policy_orientation":{
            "policy_version":POLICY_VERSION,
            "policy_type":"USER_POLICY_DEFENSIVE_ORIENTATION_NOT_AUTOMATIC",
            "regime":regime,
            "current_regime_policy":POLICY[regime],
            "all_regime_policy":POLICY,
            "fixed_sleeve_total_pct":FIXED_SLEEVE_TOTAL,
            "fixed_sleeve":FIXED_SLEEVE,
            "regime_sensitive_total_pct":67.5,
            "architecture_note":"Fixed strategic sleeve = 22.5% Real Assets (7.5% Gold, 10% Broad Commodities, 5% Global Mining) + 10% Global Core. Only the remaining 67.5% is regime-sensitive and is split between IVY4 and Strategic Cash. IVY Temporary Cash from SMA10 FAIL slots remains separate from Strategic Cash.",
        },
        "clean3f_vs_sentinel_70_75":fast["clean3f_vs_sentinel_70_75"],
        "operating_mode":mode if base_valid_for_management else "FULL BASE REFRESH REQUIRED",
        "source_checks":fast.get("source_checks",{}),
    }

    OUT_JSON.write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding="utf-8")

    if base_valid_for_management:
        base_line=(
            f"{regime}/7 – {REGIMES[regime]} "
            f"(BASE-Datenstand {base_date.date()}, {stale_days} Kalendertage alt)"
        )
    else:
        base_line=(
            f"NICHT AKTUELL FREIGEBBAR – BASE-Refresh erforderlich "
            f"(letzter BASE-Stand {base_date.date()})"
        )

    md=f"""# IVY11 – Marktregime & Risikosteuerung Runtime Snapshot

**Marktdatum Action Layer:** {fast['target_market_date']}  
**BASE-Datenstand:** {base_date.date().isoformat()}  
**BASE-Freshness:** {base_status}  
**Action Authority:** SHADOW / keine automatische reale Cashaktion

## BASE
- Regime: **{base_line}**
- VP: **{float(br['VP']):.2f}**
- MSP: **{float(br['MSP']):.2f}**

## Action Layer
- MSP Market: **{float(values['MSP_MARKET']):.2f}**
- MSP Credit: **{float(values['MSP_CREDIT_CONTROL']):.2f}**
- MSP Volatility: **{float(values['MSP_VOL']):.2f}**
- Fast Sentinel 65/70: **{values['FAST_SENTINEL_65_70']}**
- CLEAN3F Vote: **{float(values['CLEAN3F_VOTE_PCT']):.1f}%**
- CLEAN3F Stress: **{values['CLEAN3F_STRESS']}**
- Sentinel 70/75: **{values['SENTINEL_70_75']}**
- 200D gap: **{float(values['EQ_MA200_GAP_PCT']):.2f}%**
- Drawdown vom ATH: **{float(values['EQ_DRAWDOWN_PCT']):.2f}%**
- 5D-Bewegung: **{float(values['EQ_RET_5D_PCT']):.2f}%**
- 20D-Bewegung: **{float(values['EQ_RET_20D_PCT']):.2f}%**
- 200D: **{values['D200']}**
- CLEAN3F vs. 70/75: **{fast['clean3f_vs_sentinel_70_75']}**

## Crash-/Drawdown-Phase
- Phase: **{phase_code} – {phase_name}**
- Decision Status: **{phase_action}**
- USER POLICY V5.0 Soll-Allokation für R{regime}: **{POLICY[regime]['strategic_cash']:.1f} % Strategic Cash / {POLICY[regime]['ivy4_budget']:.1f} % IVY4 / 7.5 % Gold / 10.0 % Broad Commodities / 5.0 % Global Mining / 10.0 % Global Core**
- Fixer strategischer Sockel: **32.5 %** = 22.5 % Real Assets + 10.0 % Global Core.
- Regime-sensitiver Bereich: **67.5 %** = IVY4 + Strategic Cash.
- Produkte: Gold **DE000EWG2LD7** / Broad Commodities **IE00BDFL4P12** / Global Mining **IE00BDFBTQ78** / Global Core **IE00B3YLTY66**.
- Wichtig: IVY Temporary Cash aus SMA10-FAIL-Slots ist zusätzliches Cash innerhalb des IVY4-Budgets und bleibt getrennt von Strategic Cash.
- Hinweis: USER POLICY / defensive Orientierung; keine automatische reale Cashaktion, solange Action Authority SHADOW ist.

## Operating mode
**{result['operating_mode']}**

If BASE_REFRESH_REQUIRED, no same-date BASE regime claim may be made until the BASE
diagnostic runtime has refreshed successfully.
"""
    OUT_MD.write_text(md,encoding="utf-8")
    print(md)

if __name__=="__main__":
    main()
