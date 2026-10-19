#!/usr/bin/env python3
"""IVY11 BASE Stage 6: actual DAILY research candidate from anchored BASE V3.

NO publication, NO productive state mutation, NO change to IVY allocations.
- Use frozen HG/GQ anchors and reconstructed, explicitly non-original HE/HF.
- FRED historical vintages obtained for EACH day (ALFRED as-of date).
- SPY Yahoo adjusted history: retrospective/PIT-B only, strictly <= target.
- Shiller and BIS: official downloads, conservative availability lag, PIT-B.
- Independent second transition evaluator audits each day.
- Every produced candidate is RESEARCH_ONLY and release=NO_GO.

GitHub workflow produces a fresh evidence archive each run; ONLY research output.
"""
from __future__ import annotations
import argparse
import csv
import datetime as dt
import gzip
import hashlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
import zipfile
from collections import defaultdict

import numpy as np
import pandas as pd

ANCHOR = "2026-10-02"
SIDS = ("VIXCLS", "BAA10Y", "AAA10Y", "NFCI", "T10Y3M", "SAHMREALTIME")
SLOW = {"NFCI": ("NFCI", "NFCI"), "T10Y3M": ("T10Y3M", "DAILY"),
        "SAHMREALTIME": ("SAHM", "MONTHLY35")}
BUNDLE = {
    "GQ_original.csv": "72aebc10aee433221cb0",
    "GU_original.py": "8c161c6b40390a7f3b15",
    "HE_rekonstruktion.py": "2e48ddbee63bb8406604",
    "HF_rekonstruktion.json": "d5563e65a8721fc8e918",
    "HG_original.csv": "134181241cde1ccd3cff",
    "HI_original.py": "7f6620b713cb9b480522",
    "HH_BIS_Official_Schema_Adapter.py": "de2f5551be5092ff893f",
}
BUNDLE_ROOT = "base_diagnostic/original_pruefdateien/"
REQUIRED_SHADOW = ("shadow_spy", "shadow_macro", "shadow_stage5")
FORBIDDEN_OUTPUT = ("latest_full_risk_snapshot.json", "latest_fast_snapshot.json",
                    "IVY11_GRRM_V1-GX_BASE_Diagnostic_Anchor_2026-10-02.csv")

def demand(cond, msg):
    if not bool(cond):
        raise RuntimeError(msg)

def digest(b):
    return hashlib.sha256(b).hexdigest()

def readjson(blob):
    x = json.loads(blob)
    demand(isinstance(x, dict), "JSON root must be object")
    return x

def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    demand(spec is not None and spec.loader is not None, "Module missing")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

def load_originals(bundle, tmp):
    root = Path(tmp)
    with zipfile.ZipFile(bundle) as z:
        demand(len(z.namelist()) == len(set(z.namelist())), "Duplicate source archive names")
        for name, locked in BUNDLE.items():
            path = BUNDLE_ROOT + name
            demand(path in z.namelist(), f"Missing original {name}")
            b = z.read(path)
            demand(digest(b).startswith(locked), f"Original source lock changed: {name}")
            (root/name).write_bytes(b)
    hi = load_module(root/"HI_original.py", "ivy_stage6_hi")
    gu = load_module(root/"GU_original.py", "ivy_stage6_gu")
    he = load_module(root/"HE_rekonstruktion.py", "ivy_stage6_he")
    bis = load_module(root/"HH_BIS_Official_Schema_Adapter.py", "ivy_stage6_bis")
    return hi, gu, he, bis

def stage5_read(evidence, name):
    return (Path(evidence)/name).read_bytes()

