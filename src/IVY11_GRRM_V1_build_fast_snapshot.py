#!/usr/bin/env python3
"""
IVY11_GRRM_V1 — Fast Daily Snapshot Builder

Purpose
-------
Compute the exact frozen action-layer inputs for the Custom GPT outside the GPT:
- MSP Market
- MSP CONTROL Credit
- MSP Volatility
- Fast Sentinel 65/70
- CLEAN3F Vote (frozen 243-model ensemble)
- Sentinel 70/75
- Daily 200D gap

The script does NOT authorize live Strategic Cash.
It publishes a compact SHADOW snapshot that the GPT can read.

Frozen boundary
---------------
Action history / state seed: 2026-10-02
Future observations are appended only.
"""
from __future__ import annotations
from pathlib import Path
from zoneinfo import ZoneInfo
import argparse, csv, datetime as dt, hashlib, importlib.util, json, math, sys, time
import numpy as np
import pandas as pd
import requests

HERE = Path(__file__).resolve().parents[1]
ANCHOR = HERE/"data/IVY11_GRRM_V1-GQ_Live_Action_Anchor.csv"
SEED = HERE/"data/IVY11_GRRM_V1-IW_CLEAN3F_State_Seed_2026-10-02.json"
KERNEL = HERE/"runtime/IVY11_GRRM_V1-GO_CLEAN3F_Live_Kernel.py"
OUT_JSON = HERE/"docs/latest_fast_snapshot.json"
OUT_MD = HERE/"docs/latest_fast_snapshot.md"

# Frozen source-availability boundary:
# The 2026-10-02 state seed was built with VIX information available only
# through 2026-10-01.  The anchor row on 2026-10-02 therefore legitimately
# carries the 2026-10-01 VIX value forward.  Do not compare a later-published
# true 2026-10-02 Cboe close against that carried state row.
FROZEN_VIX_LAST_TRUE_OBS_DATE = pd.Timestamp("2026-10-01")

def load_kernel():
    spec=importlib.util.spec_from_file_location("ivy_clean_live_kernel",KERNEL)
    mod=importlib.util.module_from_spec(spec)
    sys.modules["ivy_clean_live_kernel"]=mod
    spec.loader.exec_module(mod)
    return mod

def current_percentile(values, x):
    a=pd.to_numeric(pd.Series(values),errors="coerce").dropna().to_numpy(float)
    if len(a)==0 or pd.isna(x):
        return np.nan
    return float(100.0*(np.sum(a<=float(x))-0.5)/len(a))

def fetch_fred(series_id):
    """
    Official authenticated FRED API v1.
    Full history from 1990-01-01 is requested because the frozen score
    percentiles were built from that evidence window.  This avoids the
    bulk fredgraph.csv transport that timed out from GitHub Actions.
    """
    import os
    key=os.environ.get("FRED_API_KEY","").strip()
    if not key:
        raise RuntimeError(
            "FRED_API_KEY GitHub Actions secret is missing. "
            "Fail closed; do not fall back to another provider."
        )

    url="https://api.stlouisfed.org/fred/series/observations"
    params={
        "series_id":series_id,
        "api_key":key,
        "file_type":"json",
        "observation_start":"1990-01-01",
        "sort_order":"asc",
        "limit":100000,
    }
    headers={
        "User-Agent":"IVY11_GRRM_V1 research runtime",
        "Accept":"application/json",
    }

    last=None
    for wait in [0,2,5]:
        if wait:
            time.sleep(wait)
        try:
            r=requests.get(url,params=params,headers=headers,timeout=(15,60))
            r.raise_for_status()
            j=r.json()
            obs=j.get("observations",[])
            if not obs:
                raise RuntimeError(f"{series_id}: FRED API returned no observations")
            d=pd.DataFrame(
                [(o.get("date"),o.get("value")) for o in obs],
                columns=["Date",series_id]
            )
            d["Date"]=pd.to_datetime(d["Date"],errors="coerce")
            d[series_id]=pd.to_numeric(d[series_id],errors="coerce")
            d=d.dropna().sort_values("Date").drop_duplicates("Date")
            if d.empty:
                raise RuntimeError(f"{series_id}: no numeric FRED API observations")
            # Never persist/log the API key.  Fingerprint response content only.
            return d, f"FRED_API:{series_id}", hashlib.sha256(r.content).hexdigest()
        except Exception as e:
            last=e

    raise RuntimeError(f"{series_id}: authenticated FRED API failed: {last!r}")

