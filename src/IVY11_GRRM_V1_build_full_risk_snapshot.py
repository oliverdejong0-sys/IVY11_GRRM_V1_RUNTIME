#!/usr/bin/env python3
"""
IVY11_GRRM_V1 — Full Risk Snapshot Builder V1

Combines:
- canonical FAST DAILY runtime snapshot, and
- last valid canonical BASE diagnostic snapshot

into one management snapshot for Button 3 "Marktregime & Risikosteuerung".

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

REGIMES = {
    1:"Kapitulation/Bodenbildung",
    2:"Frühe Erholung",
    3:"Gesunder Bullenmarkt",
    4:"Reifer Bullenmarkt",
    5:"Überhitzung/Fragilität",
    6:"Stress/Bärenmarkt",
    7:"Systemischer Crash",
}

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
        "EQ_MA200_GAP_PCT","D200","MODE"
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
- 200D: **{values['D200']}**
- CLEAN3F vs. 70/75: **{fast['clean3f_vs_sentinel_70_75']}**

## Operating mode
**{result['operating_mode']}**

If BASE_REFRESH_REQUIRED, no same-date BASE regime claim may be made until the BASE
diagnostic runtime has refreshed successfully.
"""
    OUT_MD.write_text(md,encoding="utf-8")
    print(md)

if __name__=="__main__":
    main()