def verify_stage5(evidence, target, *, require_stage5=True):
    m = readjson(stage5_read(evidence, "shadow_macro/DAILY_READINESS_STATUS.json"))
    st = readjson(stage5_read(evidence, "shadow_spy/SPY_PROSPECTIVE_STATUS.json"))
    capfiles = sorted((Path(evidence)/"shadow_spy").glob("SPY_PROSPECTIVE_CAPTURE_*.json"))
    demand(len(capfiles)==1, "Exactly one SPY captured JSON required")
    spycap=readjson(capfiles[0].read_bytes())
    demand(m.get("schema")=="IVY11_BASE_DAILY_EVIDENCE_V1", "Unknown macro status schema")
    demand(m.get("source_evidence_ok") is True and not [
       s for s in m["steps"] if s.get("result")!="PASS"
    ],"Official macro source acquisition failed")
    demand(st.get("schema")=="IVY11_BASE_SPY_PROSPECTIVE_CAPTURE_V1", "Unknown SPY schema")
    demand(st.get("target_market_date")==target and
           st.get("target_market_date")==spycap["manifest"].get("target_market_date"),
           "SPY target mismatch")
    demand(st.get("rows_after_target_excluded")==0,
           "SPY capture included future rows")
    demand(m.get("target_market_date")==target, "Macro target mismatch")
    demand(m.get("production_release")=="NO_GO", "Shadow status unexpectedly publishes")
    demand(spycap["manifest"]==st, "SPY capture/status inconsistency")
    entries=spycap.get("rows")
    demand(isinstance(entries,list) and len(entries)==st["observations"],"SPY rows missing")
    canonical_rows=json.dumps({"rows":entries},sort_keys=True,ensure_ascii=False,
                             separators=(",",":"),allow_nan=False).encode("utf-8")
    demand(digest(canonical_rows)==st["rows_sha256"],
           "SPY provider rows SHA-256 mismatch")
    demand(all(pd.Timestamp(r["Date"]).date() <= dt.date.fromisoformat(target)
               for r in entries),"Post-target SPY price")
    demand(all("Adj Close" in r and float(r["Adj Close"])>0 for r in entries),
           "SPY adjusted close missing")
    demands = stage5_read(evidence, "shadow_stage5/STAGE5_QUELLEN_UND_RELEASE_GATES.json")
    gate = readjson(demands)
    need=("SPY_OFFICIAL_DIVIDEND_EVENT","SPY_ADJCLOSE_ARITHMETIC",
          "SPY_TARGET_BOUNDARY","SOURCE_RAW_HASH","MACRO_READONLY_EVIDENCE")
    demand(all(gate.get("gates",{}).get(k)=="PASS" for k in need),
           "Stage-5 source gate not passed")
    demand(gate.get("production_release")=="NO_GO","Stage5 release changed")
    steps = {s["name"]:s for s in m["steps"]}
    for sid in SIDS:
        step = steps[f"FRED_PIT_{sid}"]
        demand(step["result"]=="PASS" and step["detail"]["asof"]==target,
               f"{sid} target vintage mismatch")
        raw = gzip.decompress(stage5_read(evidence,"shadow_macro/"+step["detail"]["archive_file"]))
        demand(digest(raw)==step["detail"]["raw_sha256"], f"{sid} archived raw hash mismatch")
    for item in ("SHILLER_RETRIEVAL_ONLY_PIT_B","BIS_CREDIT_GAP_RETRIEVAL_ONLY_PIT_B",
                 "BIS_DSR_RETRIEVAL_ONLY_PIT_B"):
        x=steps[item]
        demand(x["result"]=="PASS" and x["detail"]["pit_class"].startswith("PIT_B"),
               f"{item} is not documented as PIT-B")
        raw=gzip.decompress(stage5_read(evidence,"shadow_macro/"+x["detail"]["archive_file"]))
        demand(digest(raw)==x["detail"]["raw_sha256"], f"{item} raw hash mismatch")
    return m,st,spycap,steps