def fetch_cboe_vix():
    """Official Cboe VIX daily close history with overlap validation."""
    from io import StringIO
    urls=[
        "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
        "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
    ]
    headers={"User-Agent":"IVY11_GRRM_V1 research runtime","Accept":"text/csv,*/*"}
    last=None
    for url in urls:
        try:
            r=requests.get(url,headers=headers,timeout=(15,60),allow_redirects=True)
            r.raise_for_status()
            d=pd.read_csv(StringIO(r.text))
            d.columns=[str(c).strip().upper() for c in d.columns]
            if "DATE" not in d.columns or "CLOSE" not in d.columns:
                raise RuntimeError(f"Cboe VIX CSV unexpected columns: {list(d.columns)}")
            d=d.rename(columns={"DATE":"Date","CLOSE":"VIX"})
            d["Date"]=pd.to_datetime(d["Date"],errors="coerce")
            d["VIX"]=pd.to_numeric(d["VIX"],errors="coerce")
            d=d[["Date","VIX"]].dropna().sort_values("Date").drop_duplicates("Date")
            if d.empty:
                raise RuntimeError("Cboe VIX CSV returned no usable rows")
            return d, r.url, hashlib.sha256(r.content).hexdigest()
        except Exception as e:
            last=e
    raise RuntimeError(f"Cboe VIX official history download failed: {last!r}")

def cboe_vix_overlap_gate(anchor, cboe, min_pairs=10, tol=0.02):
    """
    Validate the official Cboe VIX close against the frozen historical VIX
    only over dates that were genuine VIX observations at the freeze boundary.

    Critical PIT rule:
    - state seed date: 2026-10-02
    - latest true VIX observation available when that seed was frozen: 2026-10-01
    - the anchor's 2026-10-02 VIX value is therefore a legitimate carry-forward
      state value, NOT a 2026-10-02 close observation.

    A later-published true 2026-10-02 Cboe close must never be used to
    invalidate the already-frozen 2026-10-02 state.  That would be look-ahead.
    """
    a=anchor[["Date","VIX"]].dropna().copy()
    a=a[a["Date"]<=FROZEN_VIX_LAST_TRUE_OBS_DATE].copy()

    q=a.merge(cboe,on="Date",how="inner",suffixes=("_ANCHOR","_CBOE")).sort_values("Date")
    q=q.tail(20).copy()

    if len(q)<min_pairs:
        raise RuntimeError(f"Cboe VIX overlap insufficient: {len(q)} pairs")

    q["abs_diff"]=(q["VIX_ANCHOR"]-q["VIX_CBOE"]).abs()
    mx=float(q["abs_diff"].max())

    if mx>tol:
        worst=q.loc[q["abs_diff"].idxmax()]
        raise RuntimeError(
            "Cboe VIX overlap parity failed before frozen information boundary: "
            f"max_diff={mx} > {tol}; "
            f"worst_date={worst['Date'].date()}; "
            f"anchor={worst['VIX_ANCHOR']}; cboe={worst['VIX_CBOE']}"
        )

    return {
        "pairs":int(len(q)),
        "max_abs_diff":mx,
        "tolerance":tol,
        "last_overlap_date":q["Date"].max().date().isoformat(),
        "frozen_vix_last_true_observation":
            FROZEN_VIX_LAST_TRUE_OBS_DATE.date().isoformat(),
        "excluded_carried_state_date":"2026-10-02",
    }


def fetch_spy(start_date):
    import yfinance as yf
    start=pd.Timestamp(start_date).date().isoformat()
    end=(pd.Timestamp.now(tz="UTC").tz_localize(None)+pd.Timedelta(days=2)).date().isoformat()
    d=yf.download("SPY",start=start,end=end,auto_adjust=False,actions=False,
                  progress=False,threads=False)
    if d is None or d.empty:
        raise RuntimeError("SPY Yahoo download empty")
    if isinstance(d.columns,pd.MultiIndex):
        s=d["Adj Close"].iloc[:,0]
    else:
        s=d["Adj Close"]
    s=pd.to_numeric(s,errors="coerce").dropna()
    s.index=pd.to_datetime(s.index).tz_localize(None)
    return s.rename("SPY_ADJ_CLOSE").reset_index().rename(columns={"index":"Date"})

