#!/usr/bin/env python3
"""IVY11 BASE – tägliche, nichtpublizierende Quellen- und Runtime-Abnahme.

Absichtlich KEINE BASE-Regime-Berechnung, KEIN Git-Commit, KEIN Release.
FRED/ALFRED: für den FAST-Markttag historisch as-of, nicht die später
revidierte Tageshistorie. Shiller/BIS/SPY: aktuelle Historien sind PIT-B,
nicht rückwirkend nachgewiesene historische Vintages.
"""
from __future__ import annotations
import argparse
import datetime as dt
import gzip
import hashlib
import io
import json
import math
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path

import pandas as pd
import requests

FROZEN="2026-10-02"
BUNDLE_SHA="8a11d225af945913b93e7d094d09525b989c41858907e83072e0234a0e7ec324"
SID=("VIXCLS","BAA10Y","AAA10Y","NFCI","T10Y3M","SAHMREALTIME")
EXPECTED_FAST="IVY11_GRRM_V1_FAST_DAILY_V1"
EXPECTED_FULL="IVY11_GRRM_V1_FULL_RISK_V2"
EXPECTED_RUNTIME="V6.3_CACHE_SAFE"
RANGE_LOWER={"VIXCLS":dt.timedelta(days=6),"BAA10Y":dt.timedelta(days=6),
             "AAA10Y":dt.timedelta(days=6),"T10Y3M":dt.timedelta(days=6),
             "NFCI":dt.timedelta(days=17),"SAHMREALTIME":dt.timedelta(days=75)}
SECRET_PATTERN=re.compile(r"(?i)(api_key=)[^&\s]+")

def digest(data:bytes)->str:
    return hashlib.sha256(data).hexdigest()

def iso_day(value):
    return dt.date.fromisoformat(str(value)[:10])

def require(ok,message):
    if not ok:
        raise ValueError(message)

def date_of_latest_nonmissing(frame):
    valid=frame[frame["value"].notna()]
    require(not valid.empty,"No numeric observations")
    return iso_day(valid.iloc[-1]["Date"])

def validate_fred_payload(payload,sid,asof):
    """Raw API object -> full numeric as-of history; no pagination or lookahead."""
    require(isinstance(payload,dict),"FRED response not object")
    rows=payload.get("observations")
    require(isinstance(rows,list) and len(rows)>10,"FRED observations missing/short")
    require(int(payload.get("count",len(rows)))==len(rows),"FRED paginated/truncated")
    require(int(payload.get("offset",0))==0,"FRED offset unexpected")
    require(str(payload.get("realtime_start"))==asof and
            str(payload.get("realtime_end"))==asof,
            "FRED API top-level vintage mismatch")
    rec=[]
    for r in rows:
        require(isinstance(r,dict),"FRED observation not object")
        d=iso_day(r.get("date"))
        require(d<=iso_day(asof),"FRED lookahead observation")
        if r.get("realtime_start") is not None:
            require(iso_day(r["realtime_start"])<=iso_day(asof),"FRED future vintage start")
        if r.get("realtime_end") is not None:
            require(iso_day(r["realtime_end"])>=iso_day(asof),"FRED expired vintage")
        x=r.get("value")
        value=None if x in (None,".","") else float(x)
        if value is not None:
            require(math.isfinite(value),"FRED nonfinite observation")
        rec.append((d.isoformat(),value))
    dates=[a for a,_ in rec]
    require(dates==sorted(dates) and len(set(dates))==len(dates),
            "FRED dates unsorted/duplicated")
    return pd.DataFrame(rec,columns=["Date","value"])

def fred_get(session,sid,asof,key,timeout=(8,30)):
    require(re.fullmatch(r"[a-z0-9]{32}",key or "") is not None,"FRED_API_KEY missing/invalid")
    url="https://api.stlouisfed.org/fred/series/observations"
    args={"series_id":sid,"api_key":key,"file_type":"json",
          "realtime_start":asof,"realtime_end":asof,
          "sort_order":"asc","limit":100000,"offset":0}
    try:
        response=session.get(url,params=args,timeout=timeout)
        response.raise_for_status()
        frame=validate_fred_payload(response.json(),sid,asof)
        # Never persist a request URL, headers, exception repr or secret.
        return frame,response.content
    except Exception as e:
        raise RuntimeError(f"FRED {sid}: {type(e).__name__}; no fallback, no secret logged") from None