def unpack_evidence_zip(zippath, outdir):
    with zipfile.ZipFile(zippath) as z:
        for member in z.infolist():
            nm=member.filename
            demand(not nm.startswith("/") and ".." not in Path(nm).parts,
                   "Evidence archive path traversal rejected")
            if nm.endswith("/"): continue
            dst=Path(outdir)/nm
            demand(dst.resolve().is_relative_to(Path(outdir).resolve()),
                   "Evidence path escapes output directory")
            dst.parent.mkdir(parents=True,exist_ok=True)
            dst.write_bytes(z.read(member))

def pit_fetch(day,sid,key, session=None):
    import requests
    demand(isinstance(key,str) and len(key)==32 and key.isalnum(),
           "FRED_API_KEY missing/invalid (never logged)")
    s=session or requests.Session()
    try:
        r=s.get("https://api.stlouisfed.org/fred/series/observations",params={
           "series_id":sid, "api_key":key, "file_type":"json",
           "realtime_start":day, "realtime_end":day,
           "limit":100000, "sort_order":"asc"
        },timeout=(15,90),headers={"User-Agent":"IVY11-BASE-research-PIT-no-trading"})
        r.raise_for_status()
        return r.content
    except Exception as exc:
        # The error URL may contain the API key; never include it in logs.
        raise RuntimeError(f"PIT {sid}/{day} API failed: {type(exc).__name__}") from None

def validate_pit(raw,day,sid):
    z=readjson(raw)
    demand(z.get("realtime_start")==day and z.get("realtime_end")==day,
           f"PIT header mismatch {day}/{sid}")
    obs=z.get("observations")
    demand(isinstance(obs,list) and len(obs)>25 and
           len(obs)==int(z.get("count",-1)),
           f"PIT truncation or insufficient history {day}/{sid}")
    values={}
    for r in obs:
        demand(r.get("realtime_start")==day and r.get("realtime_end")==day,
               "FRED individual vintage mismatch")
        d=str(r.get("date"))
        demand(len(d)==10 and d<=day,"FRED future/invalid observation")
        try: v=float(r.get("value"))
        except (ValueError,TypeError):continue
        if not math.isfinite(v):continue
        demand(d not in values, f"Duplicate FRED observation {sid}/{d}")
        values[d]=v
    demand(len(values)>=50, f"FRED {sid}: too few numeric data points")
    if sid=="SAHMREALTIME":
        stale=(pd.Timestamp(day).to_period("M")-pd.Timestamp(max(values)).to_period("M")).n
        demand(0<=stale<=3,"SAHM observation too old")
    else:
        demand((pd.Timestamp(day)-pd.Timestamp(max(values))).days <=
               (20 if sid=="NFCI" else 6),
               f"FRED {sid} observation too old")
    df=pd.DataFrame({"Date":pd.to_datetime(sorted(values)),
                     "value":[values[x] for x in sorted(values)]})
    return df,{"day":day,"series":sid,"sha256":digest(raw),
               "last_observation":max(values),"count":len(values),
               "pit_class":"PIT_A_CALENDAR_DAY_NOT_INTRADAY"}

def independently_probe_latest_completed_spy(utc_now=None, fetcher=None):
    """Freshness only: never use probe prices in BASE, never include today's partial bar."""
    import datetime as dt
    import pandas as pd
    today=(utc_now or dt.datetime.now(dt.timezone.utc)).date()
    if fetcher is None:
        import yfinance as yf
        def fetcher():
            return yf.Ticker("SPY").history(period="20d",
                            auto_adjust=False,actions=False,raise_errors=True)
    try:
        frame=fetcher()
        demand(isinstance(frame,pd.DataFrame) and not frame.empty,
               "Freshness probe returned no SPY sessions")
        idx=pd.to_datetime(frame.index)
        ds=sorted({x.date() for x in idx if x.date()<today})
        demand(bool(ds), "No completed prior SPY session for freshness probe")
        return ds[-1].isoformat()
    except Exception as exc:
        # Do not reveal an external provider token/URL in runner logs.
        raise RuntimeError("SPY latest-completed-date probe failed "
                           f"({type(exc).__name__})") from None

