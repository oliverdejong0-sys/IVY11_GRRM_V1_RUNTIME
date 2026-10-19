#!/usr/bin/env python3
"""IVY11 BASE V3 release bridge (V1): fail-closed, local/offline, diagnostic only.

No network. Existing frozen BASE anchor is NEVER modified.
Production publishing is deliberately NOT implemented: qualified release requires
an independently authorized certificate and separately reviewed workflow patch.

This script can:
 - audit real FAST/FULL public snapshots against their real BASE anchor
 - verify cryptographically bound candidate, source evidence, approval
 - stage ORIGINAL FULL builder in an isolated repository copy, never live docs
 - self-test security and integration with synthetic fixtures
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import hmac
import io
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any

SCHEMA = "IVY11_BASE_RELEASE_EVIDENCE_V1"
REQUIRED = {
    "SPY", "VIXCLS", "BAA10Y", "AAA10Y",
    "NFCI", "T10Y3M", "SAHMREALTIME",
    "CAPE", "BIS_CREDIT_GAP", "BIS_DSR",
}
FAST_SCHEMA = "IVY11_GRRM_V1_FAST_DAILY_V1"
FULL_SCHEMA = "IVY11_GRRM_V1_FULL_RISK_V2"
REVISION = "V6.3_CACHE_SAFE"
TARGET_ANCHOR_NAME = "IVY11_GRRM_V1-GX_BASE_Diagnostic_Anchor_2026-10-02.csv"
ACTION_ANCHOR_NAME = "IVY11_GRRM_V1-GQ_Live_Action_Anchor.csv"
UNRELEASED_MARKERS = ("RESEARCH", "QUARANTINE", "OFFLINE_QA", "FROZEN_BASE")

def sha(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()

def fail(msg: str):
    raise ValueError(msg)

def require(condition: bool, description: str):
    if not condition:
        fail(description)

def day(s: Any) -> dt.date:
    try:
        return dt.date.fromisoformat(str(s))
    except Exception:
        fail("Date is not ISO yyyy-mm-dd")

def utc(s: Any) -> dt.datetime:
    try:
        z=dt.datetime.fromisoformat(str(s).replace("Z","+00:00"))
        require(z.tzinfo is not None,"UTC offset required")
        return z.astimezone(dt.timezone.utc)
    except (TypeError, ValueError):
        fail("UTC timestamp invalid or missing offset")

def canonical(value: dict) -> bytes:
    return json.dumps(value,sort_keys=True,ensure_ascii=False,
                      separators=(",",":"),allow_nan=False).encode("utf-8")

def load_json(path: Path) -> dict:
    x=json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(x,dict),f"JSON object expected: {path.name}")
    return x

def runtime_audit(repo: Path, today: dt.date | None = None) -> dict:
    fast=load_json(repo/"docs/latest_fast_snapshot.json")
    full=load_json(repo/"docs/latest_full_risk_snapshot.json")
    require(fast.get("schema")==FAST_SCHEMA, "FAST schema mismatch")
    require("SHADOW" in str(fast.get("action_authority","")), "FAST action authority wrong")
    require(full.get("schema")==FULL_SCHEMA,"FULL schema mismatch")
    require(full.get("runtime_revision")==REVISION,"FULL runtime revision mismatch")
    require(fast.get("target_market_date")==full.get("target_market_date"),"FAST/FULL date mismatch")
    require(fast.get("generated_utc")==full.get("generated_utc"),"FAST/FULL created timestamp mismatch")
    target=day(full["target_market_date"])
    base=day(full["base_policy"]["base_data_date"])
    generated=utc(full["generated_utc"])
    require(generated.date()>=target,"Snapshot generated before market date")
    require(base<=target,"BASE observation after market target")
    age=(target-base).days
    require(full["base_policy"]["base_staleness_calendar_days"]==age,
            "BASE staleness accounting does not match dates")
    status=full["base_policy"]["status"]
    source_note=str(full["base"].get("SOURCE_NOTE",""))
    claimed_current=status=="CURRENT"
    require(not claimed_current or base==target, "False current status: BASE date != market date")
    if today is None: today=dt.datetime.now(dt.timezone.utc).date()
    require(target<=today, "Future market target")
    fresh_target=(today-target).days<=5
    actual_ready=(claimed_current and fresh_target and
                  not any(w in source_note.upper() for w in UNRELEASED_MARKERS))
    # This audit is NOT release authority. A matching-date snapshot is not
    # proof of qualified sources. Manual certification checked elsewhere.
    result={
        "schema":"IVY11_BASE_PUBLIC_RUNTIME_AUDIT_V1",
        "market_target":target.isoformat(),
        "generated_utc":full["generated_utc"],
        "base_date":base.isoformat(),
        "base_age_calendar_days":age,
        "base_status":status,
        "source_note":source_note,
        "market_target_fresh_within_5d":fresh_target,
        "date_and_status_consistent":True,
        "has_release_provenance":False,
        "release":"NO_GO",
        "reason":("Existing snapshot has NO verified new BASE release evidence. "
                  "Matching dates would still not suffice."),
        "management_authority":"NONE",
    }
    require("NO_PORTFOLIO_ACTION" in str(full.get("authority","")),
            "FULL unexpectedly authorizes portfolio action")
    require(full.get("risk_pressure",{}).get("action_authority")=="NONE",
            "Risk Pressure authorizes action")
    return result

def evidence_payload(cert: dict) -> dict:
    return {k:v for k,v in cert.items() if k not in ("hmac_sha256","comment")}

def validate_candidate(candidate_csv: Path, expected_day: str, expected_hash: str) -> dict:
    require(candidate_csv.is_file(),"Candidate CSV missing")
    require(sha(candidate_csv.read_bytes())==expected_hash,"Candidate hash mismatch")
    with candidate_csv.open(newline="",encoding="utf-8-sig") as f:
        rows=list(csv.DictReader(f))
    require(len(rows)==1,"Candidate must contain one row")
    row=rows[0]
    require(set(("Date","BASE_REGIME","VP","MSP","SOURCE_NOTE")).issubset(row),
            "Candidate columns missing")
    require(row["Date"]==expected_day,"Candidate date not target")
    require(str(int(row["BASE_REGIME"]))==row["BASE_REGIME"] and 1<=int(row["BASE_REGIME"])<=7,
            "Regime must be integer 1..7")
    for k in ("VP","MSP"):
        f=float(row[k])
        require(math.isfinite(f) and 0<=f<=100,f"{k} out of range")
    note=row["SOURCE_NOTE"].upper()
    require(not any(term in note for term in UNRELEASED_MARKERS),
            "Candidate is an unapproved research/frozen state")
    require("BASE_V3" in note,"Unexpected candidate model")
    return row

def verify_evidence(cert: dict, *, repo: Path, candidate: Path, archive: Path,
                    key: bytes, cutoff_time: dt.datetime | None = None) -> dict:
    require(cert.get("schema")==SCHEMA,"Unsupported release evidence schema")
    mac=cert.get("hmac_sha256","")
    require(len(key)>=32,"HMAC key missing or too short")
    computed=hmac.new(key,canonical(evidence_payload(cert)),hashlib.sha256).hexdigest()
    require(isinstance(mac,str) and hmac.compare_digest(mac,computed),
            "Release evidence HMAC invalid")
    target=day(cert["target_market_date"])
    previous=day(cert["previous_base_date"])
    cutoff=utc(cert["source_cutoff_utc"])
    issued=utc(cert["authorized_utc"])
    require(previous<target,"Candidate target must advance BASE")
    require(issued>=cutoff and issued-cutoff<=dt.timedelta(days=10),
            "Approval issued too early/late relative to cutoff")
    if cutoff_time:
        require(cutoff<=cutoff_time,"Evidence describes future source cutoff")
    require(cert.get("approval_status")=="APPROVED_BASE_RELEASE",
            "Explicit independent release approval absent")
    approvals=cert.get("reviewers",[])
    require(isinstance(approvals,list) and len(set(approvals))>=2,
            "Two distinct human reviewer IDs required")
    require(cert.get("external_forward_validation") is True,
            "External prospective validation not passed")
    require(type(cert.get("prospective_sessions")) is int and
            cert["prospective_sessions"]>=20,
            "20 independent prospective sessions required")
    require(cert.get("canonical_historical_replay") is True,
            "Canonical replay not approved")
    require(cert.get("model_revision")=="BASE_V3_RECONSTRUCTED_VALIDATED",
            "Model revision not validated")
    require(cert.get("spx_corporate_actions_verified") is True,
            "SPY price/dividend provenance missing")
    require(cert.get("cutoff_alignment_fast_base") is True,
            "FAST/BASE cutoff mismatch")
    require(cert.get("slow_sources_archived_or_policy_approved") is True,
            "Shiller/BIS historical policy approval missing")
    sources=cert.get("sources",{})
    require(isinstance(sources,dict) and set(sources)==REQUIRED,
            "Required source set mismatched")
    with tempfile.TemporaryDirectory(prefix="ivy11_verified_sources_") as tmp:
        # No trusting filenames in ZIP: fixed expected paths, hashes checked.
        import zipfile
        with zipfile.ZipFile(archive) as z:
            names=z.namelist()
            require(len(names)==len(REQUIRED) and
                    len(set(names))==len(names),
                    "Source evidence zip has missing/unexpected/duplicate paths")
            for sid in sorted(REQUIRED):
                m=sources[sid]
                require(m.get("source_class") in ("PIT_A","PIT_B_EXPLICITLY_APPROVED"),
                        f"{sid}: source provenance not approved")
                if sid in ("CAPE","BIS_CREDIT_GAP","BIS_DSR"):
                    require(m["source_class"]=="PIT_A" or
                            cert.get("slow_sources_archived_or_policy_approved") is True,
                            f"{sid}: PIT-B not approved")
                avail=utc(m["available_utc"])
                retrieved=utc(m["retrieved_utc"])
                require(avail<=retrieved<=cutoff,
                        f"{sid}: retrieval after cutoff or before availability")
                require(day(m["latest_observation_date"])<=target,
                        f"{sid}: future observation date")
                name=f"sources/{sid}.raw"
                require(m.get("path")==name and name in names,
                        f"{sid}: missing evidence archive member")
                blob=z.read(name)
                require(len(blob)>0 and sha(blob)==m["sha256"],
                        f"{sid}: evidence hash mismatch")
            require(set(names)=={f"sources/{sid}.raw" for sid in REQUIRED},
                    "Unexpected source archive content")
    require(sha(archive.read_bytes())==cert.get("archive_sha256"),
            "Source evidence archive sha invalid")
    row=validate_candidate(candidate,cert["target_market_date"],
                           cert["candidate_sha256"])
    live_fast=load_json(repo/"docs/latest_fast_snapshot.json")
    require(live_fast.get("schema")==FAST_SCHEMA,"Invalid live FAST schema")
    require(live_fast.get("target_market_date")==cert["target_market_date"],
            "FAST date not matching release candidate")
    require(sha((repo/"docs/latest_fast_snapshot.json").read_bytes())==
            cert.get("fast_sha256"),"FAST source digest mismatch")
    require(utc(live_fast["generated_utc"])>=cutoff,
            "FAST generated before evidence cutoff")
    frozen=repo/"data"/TARGET_ANCHOR_NAME
    require(frozen.exists(),"Frozen BASE anchor missing")
    with frozen.open(encoding="utf-8-sig",newline="") as f:
        frozen_rows=list(csv.DictReader(f))
    require(len(frozen_rows)==1,"Frozen BASE anchor must have one row")
    require(frozen_rows[0]["Date"]==cert.get("frozen_anchor_date","2026-10-02"),
            "Frozen BASE anchor tampered")
    return {"release_eligible":True,"candidate":row,"target":target.isoformat(),
            "source_count":len(REQUIRED),"human_approvals":len(set(approvals)),
            "model_status":"BASE_V3_RECONSTRUCTED_VALIDATED",
            "production_commit_permitted":False}

def run_staged_full(repo: Path, candidate: Path, *, result_dir: Path) -> dict:
    """Use the ORIGINAL full-builder code in isolated clone; no production writes."""
    with tempfile.TemporaryDirectory(prefix="ivy11_stage_full_") as tmp:
        fake=Path(tmp)
        for d in ("src","docs","data"):
            (fake/d).mkdir()
        for src in ("docs/latest_fast_snapshot.json",
                    f"data/{ACTION_ANCHOR_NAME}",
                    "src/IVY11_GRRM_V1_build_full_risk_snapshot.py"):
            x=repo/src
            require(x.is_file(),f"Repo file missing: {src}")
            (fake/src).write_bytes(x.read_bytes())
        (fake/"data"/TARGET_ANCHOR_NAME).write_bytes(candidate.read_bytes())
        pr=subprocess.run([sys.executable,
                           str(fake/"src/IVY11_GRRM_V1_build_full_risk_snapshot.py")],
                          cwd=fake,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                          text=True,timeout=60)
        require(pr.returncode==0,"Original FULL builder failed in sandbox: "+pr.stderr[-500:])
        new=load_json(fake/"docs/latest_full_risk_snapshot.json")
        require(new["schema"]==FULL_SCHEMA,"Staged FULL schema mismatch")
        require(new["runtime_revision"]==REVISION,"Staged FULL revision mismatch")
        require(new["base_policy"]["status"]=="CURRENT","Staged builder did not produce CURRENT")
        require(new["base_policy"]["base_valid_for_management"] is True,
                "Staged builder marked new BASE invalid")
        require(new["base_policy"]["base_data_date"]==new["target_market_date"],
                "Staged BASE date != market target")
        require(new.get("authority")=="DIAGNOSTIC_ONLY / NO_PORTFOLIO_ACTION",
                "Staged FULL grants portfolio authority")
        result_dir.mkdir(parents=True,exist_ok=True)
        j=result_dir/"STAGED_FULL_NOT_PUBLISHED.json"
        j.write_bytes((fake/"docs/latest_full_risk_snapshot.json").read_bytes())
        (result_dir/"STAGED_FULL_NOT_PUBLISHED.md").write_bytes(
            (fake/"docs/latest_full_risk_snapshot.md").read_bytes())
        return {
            "stage":"PASS",
            "target":new["target_market_date"],
            "base_status":new["base_policy"]["status"],
            "base_date":new["base_policy"]["base_data_date"],
            "full_sha256":sha(j.read_bytes()),
            "publish":"NEVER_FROM_STAGE4",
        }

def preflight(repo: Path, out: Path, candidate: Path | None = None,
              evidence: Path | None = None, archive: Path | None = None,
              key: bytes | None = None) -> dict:
    out.mkdir(parents=True,exist_ok=True)
    r=runtime_audit(repo)
    r["candidate_gate"]="NOT_SUBMITTED"
    r["stage"]="NOT_RUN"
    if candidate is not None or evidence is not None or archive is not None:
        try:
            require(all(x is not None for x in (candidate,evidence,archive)),
                    "Candidate/evidence/archive must be supplied together")
            require(key is not None,"HMAC key missing: cannot approve candidate")
            z=verify_evidence(load_json(evidence),repo=repo,
                              candidate=candidate,archive=archive,key=key)
            r["candidate_gate"]="CERTIFIED_FOR_ISOLATED_STAGE_ONLY"
            r["gate_detail"]=z
            r["staged_builder"]=run_staged_full(repo,candidate,result_dir=out)
            r["stage"]="PASS"
            r["release"]="NO_GO"  # Only a later externally reviewed publisher may release
        except Exception as ex:
            r["candidate_gate"]="BLOCKED"
            r["block_reason"]=str(ex)[:250]
            r["release"]="NO_GO"
    (out/"BASE_RELEASE_PREFLIGHT_STATUS.json").write_text(
        json.dumps(r,ensure_ascii=False,indent=2),encoding="utf-8")
    return r


def capture_spy_forward(repo: Path, out: Path, *, fetcher=None,
                        clock: dt.datetime | None = None) -> dict:
    """Prospective SPY *provider-output* archive from now on.
    Not a historical adjusted-close vintage, not an independent corporate-action check.
    """
    if clock is None: clock=dt.datetime.now(dt.timezone.utc)
    require(clock.tzinfo is not None,"Capture clock lacks timezone")
    fast=load_json(repo/"docs/latest_fast_snapshot.json")
    require(fast.get("schema")==FAST_SCHEMA,"Capture FAST invalid")
    target=day(fast["target_market_date"])
    now=clock.astimezone(dt.timezone.utc)
    require(dt.timedelta(0)<=now.date()-target<=dt.timedelta(days=5),
            "FAST market date stale/future: capture blocked")
    if fetcher is None:
        import yfinance as yf
        def fetcher(start,end):
            return yf.Ticker("SPY").history(start=start,end=end,auto_adjust=False,
                                            actions=True,raise_errors=True)
    data=fetcher((target-dt.timedelta(days=90)).isoformat(),
                 (target+dt.timedelta(days=2)).isoformat())
    import pandas as pd
    require(isinstance(data,pd.DataFrame) and not data.empty,
            "SPY feed returned empty/invalid frame")
    data=data.copy()
    require("Close" in data.columns and "Adj Close" in data.columns,
            "SPY provider output lacks unadjusted/adjusted Close")
    require("Dividends" in data.columns and "Stock Splits" in data.columns,
            "SPY corporate-action output missing")
    idx=pd.to_datetime(data.index)
    require(not idx.has_duplicates,"SPY duplicate dates")
    dates=[x.date().isoformat() for x in idx]
    require(dates==sorted(dates),"SPY dates not sorted")
    require(target.isoformat() in dates,
            "SPY source lacks required completed target date")
    known=("Open","High","Low","Close","Adj Close","Volume","Dividends","Stock Splits")
    rows=[]
    for d,r in zip(dates,data.to_dict(orient="records")):
        q={"Date":d}
        for col in known:
            f=float(r[col]) if col in r else None
            require(f is None or math.isfinite(f),
                    "SPY price/event data NaN or inf")
            q[col]=f
        require(q["Close"]>0 and q["Adj Close"]>0,"Invalid SPY daily close")
        rows.append(q)
    latest=next(r for r in rows if r["Date"]==target.isoformat())
    snapshot={
        "schema":"IVY11_BASE_SPY_PROSPECTIVE_CAPTURE_V1",
        "provider":"Yahoo Finance via yfinance Python Ticker.history",
        "data_character":"PROVIDER_TRANSFORMED_OUTPUT_NOT_ORIGINAL_EXCHANGE_RAW",
        "capture_utc":now.isoformat(),
        "target_market_date":target.isoformat(),
        "start_date":rows[0]["Date"],
        "end_date":rows[-1]["Date"],
        "observations":len(rows),
        "events_columns":["Dividends","Stock Splits"],
        "target_unadjusted_close":latest["Close"],
        "target_dividend_reported":latest["Dividends"],
        "target_split_reported":latest["Stock Splits"],
        "historical_adjusted_close_independently_pit_verified":False,
        "qualified_for_base_release":False,
        "diagnostic_only":True,
        "rows_sha256":sha(canonical({"rows":rows})),
    }
    out.mkdir(parents=True,exist_ok=True)
    filename="SPY_PROSPECTIVE_CAPTURE_"+now.strftime("%Y%m%dT%H%M%SZ")
    (out/(filename+".json")).write_text(
        json.dumps({"manifest":snapshot,"rows":rows},indent=2,allow_nan=False))
    (out/"SPY_PROSPECTIVE_STATUS.json").write_text(
        json.dumps(snapshot,indent=2))
    return snapshot


SSGA_SPY_DISTRIBUTIONS_URL = (
    "https://www.ssga.com/library-content/products/fund-data/etfs/us/"
    "spdr-etf-historical-distributions.xlsx"
)

def capture_official_ssga_distributions(out: Path, *,
                                         fetcher=None,
                                         clock: dt.datetime | None=None) -> dict:
    """Archive the unchanged official State Street SPDR distributions XLSX.

    Historical XLSX download is NOT proof of past vintages; parsing/matching
    SPY dividend ex-dates and amounts remains a separate verification gate.
    """
    import zipfile
    if clock is None: clock=dt.datetime.now(dt.timezone.utc)
    require(clock.tzinfo is not None,"Capture requires UTC-aware time")
    now=clock.astimezone(dt.timezone.utc)
    if fetcher is None:
        import requests
        def fetcher(url):
            return requests.get(
                url,timeout=(12,45),
                headers={"User-Agent":"IVY11-GRRM-ReadOnly-Research/1.0",
                         "Accept":"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*"},
                allow_redirects=True)
    try:
        response=fetcher(SSGA_SPY_DISTRIBUTIONS_URL)
        require(int(response.status_code)==200,
                f"Official distribution download HTTP {response.status_code}")
        data=response.content
        require(len(data)>=100,"SSGA XLSX empty/too small")
        require(zipfile.is_zipfile(io.BytesIO(data)),
                "Official distribution URL did not return an XLSX ZIP")
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names=set(z.namelist())
            require("[Content_Types].xml" in names and
                    "xl/workbook.xml" in names,
                    "Missing essential XLSX structure")
            require(z.testzip() is None,"Corrupt official XLSX archive")
        out.mkdir(parents=True,exist_ok=True)
        fn="SSGA_SPY_DISTRIBUTIONS_"+now.strftime("%Y%m%dT%H%M%SZ")+".xlsx"
        (out/fn).write_bytes(data)
        result={
            "schema":"IVY11_BASE_SPY_OFFICIAL_DISTRIBUTION_ARCHIVE_V1",
            "source":"State Street SPDR Historical Distributions XLSX",
            "source_url":SSGA_SPY_DISTRIBUTIONS_URL,
            "downloaded_utc":now.isoformat(),
            "raw_file":fn,
            "size_bytes":len(data),
            "sha256":sha(data),
            "parsed_spy_ex_dates":False,
            "matched_to_yahoo_spy_adjustments":False,
            "historical_vintage_guaranteed":False,
            "independent_spy_price_source":False,
            "release_authority":"NONE",
        }
        (out/"SSGA_DIVIDEND_STATUS.json").write_text(
            json.dumps(result,ensure_ascii=False,indent=2))
        return result
    except Exception:
        # Fail explicitly without vendor HTML/log tokens.
        raise RuntimeError(
            "Official SSGA distribution XLSX retrieval/structure validation failed"
        ) from None

def selftest(repo: Path) -> dict:
    # Local tests must be deterministic; no online source or mutable GitHub state.
    cases=[]
    def check(name, fn):
        try:
            fn()
            cases.append((name,"PASS"))
        except Exception as e:
            cases.append((name,"FAIL "+str(e)))
    def must_fail(name,fn):
        try: fn()
        except (ValueError,TypeError,KeyError,FileNotFoundError,RuntimeError,AssertionError):
            cases.append((name,"PASS"))
            return
        cases.append((name,"FAIL missed rejection"))
    check("canonical_deterministic",lambda: require(canonical({"b":1,"a":2})==canonical({"a":2,"b":1}),"Canonical not stable"))
    must_fail("date_invalid",lambda:day("2026-13-01"))
    must_fail("utc_missing_timezone",lambda:utc("2026-10-19T12:00:00"))
    check("runtime_real",lambda: require(runtime_audit(repo)["release"]=="NO_GO","False GO"))
    origin=load_json(repo/"docs/latest_full_risk_snapshot.json")
    fast=load_json(repo/"docs/latest_fast_snapshot.json")
    with tempfile.TemporaryDirectory() as tmp:
        q=Path(tmp)
        shutil.copytree(repo/"docs",q/"docs")
        shutil.copytree(repo/"data",q/"data")
        (q/"docs/latest_fast_snapshot.json").write_text(json.dumps(fast))
        mod=json.loads(json.dumps(origin))
        mod["base_policy"]["status"]="CURRENT"
        (q/"docs/latest_full_risk_snapshot.json").write_text(json.dumps(mod))
        must_fail("forged_current_wrong_date",lambda: runtime_audit(q))
        mod=json.loads(json.dumps(origin))
        mod["runtime_revision"]="BAD"
        (q/"docs/latest_full_risk_snapshot.json").write_text(json.dumps(mod))
        must_fail("runtime_revision_mismatch",lambda: runtime_audit(q))
        mod=json.loads(json.dumps(origin))
        mod["base_policy"]["base_staleness_calendar_days"]=999
        (q/"docs/latest_full_risk_snapshot.json").write_text(json.dumps(mod))
        must_fail("wrong_staleness",lambda: runtime_audit(q))
        mod=json.loads(json.dumps(origin))
        mod["authority"]="TRADE_NOW"
        (q/"docs/latest_full_risk_snapshot.json").write_text(json.dumps(mod))
        must_fail("unauthorized_cash_action",lambda: runtime_audit(q))
        mod=json.loads(json.dumps(origin))
        mod["risk_pressure"]["action_authority"]="CASH"
        (q/"docs/latest_full_risk_snapshot.json").write_text(json.dumps(mod))
        must_fail("unauthorized_risk_pressure_action",lambda: runtime_audit(q))
    # synthetic COMPLETE provenance test; not a real market or approval.
    with tempfile.TemporaryDirectory() as tmp:
        from zipfile import ZipFile,ZIP_DEFLATED
        q=Path(tmp); archive=q/"sources.zip"
        now=utc(fast["generated_utc"])-dt.timedelta(hours=1)
        cutoff=now-dt.timedelta(hours=2)
        target=day(fast["target_market_date"])
        source_data={name:(name+"|SYNTHETIC TEST").encode() for name in REQUIRED}
        with ZipFile(archive,"w",ZIP_DEFLATED) as z:
            for name in sorted(REQUIRED):
                z.writestr(f"sources/{name}.raw",source_data[name])
        evidence={
            "schema":SCHEMA,
            "target_market_date":target.isoformat(),
            "previous_base_date":"2026-10-02",
            "frozen_anchor_date":"2026-10-02",
            "source_cutoff_utc":cutoff.isoformat(),
            "authorized_utc":now.isoformat(),
            "approval_status":"APPROVED_BASE_RELEASE",
            "reviewers":["TESTER_A","TESTER_B"],
            "external_forward_validation":True,
            "prospective_sessions":20,
            "canonical_historical_replay":True,
            "model_revision":"BASE_V3_RECONSTRUCTED_VALIDATED",
            "spx_corporate_actions_verified":True,
            "cutoff_alignment_fast_base":True,
            "slow_sources_archived_or_policy_approved":True,
            "archive_sha256":sha(archive.read_bytes()),
            "fast_sha256":sha((repo/"docs/latest_fast_snapshot.json").read_bytes()),
            "sources":{
                name:{"source_class":"PIT_A",
                      "path":f"sources/{name}.raw",
                      "sha256":sha(source_data[name]),
                      "available_utc":(cutoff-dt.timedelta(hours=2)).isoformat(),
                      "retrieved_utc":(cutoff-dt.timedelta(hours=1)).isoformat(),
                      "latest_observation_date":target.isoformat()}
                for name in REQUIRED
            }
        }
        c=q/"candidate.csv"
        c.write_text("Date,BASE_REGIME,VP,MSP,SOURCE_NOTE\n"+
                     f"{target.isoformat()},4,60,30,BASE_V3_TEST_SYNTHETIC\n")
        evidence["candidate_sha256"]=sha(c.read_bytes())
        key=b"x"*32
        def sign(z):
            z["hmac_sha256"]=hmac.new(key,canonical(evidence_payload(z)),hashlib.sha256).hexdigest()
        sign(evidence)
        check("evidence_synthetic_all_fields",lambda: require(
            verify_evidence(evidence,repo=repo,candidate=c,archive=archive,key=key)["release_eligible"],
            "Synthetic only cert rejected"))
        check("original_FULL_builder_isolated_synthetic",lambda: require(
            run_staged_full(repo,c,result_dir=q/"stage")["base_status"]=="CURRENT",
            "Builder not CURRENT for same-date synthetic fixture"))
        def mutation(name, change):
            z=json.loads(json.dumps(evidence))
            change(z)
            must_fail(name,lambda: verify_evidence(z,repo=repo,candidate=c,archive=archive,key=key))
        mutation("unsigned_changed_reviewer",lambda z:z.__setitem__("reviewers",["SOLO"]))
        mutation("unsigned_sessions_claim",lambda z:z.__setitem__("prospective_sessions",100))
        mutation("unsigned_date_change",lambda z:z.__setitem__("target_market_date","2030-01-01"))
        mutation("no_source_sha",lambda z:z["sources"]["SPY"].__setitem__("sha256","0"*64))
        mutation("modified_fast_digest",lambda z:z.__setitem__("fast_sha256","0"*64))
        mutation("missing_source",lambda z:z["sources"].pop("NFCI"))
        mutation("source_future_date",lambda z:z["sources"]["NFCI"].__setitem__("latest_observation_date","2030-01-01"))
        mutation("source_retrieved_after_cutoff",lambda z:z["sources"]["NFCI"].__setitem__("retrieved_utc",(now+dt.timedelta(days=1)).isoformat()))
        mutation("approval_downgrade",lambda z:z.__setitem__("approval_status","RESEARCH_ONLY"))
        mutation("insufficient_sessions",lambda z:z.__setitem__("prospective_sessions",9))
        mutation("SPY_dividends_missing",lambda z:z.__setitem__("spx_corporate_actions_verified",False))
        mutation("slow_macro_policy_missing",lambda z:z.__setitem__("slow_sources_archived_or_policy_approved",False))
        mutation("not_external_validated",lambda z:z.__setitem__("external_forward_validation",False))
        mutation("no_cutoff_alignment",lambda z:z.__setitem__("cutoff_alignment_fast_base",False))
        mutation("model_not_approved",lambda z:z.__setitem__("model_revision","BASE_V3_RECONSTRUCTED_ONLY"))
        mutation("bad_signature",lambda z:z.__setitem__("hmac_sha256","f"*64))
        mutation("source_archive_digest_forged",lambda z:z.__setitem__("archive_sha256","0"*64))
        mutation("spoof_frozen_anchor_date",lambda z:z.__setitem__("frozen_anchor_date","2026-10-01"))
        mutation("approval_single_reviewer",lambda z:z.__setitem__("reviewers",["SAME","SAME"]))
        mutation("release_from_future",lambda z:z.__setitem__("authorized_utc",
                (cutoff-dt.timedelta(days=1)).isoformat()))
        mutation("missing_human_approval",lambda z:z.__setitem__("approval_status","UNAPPROVED"))
        mutation("no_replay",lambda z:z.__setitem__("canonical_historical_replay",False))
        mutation("future_source_availability",lambda z:z["sources"]["CAPE"].__setitem__(
                "available_utc",(cutoff+dt.timedelta(days=1)).isoformat()))

        # A synthetic FULL stage is not allowed to alter even one production file.
        protected=[repo/"data"/TARGET_ANCHOR_NAME,
                   repo/"docs/latest_full_risk_snapshot.json",
                   repo/"docs/latest_fast_snapshot.json"]
        originals=[sha(x.read_bytes()) for x in protected]
        check("test_stage_never_mutates_frozen_anchor",lambda: require(
            originals==[sha(x.read_bytes()) for x in protected],
            "Staged FULL builder mutated original repository"))
        with (q/"research.csv").open("w") as f:
            f.write("Date,BASE_REGIME,VP,MSP,SOURCE_NOTE\\n")
            f.write(f"{target.isoformat()},4,60,30,RESEARCH_ONLY_BASE_V3\\n")
        must_fail("research_candidate_rejected",lambda:validate_candidate(
            q/"research.csv",target.isoformat(),sha((q/"research.csv").read_bytes())))
        bad=c.read_bytes()
        c.write_bytes(bad+b"\n")
        must_fail("candidate_mutation_after_certification",lambda: verify_evidence(
            evidence,repo=repo,candidate=c,archive=archive,key=key))
    try:
        import pandas as pd
        sample=pd.DataFrame({
            "Open":[100.,101.],"High":[102.,103.],"Low":[99.,100.],
            "Close":[101.,102.],"Adj Close":[100.,101.],
            "Volume":[1234,2345],"Dividends":[0.,0.1],"Stock Splits":[0.,0.]},
            index=pd.to_datetime([str(day(fast["target_market_date"])-dt.timedelta(days=1)),
                                  fast["target_market_date"]]))
        # Avoid depending on whether the previous calendar day was a session:
        # synthetic sample is a provider fixture, not a live-market assertion.
        with tempfile.TemporaryDirectory() as td:
            clock=utc(fast["generated_utc"])+dt.timedelta(minutes=30)
            def good(start,end): return sample
            check("SPY_prospective_capture_synthetic",lambda: require(
                  capture_spy_forward(repo,Path(td),fetcher=good,clock=clock)
                  ["qualified_for_base_release"] is False,"Never mark as release"))
            broken=sample.drop(columns=["Dividends"])
            must_fail("SPY_requires_dividends",lambda: capture_spy_forward(
                repo,Path(td),fetcher=lambda a,b:broken,clock=clock))
            future=sample.copy()
            future.index=pd.to_datetime(["2029-01-01","2029-01-02"])
            must_fail("SPY_requires_actual_target",lambda: capture_spy_forward(
                repo,Path(td),fetcher=lambda a,b:future,clock=clock))
            missing=sample.copy()
            missing.loc[missing.index[-1],"Close"]=-1
            must_fail("SPY_rejects_negative_close",lambda: capture_spy_forward(
                repo,Path(td),fetcher=lambda a,b:missing,clock=clock))
            must_fail("SPY_rejects_future_clock",lambda: capture_spy_forward(
                repo,Path(td),fetcher=good,
                clock=utc(fast["generated_utc"])-dt.timedelta(days=20)))
    except ImportError:
        # Explicitly fail; this test requires pandas from canonical requirements.
        cases.append(("SPY_tests_require_pandas","FAIL pandas unavailable"))
    try:
        import zipfile
        with tempfile.TemporaryDirectory() as td:
            bio=io.BytesIO()
            with zipfile.ZipFile(bio,"w") as z:
                z.writestr("[Content_Types].xml","<Types/>")
                z.writestr("xl/workbook.xml","<workbook/>")
            class Resp:
                def __init__(self,status,blob):
                    self.status_code=status
                    self.content=blob
            good=lambda url: Resp(200,bio.getvalue())
            saved=capture_official_ssga_distributions(Path(td),
                  fetcher=good,clock=utc(fast["generated_utc"]))
            check("SSGA_official_synthetic_download",lambda: require(
                  saved["sha256"]==sha(bio.getvalue()) and
                  saved["release_authority"]=="NONE",
                  "SSGA download archive invalid"))
            must_fail("SSGA_http_403_rejected",lambda:capture_official_ssga_distributions(
                Path(td),fetcher=lambda url:Resp(403,bio.getvalue())))
            must_fail("SSGA_html_instead_xlsx_rejected",lambda:capture_official_ssga_distributions(
                Path(td),fetcher=lambda url:Resp(200,b"<html>blocked</html>")))
            must_fail("SSGA_zip_without_workbook_rejected",lambda:capture_official_ssga_distributions(
                Path(td),fetcher=lambda url:Resp(200,bio.getvalue()[:180])))
    except ImportError:
        cases.append(("SSGA_tests_environment","FAIL zipfile missing"))
    failures=[name+": "+s for name,s in cases if s!="PASS"]
    return {"total":len(cases),"passed":len(cases)-len(failures),
            "failures":failures,
            "tests":dict(cases),
            "release":"NO_GO",
            "synthetic_current_fixture":"NOT_A_MARKET_REGIME"}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("command",choices=("audit","preflight","selftest","capture-spy","capture-ssga"))
    ap.add_argument("--repo",default=".")
    ap.add_argument("--out",default="results")
    ap.add_argument("--candidate")
    ap.add_argument("--evidence")
    ap.add_argument("--source-archive")
    args=ap.parse_args()
    repo=Path(args.repo).resolve()
    out=Path(args.out).resolve()
    if args.command=="capture-ssga":
        result=capture_official_ssga_distributions(out)
        print(json.dumps({"raw_sha256":result["sha256"],
                          "parsed_spy_ex_dates":False,
                          "release_authority":"NONE"}))
        return 0
    if args.command=="capture-spy":
        result=capture_spy_forward(repo,out)
        print(json.dumps({"target":result["target_market_date"],
                          "rows":result["observations"],
                          "qualified_for_base_release":False}))
        return 0
    if args.command=="selftest":
        result=selftest(repo)
        out.mkdir(parents=True,exist_ok=True)
        (out/"SELFTEST.json").write_text(json.dumps(result,indent=2,ensure_ascii=False))
        print(json.dumps({"passed":result["passed"],"total":result["total"],
                          "release":result["release"]}))
        return 0 if result["passed"]==result["total"] else 1
    if args.command=="audit":
        result=preflight(repo,out)
    else:
        key=os.environ.get("IVY11_BASE_RELEASE_HMAC_KEY","").encode()
        result=preflight(repo,out,
                         Path(args.candidate) if args.candidate else None,
                         Path(args.evidence) if args.evidence else None,
                         Path(args.source_archive) if args.source_archive else None,
                         key=key if key else None)
    print(json.dumps({"release":result["release"],
                      "base_date":result["base_date"],
                      "candidate_gate":result["candidate_gate"],
                      "stage":result["stage"]},ensure_ascii=False))
    return 0 if result["release"]=="NO_GO" else 2

if __name__=="__main__":
    raise SystemExit(main())