def model_scores_nfci(frame,asof):
    """Independent implementation of frozen HI normalisation and Wednesday lag."""
    x=frame.dropna(subset=["value"]).copy()
    x["Date"]=pd.to_datetime(x["Date"])
    x["available"]=x["Date"].map(lambda z:
        z+pd.Timedelta(days=1)+pd.Timedelta(
            days=(2-(z+pd.Timedelta(days=1)).weekday())%7))
    x=x[x["available"]<=pd.Timestamp(asof)].sort_values("available")
    require(not x.empty,"No NFCI value yet available under Wednesday-lag rule")
    calendar=pd.bdate_range(x["available"].min(),asof)
    left=pd.DataFrame({"Date":calendar})
    joined=pd.merge_asof(left,x[["available","value"]].sort_values("available"),
              left_on="Date",right_on="available",direction="backward")
    values=joined["value"].dropna().to_numpy(float)
    require(len(values)>10,"NFCI rank history missing")
    latest=float(values[-1])
    rank=float(100.0*(sum(values<=latest)-0.5)/len(values))
    return {"score":rank,"raw":latest,
            "observation_date":x.iloc[-1]["Date"].date().isoformat(),
            "availability_date":x.iloc[-1]["available"].date().isoformat(),
            "normalized_days":len(values)}

def validate_runtime(fast,full,now_date):
    require(fast.get("schema")==EXPECTED_FAST,"FAST schema mismatch")
    require("SHADOW" in fast.get("action_authority",""),"FAST action authority mismatch")
    require(full.get("schema")==EXPECTED_FULL,"FULL schema mismatch")
    require(full.get("runtime_revision")==EXPECTED_RUNTIME,"FULL revision mismatch")
    target=iso_day(fast["target_market_date"])
    require(iso_day(full["target_market_date"])==target,"FAST/FULL date mismatch")
    gen_fast=fast.get("generated_utc")
    require(bool(gen_fast) and gen_fast==full.get("generated_utc"),
            "FAST/FULL generation timestamp missing or different")
    generated=dt.datetime.fromisoformat(str(gen_fast).replace("Z","+00:00"))
    require(generated.tzinfo is not None,"Generated UTC timestamp missing timezone")
    require(generated.astimezone(dt.timezone.utc).date()>=target,
            "FAST generated before target trading day")
    require(dt.date.fromisoformat(FROZEN)<=target<=now_date,"Invalid target market date")
    age=(now_date-target).days
    # On extended weekends/holidays this becomes an explicit 'missing daily snapshot'
    require(age<=5,"FAST is older than five calendar days")
    values=fast["values"]
    for key in ("MSP_MARKET","MSP_CREDIT_CONTROL","MSP_VOL"):
        v=float(values[key])
        require(math.isfinite(v) and 0<=v<=100,"FAST component nonfinite/out of range: "+key)
        f=float(full["fast_action"][key])
        require(abs(f-v)<1e-9,"FAST/FULL numerical mismatch: "+key)
    base_day=iso_day(full["base_policy"]["base_data_date"])
    require(base_day<=target,"BASE date is after FAST date")
    age_base=(target-base_day).days
    published_age=int(full["base_policy"].get("base_staleness_calendar_days",-1))
    require(published_age==age_base,"BASE staleness count mismatch")
    status=str(full["base_policy"]["status"])
    if status=="CURRENT":
        require(age_base==0,"BASE falsely marked CURRENT")
    if status=="VALID_STALE_WITHIN_CALM_POLICY":
        require(0<age_base<=7,"BASE falsely marked valid stale")
    require(status in ("CURRENT","VALID_STALE_WITHIN_CALM_POLICY","BASE_REFRESH_REQUIRED"),
            "Unknown BASE policy status")
    return {"target_market_date":target.isoformat(),"fast_staleness_days":age,
            "base_data_date":base_day.isoformat(),
            "base_age_calendar_days":(target-base_day).days,
            "base_policy_status":full["base_policy"]["status"],
            "base_regime_diagnostic_only":full["base"].get("REGIME"),
            "fast_sha256":None,"full_sha256":None}

def sanitized_error(exc):
    msg=str(exc)
    # Do not allow exception URL with api_key to leak into artifact.
    msg=SECRET_PATTERN.sub(r"\1[REDACTED]",msg)
    return (type(exc).__name__+": "+msg)[:350]

def atomic_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(value,indent=2,ensure_ascii=False,sort_keys=True)+"\n",encoding="utf-8")
    os.replace(tmp,path)