def slow_sources(evidence, steps, hi, bis, tmp, offline_slow_dir=None):
    shiller_step=steps["SHILLER_RETRIEVAL_ONLY_PIT_B"]["detail"]
    shiller_raw=gzip.decompress(stage5_read(evidence,"shadow_macro/"+shiller_step["archive_file"]))
    # Preserve original official Shiller parse from source code.
    parser=load_module(Path(tmp)/"IVY11_BASE_Online_Abnahme.py",
                       "ivy_stage6_shiller_acquirer")
    if offline_slow_dir:
        # Historical pre-parsed official series, only for explicit offline research
        # when the local execution environment has no legacy XLS reader.
        d=Path(offline_slow_dir)
        cape=pd.read_csv(d/"CAPE_monthly.csv")
    else:
        cape=parser.parse_shiller_cape(shiller_raw)
    gap=bis.parse_bis_csv(gzip.decompress(stage5_read(evidence,
        "shadow_macro/"+steps["BIS_CREDIT_GAP_RETRIEVAL_ONLY_PIT_B"]["detail"]["archive_file"])),
        "Q.US.P.A.C")
    dsr=bis.parse_bis_csv(gzip.decompress(stage5_read(evidence,
        "shadow_macro/"+steps["BIS_DSR_RETRIEVAL_ONLY_PIT_B"]["detail"]["archive_file"])),
        "Q.US.P")
    return {"CAPE":hi.stamp_available(cape,"MONTHLY35"),
            "CREDIT_GAP":hi.stamp_available(gap,"QUARTERLY120"),
            "DSR":hi.stamp_available(dsr,"QUARTERLY120")}

def independent_step(prev, fallrun, market, credit, nfci, vol, valuation,
                     curve, cycle, d20):
    demand(all(math.isfinite(float(v)) for v in
        (market,credit,nfci,vol,valuation,curve,cycle,d20)),
        "Independent nonfinite component")
    vp=(valuation+curve+cycle)/3
    msp=(market+credit+nfci+vol)/4
    stress=(msp>=65 and market>=70 and (vol>=70 or credit>=70))
    baseline=(7 if msp>=85 else 6) if stress else (
        5 if vp>=70 and msp<65 else (4 if vp>=55 or 45<=msp<65 else 3))
    nxt=baseline
    if prev==7:
        fallrun=fallrun+1 if d20<=-3 else 0
        nxt=1 if fallrun>=3 else 7
    elif prev==1:
        nxt=baseline if stress and d20>=3.5 else (2 if msp<65 else 1)
    elif prev==2:
        nxt=baseline if ((stress and d20>=3.5) or msp<45) else 2
    elif prev==6 and msp<65 and market<70 and d20<0:
        nxt=2
    if nxt!=7:fallrun=0
    return nxt,vp,msp,fallrun