def latest_finalized_cap():
    """Do not treat today's partial US session as a close."""
    now=dt.datetime.now(ZoneInfo("America/New_York"))
    today=pd.Timestamp(now.date())
    if now.weekday()>=5:
        return today
    if (now.hour,now.minute) < (16,15):
        return today-pd.offsets.BDay(1)
    return today

def spy_overlap_gate(anchor, spy, min_pairs=5, tol=5e-4):
    a=anchor[["Date","EQUITY"]].dropna().copy()
    q=spy.merge(a,on="Date",how="inner").sort_values("Date")
    q=q[q.Date>=anchor.Date.max()-pd.Timedelta(days=45)]
    q["spy_r"]=q.SPY_ADJ_CLOSE.pct_change()
    q["anchor_r"]=q.EQUITY.pct_change()
    z=q.dropna(subset=["spy_r","anchor_r"]).tail(12).copy()
    if len(z)<min_pairs:
        raise RuntimeError(f"SPY overlap insufficient: {len(z)} return pairs")
    mx=float((z.spy_r-z.anchor_r).abs().max())
    if mx>tol:
        raise RuntimeError(f"SPY return-overlap parity failed: {mx} > {tol}")
    return {"pairs":int(len(z)),"max_abs_return_diff":mx,"tolerance":tol}

def build_equity(anchor, spy, final_target):
    a=anchor[["Date","EQUITY"]].dropna().sort_values("Date")
    anchor_end=anchor.Date.max()
    anchor_val=float(anchor.loc[anchor.Date==anchor_end,"EQUITY"].iloc[-1])

    ref=spy.loc[spy.Date==anchor_end,"SPY_ADJ_CLOSE"]
    if ref.empty:
        raise RuntimeError(f"SPY must contain frozen anchor date {anchor_end.date()}")
    ref=float(ref.iloc[-1])

    bcal=pd.bdate_range(anchor.Date.min(),final_target)
    eq=anchor.set_index("Date").EQUITY.reindex(bcal)
    new=spy[(spy.Date>anchor_end)&(spy.Date<=final_target)].copy()
    new["EQUITY_CONTINUED"]=anchor_val*(new.SPY_ADJ_CLOSE/ref)
    for _,r in new.iterrows():
        if r.Date in eq.index:
            eq.loc[r.Date]=float(r.EQUITY_CONTINUED)
    return eq.ffill()

def market_score_from_equity(eq,target):
    px=eq.astype(float)
    f=pd.DataFrame(index=px.index)
    f["EQ_MA200_GAP"]=100*(px/px.rolling(200).mean()-1)
    f["EQ_MOM6"]=100*(px/px.shift(126)-1)
    f["EQ_MOM12"]=100*(px/px.shift(252)-1)
    f["EQ_DRAWDOWN"]=100*(px/px.cummax()-1)
    cur=f.loc[target]
    parts={}
    for c in ["EQ_MA200_GAP","EQ_MOM6","EQ_MOM12","EQ_DRAWDOWN"]:
        hist=f.loc[:target,c].dropna()
        p=current_percentile(hist,cur[c])
        parts[c]=float(100-p)
    ret5=100*(px/px.shift(5)-1)
    ret20=100*(px/px.shift(20)-1)
    diag={
        "EQ_DRAWDOWN_PCT":float(cur["EQ_DRAWDOWN"]),
        "EQ_RET_5D_PCT":float(ret5.loc[target]) if pd.notna(ret5.loc[target]) else None,
        "EQ_RET_20D_PCT":float(ret20.loc[target]) if pd.notna(ret20.loc[target]) else None,
    }
    return float(np.mean(list(parts.values()))),float(cur["EQ_MA200_GAP"]),parts,diag

def native_current_score(df,col,target):
    q=df[df.Date<=target].copy()
    if q.empty:
        raise RuntimeError(f"{col}: no data through {target.date()}")
    row=q.iloc[-1]
    x=float(row[col])
    p=current_percentile(q[col],x)
    return p,pd.Timestamp(row.Date),x

