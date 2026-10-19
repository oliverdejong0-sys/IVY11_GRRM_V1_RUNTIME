#!/usr/bin/env python3
"""Read-only nine-business-day FRED/ALFRED as-of backfill.

All saved data are historical observations as-of each requested decision date.
This is NOT a BASE model run, independent forward validation, or release.
"""
from __future__ import annotations
import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import time

import requests

DATES=["2026-10-05","2026-10-06","2026-10-07","2026-10-08","2026-10-09",
       "2026-10-12","2026-10-13","2026-10-14","2026-10-15"]
SERIES=["VIXCLS","BAA10Y","AAA10Y","NFCI","T10Y3M","SAHMREALTIME"]
URL="https://api.stlouisfed.org/fred/series/observations"
KEY_PATTERN=r"[a-z0-9]{32}"

def check(ok,msg):
    if not ok: raise ValueError(msg)

def sha(raw):
    return hashlib.sha256(raw).hexdigest()

def validate_history(payload, day, sid):
    check(isinstance(payload,dict),"FRED not object")
    check(payload.get("realtime_start")==day and payload.get("realtime_end")==day,
          "Top level has wrong vintage")
    arr=payload.get("observations")
    check(isinstance(arr,list) and len(arr)>20,"Empty/short history")
    check(int(payload.get("count",-1))==len(arr) and int(payload.get("offset",-1))==0,
          "FRED history truncated/paginated")
    prev=None
    usable=0
    latest_date=None
    for r in arr:
        day_obs=dt.date.fromisoformat(str(r["date"]))
        check(day_obs<=dt.date.fromisoformat(day),"Future observation date")
        check(str(r.get("realtime_start",""))<=day
             and str(r.get("realtime_end",""))>=day,
             "Unexpected row-vintage span")
        check(prev is None or prev<day_obs,"Duplicate/unsorted dates")
        prev=day_obs
        v=r.get("value")
        if v not in (None,"","."):
            val=float(v)
            check(abs(val)!=float("inf") and val==val,"Infinite/NaN number")
            usable+=1
            latest_date=day_obs.isoformat()
    check(usable>=20,"Too few valid observations")
    return {"market_day":day,"series":sid,"count":len(arr),
            "valid":usable,"latest_observation":latest_date}

def retrieve_one(session,key,day,sid,timeout=(10,28),retries=2):
    args={
      "series_id":sid,"api_key":key,"file_type":"json",
      "realtime_start":day,"realtime_end":day,
      "observation_start":"1900-01-01","sort_order":"asc",
      "offset":0,"limit":100000,
    }
    fail=None
    for attempt in range(retries):
        try:
            response=session.get(URL,params=args,timeout=timeout)
            response.raise_for_status()
            raw=response.content
            record=validate_history(response.json(),day,sid)
            return raw,record
        except Exception as exc:
            # Never print urllib exception: URL could include the secret key.
            fail=type(exc).__name__
            if attempt+1<retries:time.sleep(min(5,2**attempt))
    raise RuntimeError("Source request/validation unsuccessful: "+str(fail))

def collect(out:Path,key:str,session=None,days=None,series=None):
    out=Path(out)
    out.mkdir(parents=True,exist_ok=True)
    days_copy=list(DATES) if days is None else list(days)
    series_copy=list(SERIES) if series is None else list(series)
    check(re.fullmatch(KEY_PATTERN,key or "") is not None,"FRED API key absent/invalid (never logged)")
    check(len(days_copy)==len(set(days_copy)) and
          all(dt.date.fromisoformat(q).weekday()<5 for q in days_copy),
          "Invalid/backfill dates")
    session=session or requests.Session()
    status={
      "schema":"IVY11_BASE_9DAY_PIT_ASOF_BACKFILL_V1",
      "created_utc":dt.datetime.now(dt.timezone.utc).isoformat(),
      "role":"EXTERNAL_SOURCE_EVIDENCE_ONLY",
      "tested_dates":days_copy,"series":series_copy,
      "expected_requests":len(days_copy)*len(series_copy),
      "successful_requests":0,"records":[],
      "publication":"NO_GO",
      "model_computation":"NOT_PERFORMED",
      "portfolio_action":"NONE",
      "secrets_logged":False,
      "errors":[],
    }
    archive_dir=out/"raw_pit"
    archive_dir.mkdir(exist_ok=True)
    try:
        for day in days_copy:
            for sid in series_copy:
                try:
                    raw,detail=retrieve_one(session,key,day,sid)
                    relative=f"raw_pit/{day}/{sid}.json.gz"
                    destination=out/relative
                    destination.parent.mkdir(parents=True,exist_ok=True)
                    destination.write_bytes(gzip.compress(raw,mtime=0))
                    detail.update({"raw_sha256":sha(raw),"archive":relative})
                    status["records"].append(detail)
                    status["successful_requests"]+=1
                except Exception as exc:
                    # Only curated names + exception classes; no URL / API key.
                    status["errors"].append({
                        "market_day":day,"series":sid,
                        "failure_type":type(exc).__name__,
                        "reason":"FAILED_CLOSED",
                    })
        status["all_sources_passed"]=(status["successful_requests"]
                                ==status["expected_requests"] and not status["errors"])
        (out/"BACKFILL_STATUS.json").write_text(
            json.dumps(status,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        lines=["# IVY11 BASE - archivierter FRED/ALFRED Tages-Backfill",
               "","**Read-only / kein BASE-Regime / kein Release**",
               "",
               f"- Erfolgreiche Datenabfragen: {status['successful_requests']}/{status['expected_requests']}",
               f"- Fehlerfälle: {len(status['errors'])}",
               "- Diese Quellenabnahme ersetzt nicht SPY-/Shiller-/BIS-PIT oder unabhängige Regimevalidierung.",
               "- Die ZIP enthält nur FRED-Rohdaten, Metadaten und SHA-Prüfsummen.",
               ]
        (out/"BACKFILL_BERICHT.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
        return status
    except Exception:
        raise

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--out",type=Path,default=Path("base_pit_backfill"))
    a=ap.parse_args()
    # Do not write secrets to logs.
    key=os.environ.get("FRED_API_KEY","")
    if not re.fullmatch(KEY_PATTERN,key):
        a.out.mkdir(parents=True,exist_ok=True)
        (a.out/"BACKFILL_STATUS.json").write_text(
            json.dumps({"status":"NO_GO","error":"FRED_API_KEY missing or invalid",
                        "publication":"NO_GO","no_secret_logged":True},indent=2))
        raise SystemExit(2)
    outcome=collect(a.out,key)
    print(json.dumps({"successful_requests":outcome["successful_requests"],
          "expected_requests":outcome["expected_requests"],
          "all_sources_passed":outcome["all_sources_passed"],
          "publication":"NO_GO"}))
    raise SystemExit(0 if outcome["all_sources_passed"] else 1)

if __name__=="__main__":main()