def fetch_audit(repo,out,api_key,now_date=None,session=None,research_loader=None):
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    repo=Path(repo)
    now_date=now_date or dt.datetime.now(dt.timezone.utc).date()
    session=session or requests.Session()
    audit={"schema":"IVY11_BASE_DAILY_EVIDENCE_V1",
           "execution_utc":dt.datetime.now(dt.timezone.utc).isoformat(),
           "authority":"DIAGNOSTIC_ONLY_NO_TRADING",
           "production_release":"NO_GO",
           "official_full_snapshot_changed":False,
           "state_seed_changed":False,
           "fast_pipeline_changed":False,
           "steps":[],"blocked_by":[],
           "pit_limitations":[
             "SPY historical adjusted prices not authenticated as-of for every bridge day",
             "Shiller CAPE historical daily publication vintages not completely authenticated",
             "BIS historical daily publication vintages not completely authenticated",
             "Reconstructed HE/HF not independently forward validated"]}

    def gate(name,fn):
        try:
            detail=fn()
            # Runtime return may contain local module objects; evidence JSON must
            # contain plain, non-sensitive serializable data only.
            safe_detail = detail
            try:
                json.dumps(safe_detail)
            except (TypeError, ValueError):
                safe_detail = "Loaded audited parser modules from pinned research bundle"
            audit["steps"].append({"name":name,"result":"PASS","detail":safe_detail})
            return detail
        except Exception as e:
            audit["steps"].append({"name":name,"result":"FAIL","error":sanitized_error(e)})
            audit["blocked_by"].append(name)
            return None
    try:
        def runtime_step():
            fast_bytes=(repo/"docs/latest_fast_snapshot.json").read_bytes()
            full_bytes=(repo/"docs/latest_full_risk_snapshot.json").read_bytes()
            f=json.loads(fast_bytes);j=json.loads(full_bytes)
            res=validate_runtime(f,j,now_date)
            res["fast_sha256"]=digest(fast_bytes)
            res["full_sha256"]=digest(full_bytes)
            return res
        runtime=gate("FAST_FULL_CONSISTENCY",runtime_step)
        if runtime is None:
            return audit
        audit.update(runtime)
        target=runtime["target_market_date"]
        bundle=repo/"base-parity-research-bundle.zip"
        def bundle_step():
            raw=bundle.read_bytes()
            require(digest(raw)==BUNDLE_SHA,"Research bundle original SHA256 mismatch")
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                names=set(z.namelist())
                require({"base_diagnostic/original_pruefdateien/HG_original.csv",
                         "base_diagnostic/original_pruefdateien/HI_original.py"}<=names,
                        "Research bundle frozen reference files missing")
                hg=pd.read_csv(z.open("base_diagnostic/original_pruefdateien/HG_original.csv"))
                q=hg.loc[hg["Date"]==FROZEN]
                require(len(q)==1,"Frozen HG date missing or duplicated")
                return {"frozen_nfci_score":float(q.iloc[0].N_NFCI),
                        "frozen_nfci_raw":float(q.iloc[0].NFCI),
                        "bundle_sha256":digest(raw)}
        baseline=gate("FROZEN_REFERENCE_SHA256",bundle_step)
        if baseline is None:
            return audit
        # All six FRED/ALFRED series are fetched *at the FAST market date*.
        # The NFCI anchor is separately fetched as of frozen original date.
        obtained={}
        for sid in SID:
            def retrieve(sid=sid):
                df,raw=fred_get(session,sid,target,api_key)
                last=date_of_latest_nonmissing(df)
                require(iso_day(last)>=iso_day(target)-RANGE_LOWER[sid],
                        f"FRED {sid}: source observation too old")
                file=out/"raw_asof"/(sid+"_"+target+".json.gz")
                file.parent.mkdir(parents=True,exist_ok=True)
                file.write_bytes(gzip.compress(raw,mtime=0))
                obtained[sid]=df
                return {"asof":target,"last_valid_observation":last.isoformat(),
                        "numeric_observations":int(df.value.notna().sum()),
                        "source_frequency":"MONTHLY" if sid=="SAHMREALTIME"
                          else ("WEEKLY" if sid=="NFCI" else "DAILY"),
                        "raw_sha256":digest(raw),
                        "archive_file":str(file.relative_to(out))}
            gate("FRED_PIT_"+sid,retrieve)
        def anchor_step():
            df,raw=fred_get(session,"NFCI",FROZEN,api_key)
            stats=model_scores_nfci(df,FROZEN)
            require(abs(stats["raw"]-baseline["frozen_nfci_raw"])<1e-8,
                    "Frozen ALFRED NFCI raw mismatches canonical HG")
            require(abs(stats["score"]-baseline["frozen_nfci_score"])<1e-8,
                    "Frozen ALFRED NFCI normalized mismatches canonical HG")
            file=out/"raw_asof"/("NFCI_"+FROZEN+".json.gz")
            file.parent.mkdir(parents=True,exist_ok=True)
            file.write_bytes(gzip.compress(raw,mtime=0))
            return {"asof":FROZEN,"raw":stats["raw"],"score":stats["score"],
                    "source_raw_sha256":digest(raw),"available_date":stats["availability_date"]}
        gate("ALFRED_NFCI_ANCHOR_EXACT",anchor_step)
        if "NFCI" in obtained:
            gate("ALFRED_NFCI_CURRENT_RESEARCH_COMPONENT",
                 lambda:model_scores_nfci(obtained["NFCI"],target))
        # Separate real-world CAPE / BIS acquisitions. These are *retrieval-date*
        # research observations. They are not ALFRED-style historical as-of
        # vintages, even if their time-series observations are much older.
        def load_research_sources():
            import importlib.util
            with zipfile.ZipFile(bundle) as zz:
                keep={
                    "online":"base_diagnostic/IVY11_BASE_Online_Abnahme.py",
                    "bis":"base_diagnostic/original_pruefdateien/HH_BIS_Official_Schema_Adapter.py"}
                with tempfile.TemporaryDirectory() as temp:
                    modules={}
                    for label,path in keep.items():
                        fp=Path(temp)/(label+".py")
                        fp.write_bytes(zz.read(path))
                        spec=importlib.util.spec_from_file_location("ivy_daily_"+label,str(fp))
                        mod=importlib.util.module_from_spec(spec)
                        sys.modules[spec.name]=mod
                        spec.loader.exec_module(mod)
                        modules[label]=mod
                    return modules["online"],modules["bis"]
        research_modules=gate("RESEARCH_PARSERS_FROM_PINNED_BUNDLE",research_loader or load_research_sources)
        if research_modules:
            online,bis=research_modules
            def record_binary(name,raw,info):
                path=out/"retrieval_date_only"/(name+".raw.gz")
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(gzip.compress(raw,mtime=0))
                info.update({"retrieved_utc":dt.datetime.now(dt.timezone.utc).isoformat(),
                     "pit_class":"PIT_B_CURRENT_REVISED_HISTORY_NOT_PROVED_AS_OF",
                     "raw_sha256":digest(raw),"archive_file":str(path.relative_to(out))})
                return info
            def cape_capture():
                cape,provenance,raw=online.fetch_shiller_official(session)
                require(len(cape)>1000,"Shiller CAPE history too short")
                return record_binary("SHILLER_CAPE",raw,{
                    "provider":provenance,"observations":len(cape),
                    "last_observation":str(cape.Date.max().date())})
            gate("SHILLER_RETRIEVAL_ONLY_PIT_B",cape_capture)
            for flow,key,label in (("WS_CREDIT_GAP","Q.US.P.A.C","BIS_CREDIT_GAP"),
                                   ("WS_DSR","Q.US.P","BIS_DSR")):
                def bis_capture(flow=flow,key=key,label=label):
                    frame,provenance,raw=online.fetch_bis_official(session,bis,flow,key)
                    require(len(frame)>=40,"BIS history too short")
                    return record_binary(label,raw,{"provider":provenance,
                        "observations":len(frame),"last_period":str(frame.Period.max())})
                gate(label+"_RETRIEVAL_ONLY_PIT_B",bis_capture)
        # SOURCE gates do not equate to a BASE regime. Separate prospective
        # slow-data vintages / SPY action continuation and independent HE testing
        # remain release prerequisites. No output is named latest_base*.
    finally:
        audit["source_evidence_ok"]=bool(
            not audit["blocked_by"]
            and len([s for s in audit["steps"] if s["name"].startswith("FRED_PIT_")])==6)
        audit["base_regime_currently_publishable"]=False
        audit["daily_regime_ready"]=False
        audit["production_release"]="NO_GO"
        atomic_json(out/"DAILY_READINESS_STATUS.json",audit)
        report=[
            "# IVY11 BASE Daily Evidence – Research/NO-GO",
            "",
            f"Executed UTC: {audit['execution_utc']}",
            f"FAST date: {audit.get('target_market_date','UNKNOWN')}",
            f"BASE date: {audit.get('base_data_date','UNKNOWN')}",
            f"BASE status: {audit.get('base_policy_status','UNKNOWN')}",
            "",
            "| Gate | Status |",
            "|---|---|",
        ]
        report += [f"| {x['name']} | {x['result']} |" for x in audit["steps"]]
        report += ["","## Freigabe","**NO-GO**. No BASE model/state published.",
                   "", "## Unresolved evidence",
                   *["- "+x for x in audit["pit_limitations"]]]
        (out/"DAILY_READINESS_REPORT.md").write_text("\n".join(report)+"\n",encoding="utf-8")
    return audit

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--repo",type=Path,default=Path("."))
    ap.add_argument("--out",type=Path,required=True)
    a=ap.parse_args()
    status=fetch_audit(a.repo,a.out,os.environ.get("FRED_API_KEY"))
    print(json.dumps({"fast_date":status.get("target_market_date"),
          "base_date":status.get("base_data_date"),
          "source_evidence_ok":status.get("source_evidence_ok"),
          "blocked_by":status["blocked_by"],
          "production_release":status["production_release"]},indent=2))
    # No false success when inputs or official source feeds are invalid.
    return 2 if status["blocked_by"] else 0

if __name__=="__main__":
    raise SystemExit(main())