def run(args):
    out=Path(args.out)
    demand(not any(p in {"docs","data","runtime"} for p in out.parts[-2:]),
           "Candidate outputs cannot target production dirs")
    for bad in FORBIDDEN_OUTPUT:
        demand(out.name!=bad,"Production file cannot be written")
    out.mkdir(parents=True,exist_ok=True)
    status={"schema":"IVY11_BASE_STAGE6_REAL_CANDIDATE_V1",
            "production_release":"NO_GO","candidate_class":"RESEARCH_ONLY",
            "pipeline":"SOURCE_PIT_PLUS_RECONSTRUCTED_BASE",
            "actual_candidate_computed":False,
            "source_gate":"UNVERIFIED","observation_asof_policy":"FRED_DAY_PIT; SPY_SLOW_PIT_B",
            "gates":{},"errors":[]}
    def save_status():
        (out/"STATUS_STAGE6.json").write_text(
            json.dumps(status,indent=2,ensure_ascii=False,allow_nan=False)+"\n",encoding="utf-8")
    try:
        with tempfile.TemporaryDirectory(prefix="ivy11_stage6_") as d:
            tmp=Path(d)
            with zipfile.ZipFile(args.bundle) as z:
                src="base_diagnostic/IVY11_BASE_Online_Abnahme.py"
                demand(src in z.namelist(),"Official Shiller decoder source missing")
                (tmp/"IVY11_BASE_Online_Abnahme.py").write_bytes(z.read(src))
            hi,gu,he,bis=load_originals(args.bundle,tmp)
            if args.evidence_zip:
                root=tmp/"evidence"
                unpack_evidence_zip(args.evidence_zip,root)
            else:
                root=Path(args.evidence_dir)
            fast=readjson(Path(args.fast_json).read_bytes())
            target=str(fast.get("target_market_date",""))
            demand(fast.get("schema")=="IVY11_GRRM_V1_FAST_DAILY_V1",
                   "FAST schema invalid")
            demand(dt.date.fromisoformat(target)>dt.date.fromisoformat(ANCHOR),
                   "FAST target must advance 2026-10-02")
            demand(pd.Timestamp(target).weekday()<5,
                   "FAST target must be a business weekday")
            demand((dt.datetime.now(dt.timezone.utc).date()-
                    dt.date.fromisoformat(target)).days<=args.max_market_age_days,
                   "FAST target too old for daily candidate schedule")
            demand(dt.date.fromisoformat(target)<=dt.datetime.now(dt.timezone.utc).date(),
                   "FAST future market target")
            generated=dt.datetime.fromisoformat(fast["generated_utc"].replace("Z","+00:00"))
            demand(generated.tzinfo is not None and generated.date()>=
                   dt.date.fromisoformat(target), "FAST generation timestamp invalid")
            status["market_date"]=target
            status["fast_generated_utc"]=fast["generated_utc"]
            if args.require_latest_closed_spy_date:
                latest=independently_probe_latest_completed_spy()
                status["latest_spy_completed_session_date"]=latest
                demand(latest==target,
                       "FAST market date is not the latest completed SPY "
                       f"session: FAST={target}, probe={latest}")
                status["gates"]["FAST_LATEST_COMPLETED_SPY_DATE"]="PASS"
            status["fast_sha256"]=digest(Path(args.fast_json).read_bytes())
            m,spy_status,spycap,steps=verify_stage5(root,target)
            status["gates"]["STAGE5_OFFICIAL_SPY_AND_MACRO"]="PASS"
            spy=pd.DataFrame([
                {"Date":r["Date"],"SPY_ADJ_CLOSE":r["Adj Close"]}
                for r in spycap["rows"]
            ])
            demand(spy["Date"].is_unique, "Duplicate SPY dates")
            demand(pd.Timestamp(target) in set(pd.to_datetime(spy.Date)),
                   "SPY target date is not captured")
            gq=pd.read_csv(tmp/"GQ_original.csv",parse_dates=["Date"])
            hg=pd.read_csv(tmp/"HG_original.csv",parse_dates=["Date"]).set_index("Date")
            seed=json.loads((tmp/"HF_rekonstruktion.json").read_text())
            demand(seed.get("anchor_date")==ANCHOR and
                   seed.get("reconstruction_status")=="RESEARCH_ONLY_NONORIGINAL",
                   "Base seed is not original locked research anchor")
            kernel=he.BaseV3LiveKernel.from_seed_file(tmp/"HF_rekonstruktion.json")
            demand(pd.Timestamp(ANCHOR) in hg.index,
                   "Frozen HG anchor date missing")
            demand(int(hg.loc[pd.Timestamp(ANCHOR),"BASE_V3_REGIME"]) ==
                   int(seed["prev_regime"]), "Frozen HG and HF state anchor disagree")
            overlap=gq.dropna(subset=["EQUITY"]).set_index("Date")["EQUITY"]
            ss=spy.assign(Date=pd.to_datetime(spy.Date)).set_index("Date")["SPY_ADJ_CLOSE"]
            common=ss.index.intersection(overlap.index)
            demand(len(common)>=10,"Insufficient SPY/GQ overlap")
            # A small historical adjusted-price revision is a reason for NO-GO, not
            # for silently replacing the frozen GQ historical equity series.
            max_diff=float((ss.loc[common]-overlap.loc[common]).abs().max())
            status["spy_overlap_max_abs"]=max_diff
            demand(max_diff<=1.0, "SPY/GQ overlap is inconsistent")
            eq,_=gu.build_equity_continuation(gq,spy,pd.Timestamp(target))
            slow_common=slow_sources(root,steps,hi,bis,tmp,args.offline_slow_dir)
            if args.offline_slow_dir: status["gates"]["SHILLER_LOCAL_PARSED_RESEARCH_FALLBACK"]="NOT_PRODUCTION"
            status["gates"]["SLOW_OFFICIAL_CURRENT_RETRIEVAL"]="PASS_PIT_B"
            dates=[x.strftime("%Y-%m-%d") for x in pd.bdate_range(
                pd.Timestamp(ANCHOR)+pd.offsets.BDay(1),pd.Timestamp(target))]
            demand(0<len(dates)<=args.max_bridge_days, "Business-day bridge invalid / too long")
            status["bridge_business_days"]=len(dates)
            archived=None
            if args.pit_zip:
                archived=zipfile.ZipFile(args.pit_zip)
                bst=readjson(archived.read("BACKFILL_STATUS.json"))
                demand(bst["successful_requests"]==bst["expected_requests"]==54 and
                       set(bst["tested_dates"])>=set(dates),
                       "Historical 54 PIT set does not cover this bridge")
            pitdir=out/"fred_pit_archive"
            pitdir.mkdir(parents=True,exist_ok=True)
            history={pd.Timestamp(k):float(v) for k,v in hg["MSP"].dropna().items()}
            rows=[]
            pitlog=[]
            for day in dates:
                src={}
                pit_frames={}
                for sid in SIDS:
                    if archived:
                        raw=gzip.decompress(archived.read(f"raw_pit/{day}/{sid}.json.gz"))
                        rec=next((a for a in bst["records"] if
                                  a["series"]==sid and a["market_day"]==day),None)
                        demand(rec is not None and rec["raw_sha256"]==digest(raw),
                               f"PIT raw file tampered: {day}/{sid}")
                    else:
                        raw=pit_fetch(day,sid,os.environ.get("FRED_API_KEY",""))
                    data,meta=validate_pit(raw,day,sid)
                    if day==target:
                        official=gzip.decompress(stage5_read(
                            root, "shadow_macro/"+steps[f"FRED_PIT_{sid}"]["detail"]["archive_file"]))
                        demand(readjson(raw).get("observations")==
                               readjson(official).get("observations"),
                               f"FRED Stage5 independent target-vintage mismatch: {sid}")
                    pitlog.append(meta)
                    pit_frames[sid]=data
                    (pitdir/f"{day}_{sid}.json.gz").write_bytes(gzip.compress(raw,mtime=0))
                # Preserve strict HI source availability policies.
                slowdict={}
                for sid,(key,policy) in SLOW.items():
                    slowdict[key]=hi.stamp_available(pit_frames[sid],policy)
                slowdict.update(slow_common)
                slower=hi.component_scores_from_sources(slowdict,pd.Timestamp(day))
                vix=pit_frames["VIXCLS"].rename(columns={"value":"VIXCLS"})
                baa=pit_frames["BAA10Y"].rename(columns={"value":"BAA10Y"})
                aaa=pit_frames["AAA10Y"].rename(columns={"value":"AAA10Y"})
                mkt,ma200,_=gu.market_score_from_equity(eq.loc[:day],pd.Timestamp(day))
                vol,vix_date,_=gu.native_current_score(vix,"VIXCLS",pd.Timestamp(day),True)
                cr1,bdate,_=gu.native_current_score(baa,"BAA10Y",pd.Timestamp(day),True)
                cr2,adate,_=gu.native_current_score(aaa,"AAA10Y",pd.Timestamp(day),True)
                is_actual_spy_session=pd.Timestamp(day) in set(ss.index)
                if is_actual_spy_session:
                    demand(pd.Timestamp(vix_date).date()==dt.date.fromisoformat(day),
                           f"VIX observation not current on market session {day}")
                else:
                    demand((pd.Timestamp(day)-pd.Timestamp(vix_date)).days<=4,
                           f"Holiday bridge VIX too old: {day}")
                demand(max((pd.Timestamp(day)-pd.Timestamp(bdate)).days,
                           (pd.Timestamp(day)-pd.Timestamp(adate)).days)<=4,
                       f"Credit observation older than 4 days: {day}")
                credit=float(np.mean([cr1,cr2]))
                msp=float(np.mean([mkt,credit,slower["N_NFCI"],vol]))
                lag_date=(pd.Timestamp(day)-pd.offsets.BDay(20)).normalize()
                if lag_date in history:
                    lag_value=history[lag_date]
                elif lag_date in hg.index and pd.notna(hg.loc[lag_date,"MSP_D20"]):
                    lag_value=float(hg.loc[lag_date,"MSP"]-hg.loc[lag_date,"MSP_D20"])
                else:
                    raise RuntimeError(f"No exact 20-BDay MSP historical lag: {lag_date}")
                d20=msp-lag_value
                before=(kernel.prev_regime,kernel.fallrun)
                check=independent_step(before[0],before[1],float(mkt),credit,
                    slower["N_NFCI"],float(vol),slower["VP_VALUATION"],
                    slower["VP_CURVE"],slower["VP_CREDITCYCLE"],d20)
                calc=kernel.update(float(mkt),credit,slower["N_NFCI"],float(vol),
                    slower["VP_VALUATION"],slower["VP_CURVE"],
                    slower["VP_CREDITCYCLE"],slower["MACRO_CONFIRM"],d20)
                demand(calc["BASE_REGIME"]==check[0] and
                       abs(calc["VP"]-check[1])<1e-9 and
                       abs(calc["MSP"]-check[2])<1e-9 and
                       kernel.fallrun==check[3],f"Independent kernel mismatch: {day}")
                history[pd.Timestamp(day)]=msp
                rows.append({
                    "Date":day,
                    "DAY_TYPE":"MARKET_SESSION" if is_actual_spy_session else "WEEKDAY_HOLIDAY_BRIDGE",
                    "BASE_REGIME":int(calc["BASE_REGIME"]),
                    "VP":float(calc["VP"]),"MSP":float(calc["MSP"]),
                    "MSP_D20":float(d20),
                    "MSP_MARKET":float(mkt),"MSP_CREDIT_CONTROL":credit,
                    "MSP_VOL":float(vol),"N_NFCI":float(slower["N_NFCI"]),
                    "VP_VALUATION":float(slower["VP_VALUATION"]),
                    "VP_CURVE":float(slower["VP_CURVE"]),
                    "VP_CREDITCYCLE":float(slower["VP_CREDITCYCLE"]),
                    "N_SAHM":float(slower["N_SAHM"]),
                    "VIX_SOURCE_DATE":pd.Timestamp(vix_date).date().isoformat(),
                    "BAA_SOURCE_DATE":pd.Timestamp(bdate).date().isoformat(),
                    "AAA_SOURCE_DATE":pd.Timestamp(adate).date().isoformat(),
                    "SOURCE_NOTE":"RESEARCH_ONLY__PIT_A_FRED__PIT_B_SPY_SHILLER_BIS",
                })
            if archived:archived.close()
            demand(len(rows)==len(dates) and rows[-1]["Date"]==target,
                   "Incomplete BASE bridge")
            with (out/"TAGESKANDIDATEN_RESEARCH.csv").open("w",newline="",encoding="utf-8") as f:
                w=csv.DictWriter(f,fieldnames=list(rows[0]))
                w.writeheader();w.writerows(rows)
            cand=rows[-1]
            with (out/"BASE_KANDIDAT_NICHT_FREIGEGEBEN.csv").open("w",newline="",encoding="utf-8") as f:
                w=csv.DictWriter(f,fieldnames=["Date","BASE_REGIME","VP","MSP","SOURCE_NOTE"])
                w.writeheader();w.writerow({k:cand[k] for k in w.fieldnames})
            # No state seed is written. ONLY a proposed state JSON.
            state={"schema":"IVY11_BASE_STAGE6_SHADOW_STATE_V1",
                   "anchor_date":target,"prev_regime":kernel.prev_regime,
                   "fallrun":kernel.fallrun,"production_approved":False,
                   "kernel":"BASE_V3_RECONSTRUCTED","research_only":True}
            (out/"STATE_VORSCHLAG_NICHT_PRODUKTIV.json").write_text(
                json.dumps(state,indent=2)+"\n",encoding="utf-8")
            (out/"PIT_QUELLEN_MANIFEST.json").write_text(
                json.dumps(pitlog,indent=2)+"\n",encoding="utf-8")
            status.update({
                "actual_candidate_computed":True,
                "source_gate":"PASS_RESEARCH_NOT_RELEASE",
                "candidate":{"date":target,"regime_research":cand["BASE_REGIME"],
                             "vp_research":cand["VP"],"msp_research":cand["MSP"]},
                "files_sha256":{name:digest((out/name).read_bytes()) for name in [
                    "TAGESKANDIDATEN_RESEARCH.csv",
                    "BASE_KANDIDAT_NICHT_FREIGEGEBEN.csv",
                    "STATE_VORSCHLAG_NICHT_PRODUKTIV.json",
                    "PIT_QUELLEN_MANIFEST.json"
                ]},
                "gates":{**status["gates"],
                  "FRED_PIT_EACH_DAY":"PASS",
                  "RECONSTRUCTED_HE_HF_CALCULATED":"PASS_RESEARCH",
                  "INDEPENDENT_RULE_CROSSCHECK":"PASS",
                  "SPY_HISTORIC_PIT_PROVEN":"BLOCKED",
                  "SHILLER_BIS_HISTORIC_VINTAGE":"BLOCKED",
                  "FAST_BASE_SAME_PUBLISHING_CUTOFF":"BLOCKED",
                  "INDEPENDENT_PROSPECTIVE_20_SESSIONS":"BLOCKED",
                  "PRODUCTION_RELEASE":"NO_GO"
                }
            })
    except Exception as exc:
        status["errors"].append(f"{type(exc).__name__}: {str(exc)[:280]}")
        status["source_gate"]="FAIL"
    finally:
        save_status()
    return status

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--bundle",required=True)
    p.add_argument("--fast-json",required=True)
    grp=p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--evidence-zip")
    grp.add_argument("--evidence-dir")
    p.add_argument("--pit-zip",help="Historical 54-response archive for offline replay")
    p.add_argument("--offline-slow-dir",help="Explicit offline Shiller pre-parsed input; never use for GitHub daily")
    p.add_argument("--out",required=True)
    p.add_argument("--require-latest-closed-spy-date",action="store_true",
                   help="For daily GitHub scheduling: refuse stale FAST vs independent SPY date probe")
    p.add_argument("--max-market-age-days",type=int,default=5)
    p.add_argument("--max-bridge-days",type=int,default=60)
    args=p.parse_args()
    result=run(args)
    print(json.dumps({k:v for k,v in result.items() if k!="gates"},
                     indent=2,ensure_ascii=False))
    # FAIL for missing real candidate; but NO_GO itself is expected and safe
    return 0 if result["actual_candidate_computed"] else 1

if __name__=="__main__":
    sys.exit(main())