def level(status):
    return {"NORMAL MODE":"green","ALERT MODE":"orange","DAILY STRESS WATCH":"red"}[status]

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--target-date",default="auto",
                    help="YYYY-MM-DD or auto; auto chooses latest finalized common SPY/VIX date")
    args=ap.parse_args()

    anchor=pd.read_csv(ANCHOR,parse_dates=["Date"]).sort_values("Date")
    anchor_end=anchor.Date.max()
    seed_info=json.loads(SEED.read_text(encoding="utf-8"))
    seed_date=pd.Timestamp(seed_info["anchor_date"])
    if seed_date!=anchor_end:
        raise RuntimeError("Frozen state seed date and anchor end differ")

    spy=fetch_spy(anchor_end-pd.Timedelta(days=50))
    spy_gate=spy_overlap_gate(anchor,spy)

    vix,vix_url,vix_sha=fetch_cboe_vix()
    vix_gate=cboe_vix_overlap_gate(anchor,vix)
    baa,baa_url,baa_sha=fetch_fred("BAA10Y")
    aaa,aaa_url,aaa_sha=fetch_fred("AAA10Y")

    if args.target_date=="auto":
        cap=pd.Timestamp(latest_finalized_cap()).normalize()
        spy_dates=set(spy.loc[spy.Date<=cap,"Date"].dt.normalize())
        vix_dates=set(vix.loc[vix.Date<=cap,"Date"].dt.normalize())
        common=sorted(d for d in spy_dates.intersection(vix_dates) if d>anchor_end)
        if not common:
            raise RuntimeError("No finalized common SPY/VIX market date after frozen anchor")
        final_target=common[-1]
    else:
        final_target=pd.Timestamp(args.target_date).normalize()

    if final_target<=anchor_end:
        raise RuntimeError("Target must be after frozen 2026-10-02 anchor")

    # Every post-anchor SPY market date through target must be represented by VIX.
    replay_dates=sorted(
        d for d in set(spy.loc[(spy.Date>anchor_end)&(spy.Date<=final_target),"Date"].dt.normalize())
        if d in set(vix.Date.dt.normalize())
    )
    if not replay_dates or replay_dates[-1]!=final_target:
        raise RuntimeError("Cannot form complete replay path to target")

    eq=build_equity(anchor,spy,final_target)
    kernel_mod=load_kernel()
    kernel=kernel_mod.Clean3FLiveKernel.from_seed_file(SEED)

    vix_hist=anchor[["Date","VIX"]].dropna().copy()
    appended_vix=[]
    rows=[]

    for d in replay_dates:
        market,gap,parts,eq_diag=market_score_from_equity(eq,d)

        vrow=vix.loc[vix.Date==d]
        if vrow.empty:
            raise RuntimeError(f"VIX missing for {d.date()}")
        vix_val=float(vrow.iloc[-1].VIX)
        prior_vix=pd.concat(
            [vix_hist["VIX"],pd.Series([x for _,x in appended_vix])],
            ignore_index=True
        )
        vix_score=current_percentile(pd.concat([prior_vix,pd.Series([vix_val])],ignore_index=True),vix_val)
        appended_vix.append((d,vix_val))

        baa_score,baa_date,baa_val=native_current_score(baa,"BAA10Y",d)
        aaa_score,aaa_date,aaa_val=native_current_score(aaa,"AAA10Y",d)
        if (d-baa_date).days>4 or (d-aaa_date).days>4:
            raise RuntimeError(
                f"Credit stale on {d.date()}: BAA {baa_date.date()}, AAA {aaa_date.date()}"
            )
        credit=float(np.mean([baa_score,aaa_score]))

        action=kernel.update(market,credit,vix_score)
        s6570=bool(market>=65 and vix_score>=70)
        s7075=bool(market>=70 and vix_score>=75)
        mode="DAILY STRESS WATCH" if action["CLEAN3F_STRESS"] else ("ALERT MODE" if s6570 else "NORMAL MODE")
        rows.append({
            "Date":d,
            "MSP_MARKET":market,
            "MSP_VOL":vix_score,
            "MSP_CREDIT_CONTROL":credit,
            "CLEAN3F_VOTE_PCT":100*float(action["CLEAN3F_VOTE"]),
            "CLEAN3F_STRESS":bool(action["CLEAN3F_STRESS"]),
            "CLEAN3F_R6_R7_COUNT":int(action["R6_R7_COUNT"]),
            "FAST_SENTINEL_65_70":"ALERT" if s6570 else "NO ALERT",
            "SENTINEL_70_75":"STRESS" if s7075 else "NO STRESS",
            "EQ_MA200_GAP_PCT":gap,
            "EQ_DRAWDOWN_PCT":eq_diag["EQ_DRAWDOWN_PCT"],
            "EQ_RET_5D_PCT":eq_diag["EQ_RET_5D_PCT"],
            "EQ_RET_20D_PCT":eq_diag["EQ_RET_20D_PCT"],
            "D200":"RISK ON" if gap>0 else "RISK OFF",
            "MODE":mode,
            "VIX_RAW":vix_val,
            "BAA10Y_RAW":baa_val,
            "AAA10Y_RAW":aaa_val,
            "BAA_DATE":baa_date.date().isoformat(),
            "AAA_DATE":aaa_date.date().isoformat(),
        })

    latest=rows[-1]
    clean_state="STRESS" if latest["CLEAN3F_STRESS"] else "NO STRESS"
    comparison="ÜBEREINSTIMMUNG"
    if (clean_state=="STRESS") != (latest["SENTINEL_70_75"]=="STRESS"):
        comparison="DIVERGENZ"

    generated=dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    snapshot={
        "schema":"IVY11_GRRM_V1_FAST_DAILY_V1",
        "generated_utc":generated,
        "target_market_date":latest["Date"].date().isoformat(),
        "frozen_anchor_date":anchor_end.date().isoformat(),
        "action_authority":"SHADOW / NO_AUTOMATIC_REAL_CASH_ACTION",
        "research_status":"CLEAN3F_vs_SENTINEL_70_75_PARALLEL_PROSPECTIVE",
        "values":{
            k:(v.item() if hasattr(v,"item") else v)
            for k,v in latest.items() if k!="Date"
        },
        "clean3f_vs_sentinel_70_75":comparison,
        "source_checks":{
            "SPY_return_overlap":spy_gate,
            "VIX_source":"Cboe VIX Index daily history (official)",
            "VIX_overlap_gate":vix_gate,
            "VIX_frozen_information_boundary":"2026-10-01 true observation; 2026-10-02 anchor row was carried state",
            "VIX_url":vix_url,
            "BAA_source":"FRED BAA10Y",
            "AAA_source":"FRED AAA10Y",
            "VIX_sha256":vix_sha,
            "BAA_sha256":baa_sha,
            "AAA_sha256":aaa_sha,
        },
        "replay_days_after_anchor":len(rows),
    }
    OUT_JSON.write_text(json.dumps(snapshot,indent=2,ensure_ascii=False,default=str),encoding="utf-8")

    status_icon={"NORMAL MODE":"🟢","ALERT MODE":"🟠","DAILY STRESS WATCH":"🔴"}[latest["MODE"]]
    md=f"""# IVY11 – FAST DAILY Runtime Snapshot

**Datenstand:** {snapshot['target_market_date']}  
**Erzeugt:** {generated}  
**Modus:** {status_icon} **{latest['MODE']}**  
**Action Authority:** SHADOW / keine automatische reale Cashaktion

| Indikator | Wert |
|---|---:|
| MSP Market | {latest['MSP_MARKET']:.2f} |
| MSP Volatility | {latest['MSP_VOL']:.2f} |
| Fast Sentinel 65/70 | {latest['FAST_SENTINEL_65_70']} |
| MSP Credit | {latest['MSP_CREDIT_CONTROL']:.2f} |
| CLEAN3F Vote | {latest['CLEAN3F_VOTE_PCT']:.1f}% |
| CLEAN3F | {clean_state} |
| Sentinel 70/75 | {latest['SENTINEL_70_75']} |
| 200D-Abstand | {latest['EQ_MA200_GAP_PCT']:.2f}% |
| Drawdown vom ATH | {latest['EQ_DRAWDOWN_PCT']:.2f}% |
| 5D-Bewegung | {latest['EQ_RET_5D_PCT']:.2f}% |
| 20D-Bewegung | {latest['EQ_RET_20D_PCT']:.2f}% |
| 200D | {latest['D200']} |
| CLEAN3F vs. 70/75 | {comparison} |

This file is generated by the frozen IVY11_GRRM_V1 action runtime.
"""
    OUT_MD.write_text(md,encoding="utf-8")
    print(md)

if __name__=="__main__":
    main()
