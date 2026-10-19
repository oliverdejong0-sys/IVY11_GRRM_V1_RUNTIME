#!/usr/bin/env python3
# coding: utf-8
"""IVY11 BASE Stage 5: official SPY distributions, corporate actions, audit cutoffs.
READ ONLY. Invariant: this script has no publication path, no BASE values and
does not amend the IVY4-out-of-11 portfolio.
Standard-library-only XLSX reader intentionally parses raw, unchanged SSGA
workbook XML. Supports shadow ZIP or the directory tree generated on GitHub.
"""
from __future__ import annotations
import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

SCHEMA="IVY11_BASE_STAGE5_SPY_OFFICIAL_AUDIT_V1"
SHEETS_NS="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OFFICIAL_COLUMNS=("FUND NAME","TICKER","CUSIP","EX-DATE","RECORD DATE",
                  "PAYABLE DATE","DIVIDEND ($)","SHORT TERM CAPITAL GAIN ($)",
                  "LONG TERM CAPITAL GAIN ($)","FREQUENCY")
FULL_SCHEMA="IVY11_GRRM_V1_FULL_RISK_V2"
FAST_SCHEMA="IVY11_GRRM_V1_FAST_DAILY_V1"
REVISION="V6.3_CACHE_SAFE"
SOURCE_BASENAMES={
    "spy":"SPY_PROSPECTIVE_CAPTURE_",
    "ssga":"SSGA_SPY_DISTRIBUTIONS_",
}
class AuditError(ValueError): pass

def need(test: Any, msg: str):
    if not test: raise AuditError(msg)

def digest(b: bytes)->str: return hashlib.sha256(b).hexdigest()

def canonical(v: Any)->bytes:
    return json.dumps(v,sort_keys=True,separators=(",",":"),ensure_ascii=False,
                      allow_nan=False).encode("utf-8")

def date(s:str)->dt.date:
    try:
        value=dt.date.fromisoformat(str(s))
        need(str(value)==str(s),"Noncanonical date")
        return value
    except (TypeError, ValueError) as e:
        raise AuditError(f"Invalid ISO day: {s!r}") from e

def aware(s: str)->dt.datetime:
    try:
        v=dt.datetime.fromisoformat(s.replace("Z","+00:00"))
        need(v.tzinfo is not None and v.utcoffset() is not None,"No UTC offset")
        return v.astimezone(dt.timezone.utc)
    except (TypeError, ValueError) as e:
        raise AuditError(f"UTC timestamp missing/invalid {s!r}") from e

def dec(s)->Decimal:
    try:
        value=Decimal(str(s).replace(",","").strip())
        need(value.is_finite(),"Not finite")
        return value
    except (InvalidOperation,TypeError) as e:
        raise AuditError(f"Invalid money amount {s!r}") from e

def _xls_text(shared: list[str], c: ET.Element)->str:
    ty=c.attrib.get("t")
    v=c.find("{%s}v"%SHEETS_NS)
    if ty=="inlineStr":
        t=c.find("{%s}is"%SHEETS_NS)
        return "".join((p.text or "") for p in t.iter()
                       if p.tag=="{%s}t"%SHEETS_NS) if t is not None else ""
    val=v.text if v is not None and v.text is not None else ""
    if ty=="s":
        i=int(val)
        need(0<=i<len(shared),"Shared-string offset out of bounds")
        return shared[i]
    return val

def parse_official_spy_xlsx(raw:bytes)->list[dict]:
    """No row number assumptions. Reject duplicated SPY exdates or ambiguity.
    Parse the unchanged XLSX XML; never infer a historic publication vintage.
    """
    need(len(raw)<12_000_000,"Official XLSX unusually large")
    need(zipfile.is_zipfile(io.BytesIO(raw)),"Official distribution XLSX not ZIP")
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        members=z.namelist()
        need(len(members)==len(set(members)),"XLSX duplicate archive paths")
        need("xl/worksheets/sheet1.xml" in members,"Missing distributions worksheet")
        need("xl/workbook.xml" in members,"Missing XLSX workbook")
        need(z.getinfo("xl/worksheets/sheet1.xml").file_size<40_000_000,
             "Oversized worksheet")
        need(z.getinfo("xl/sharedStrings.xml").file_size<30_000_000,
             "Oversized shared strings")
        wb=ET.fromstring(z.read("xl/workbook.xml"))
        sheets=wb.find("{%s}sheets"%SHEETS_NS)
        need(sheets is not None and len(sheets)>=1
             and sheets[0].get("name")=="dividend",
             "Unexpected workbook sheet/position")
        root=ET.fromstring(z.read("xl/sharedStrings.xml"))
        shared=["".join((p.text or "") for p in si.iter()
                        if p.tag=="{%s}t"%SHEETS_NS)
                for si in root.findall("{%s}si"%SHEETS_NS)]
        sh=ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
        rows=sh.findall(".//{%s}sheetData/{%s}row"%(SHEETS_NS,SHEETS_NS))
        need(len(rows)>50,"Official distributions list unexpectedly short")
        headers=None
        out={}
        for r in rows:
            cells={}
            for c in r.findall("{%s}c"%SHEETS_NS):
                loc=c.get("r","")
                m=re.match(r"([A-Z]+)[0-9]+$",loc)
                if m:
                    cells[m.group(1)]=_xls_text(shared,c).strip()
            if headers is None:
                headers=[cells.get(chr(65+k),"") for k in range(len(OFFICIAL_COLUMNS))]
                need(tuple(headers)==OFFICIAL_COLUMNS,"SSGA distribution column headers changed")
                continue
            if cells.get("B","").upper()!="SPY": continue
            raw_date=cells.get("D","")
            try:
                ex=dt.datetime.strptime(raw_date,"%m/%d/%Y").date().isoformat()
            except ValueError as e:
                raise AuditError(f"SPY ex-date invalid: {raw_date!r}") from e
            dividend=dec(cells.get("G","0") or "0")
            short=dec(cells.get("H","0") or "0")
            long=dec(cells.get("I","0") or "0")
            need(dividend>=0 and short>=0 and long>=0,"Negative official distribution")
            need(ex not in out,"Duplicated official SPY ex-date")
            out[ex]={"date":ex, "dividend":str(dividend),
                     "short_term_gain":str(short),"long_term_gain":str(long),
                     "source":"SSGA_official_XLSX"}
        need(len(out)>=100,"Insufficient SPY historical distribution records")
        return sorted(out.values(),key=lambda r:r["date"])

def shadow_inputs_from_zip(zbytes:bytes)->tuple[dict,bytes,dict,dict,dict]:
    """Read the unmodified GitHub Actions stage-4 artifact (zip)."""
    with zipfile.ZipFile(io.BytesIO(zbytes)) as z:
        paths=z.namelist()
        need(len(paths)==len(set(paths)),"Shadow zip contains duplicate names")
        def only(prefix,suffix=None):
            files=[p for p in paths if p.startswith(prefix) and
                   (suffix is None or p.endswith(suffix))]
            need(len(files)==1,f"Expected one file for {prefix!r}: {files}")
            return files[0]
        spy=json.loads(z.read(only("shadow_spy/SPY_PROSPECTIVE_CAPTURE_",".json")))
        official=z.read(only("shadow_ssga/SSGA_SPY_DISTRIBUTIONS_",".xlsx"))
        status=json.loads(z.read("shadow_ssga/SSGA_DIVIDEND_STATUS.json"))
        macro=json.loads(z.read("shadow_macro/DAILY_READINESS_STATUS.json"))
        bridge=json.loads(z.read("shadow_release_bridge/BASE_RELEASE_PREFLIGHT_STATUS.json"))
        return spy,official,status,macro,bridge

def shadow_inputs_from_directory(parent: Path)->tuple[dict,bytes,dict,dict,dict]:
    def only(folder,pat):
        hits=list((parent/folder).glob(pat))
        need(len(hits)==1,f"Require exactly one {folder}/{pat}, got {len(hits)}")
        return hits[0]
    spy=json.loads(only("shadow_spy","SPY_PROSPECTIVE_CAPTURE_*.json").read_text())
    official=only("shadow_ssga","SSGA_SPY_DISTRIBUTIONS_*.xlsx").read_bytes()
    status=json.loads((parent/"shadow_ssga"/"SSGA_DIVIDEND_STATUS.json").read_text())
    macro=json.loads((parent/"shadow_macro"/"DAILY_READINESS_STATUS.json").read_text())
    bridge=json.loads((parent/"shadow_release_bridge"/"BASE_RELEASE_PREFLIGHT_STATUS.json").read_text())
    return spy,official,status,macro,bridge

def verify_spy_capture(obj:dict)->tuple[list[dict],dict]:
    need(set(("manifest","rows"))<=set(obj),"SPY capture schema missing")
    man=obj["manifest"]
    rows=obj["rows"]
    need(man["schema"]=="IVY11_BASE_SPY_PROSPECTIVE_CAPTURE_V1","SPY schema changed")
    need(len(rows)==man["observations"] and len(rows)>=30,"Incomplete SPY capture")
    need(man["rows_sha256"]==digest(canonical({"rows":rows})),"SPY rows hash mismatch")
    target=date(man["target_market_date"])
    capture=aware(man["capture_utc"])
    need(capture.date()>=target,"SPY retrieved before target date")
    seen=set()
    prev=None
    in_target=[]
    excluded=[]
    for r in rows:
        d=date(r["Date"])
        need(d not in seen,"Duplicate SPY session")
        seen.add(d)
        need(prev is None or d>prev,"SPY sessions out of order")
        prev=d
        for k in ("Close","Adj Close","Dividends","Stock Splits"):
            need(k in r and r[k] is not None,f"SPY column {k} missing")
            f=float(r[k])
            need(math.isfinite(f),f"SPY nonfinite {k}")
        need(r["Close"]>0 and r["Adj Close"]>0,"SPY price nonpositive")
        need(r["Dividends"]>=0 and r["Stock Splits"]>=0,"SPY corporate action negative")
        if d>target:excluded.append(r)
        else:in_target.append(r)
    need(in_target and date(in_target[-1]["Date"])==target,
         "SPY missing target-date observation")
    need(man["start_date"]==rows[0]["Date"] and
         man["end_date"]==rows[-1]["Date"],"SPY declared date range mismatch")
    if "requested_end_exclusive" in man:
        need(date(man["requested_end_exclusive"])==target+dt.timedelta(days=1),
             "SPY end-exclusive request incorrect")
    return in_target,{"original_rows":len(rows),"target_rows":len(in_target),
                      "post_target_rows":len(excluded),
                      "date_target":str(target),
                      "capture_utc":capture.isoformat(),
                      "provider_adjusted_close_pit_independently_verified":False}

def compare_official_events(rows:list[dict],official:list[dict])->tuple[list[dict],dict]:
    off={r["date"]:r for r in official}
    yf={r["Date"]:r for r in rows if float(r["Dividends"])>0}
    start,end=rows[0]["Date"],rows[-1]["Date"]
    covered={d:v for d,v in off.items() if start<=d<=end and dec(v["dividend"])>0}
    results=[]
    missing_off=sorted(set(yf)-set(covered))
    missing_provider=sorted(set(covered)-set(yf))
    for day in sorted(set(yf)&set(covered)):
        o=covered[day]
        raw=Decimal(str(yf[day]["Dividends"]))
        expected=dec(o["dividend"])
        # Yahoo's history often rounds to 3 decimals. The tolerance is
        # strictly one half of its last displayed decimal place, plus 1e-6.
        digits=max(0,-raw.as_tuple().exponent)
        tol=Decimal(1).scaleb(-digits)/2+Decimal("0.000001")
        error=abs(raw-expected)
        results.append({"date":day,"yahoo_dividend_usd":str(raw),
                        "ssga_dividend_usd":str(expected),"abs_diff_usd":str(error),
                        "tolerance_usd":str(tol),"match":bool(error<=tol),
                        "capital_gains_in_official_file":
                          (dec(o["short_term_gain"])+dec(o["long_term_gain"]))!=0})
    matches=all(r["match"] for r in results)
    success=bool(results) and not missing_off and not missing_provider and matches
    return results,{
        "status":"PASS" if success else ("INSUFFICIENT_EVENTS" if not (covered or yf) else "FAIL"),
        "official_history_rows":len(official),
        "official_events_in_capture_window":len(covered),
        "provider_events_in_capture_window":len(yf),
        "event_matches":sum(r["match"] for r in results),
        "mismatched_amounts":sum(not r["match"] for r in results),
        "missing_official_ex_dates":missing_off,
        "missing_yahoo_ex_dates":missing_provider,
        "does_not_validate_adjusted_close_historical_vintages":True,
    }

def adjusted_close_audit(rows:list[dict])->dict:
    """Independent *arithmetic* check of Yahoo adjusted prices and reported dividends.
    Still not an independent price source or vintage. Formula uses total-return
    approximation on ex-dates (dividend / previous close).
    """
    max_delta=0
    count=0
    events=0
    for old,new in zip(rows,rows[1:]):
        if float(new["Stock Splits"])>0 or float(old["Stock Splits"])>0:
            continue  # split-adjustment needs separate certified methodology
        total=(float(new["Close"])+float(new["Dividends"]))/float(old["Close"])-1
        adj=float(new["Adj Close"])/float(old["Adj Close"])-1
        diff=abs(adj-total)
        max_delta=max(max_delta,diff)
        count+=1
        if float(new["Dividends"]):events+=1
    # 2 bp max error, covering penny-rounded cash payout and multiplicative adjustments.
    result=bool(count>=20 and events>=1 and max_delta<=0.0002)
    return {"status":"PASS" if result else "FAIL",
            "pairs":count,"ex_div_pairs":events,
            "max_abs_daily_total_return_diff":max_delta,
            "tolerance_return_fraction":0.0002,
            "provider_internal_arithmetic_only":True}

def examine(spy:dict,official_raw:bytes,official_status:dict,
            macro:dict,bridge:dict)->dict:
    effective,spy_stat=verify_spy_capture(spy)
    need(digest(official_raw)==official_status["sha256"],
         "Official SSGA workbook hash mismatch")
    need(official_status["schema"]=="IVY11_BASE_SPY_OFFICIAL_DISTRIBUTION_ARCHIVE_V1",
         "SSGA source schema mismatch")
    need(official_status.get("source_url") ==
         "https://www.ssga.com/library-content/products/fund-data/etfs/us/"
         "spdr-etf-historical-distributions.xlsx", "Unexpected SSGA source URL")
    official=parse_official_spy_xlsx(official_raw)
    results,match=compare_official_events(effective,official)
    adjustment=adjusted_close_audit(effective)
    target=spy_stat["date_target"]
    need(bridge["schema"]=="IVY11_BASE_PUBLIC_RUNTIME_AUDIT_V1","Bridge schema changed")
    need(bridge["market_target"]==target,"FAST/BASE shadow dates differ")
    need(macro["schema"]=="IVY11_BASE_DAILY_EVIDENCE_V1","Macro audit schema changed")
    need(macro["target_market_date"]==target,"Macro target differs from SPY")
    checks=macro.get("steps",[])
    failed=[x["name"] for x in checks if x.get("result")!="PASS"]
    macro_pass=bool(macro.get("source_evidence_ok") and not failed
                    and len(checks)>=10)
    bridge_block=(bridge.get("release")=="NO_GO" and
                  bridge.get("candidate_gate")=="NOT_SUBMITTED")
    need(bridge_block,"Unexpected apparent BASE release in shadow data")
    cutoff=aware(bridge["generated_utc"])
    spy_capture=aware(spy["manifest"]["capture_utc"])
    ssga_capture=aware(official_status["downloaded_utc"])
    # Audit acquired after pre-existing FAST build is NOT retroactive PIT evidence.
    known_before_fast=spy_capture<=cutoff and ssga_capture<=cutoff
    return {
        "schema":SCHEMA,
        "target_market_date":target,
        "base_data_date":bridge["base_date"],
        "base_status":bridge["base_status"],
        "fast_generated_utc":bridge["generated_utc"],
        "source_capture_utc":spy_stat["capture_utc"],
        "spy":spy_stat,
        "official_distribution":{"sha256":digest(official_raw),
                                 "source_file_rows":len(official),
                                 "downloaded_utc":official_status["downloaded_utc"],
                                 "history_vintage_asof_target_proven":False},
        "dividend_reconciliation":match,
        "dividend_matches":results,
        "adjusted_close_consistency":adjustment,
        "macro":{"status":"PASS" if macro_pass else "FAIL",
                 "steps":len(checks),"failures":failed},
        "gates":{
          "SPY_OFFICIAL_DIVIDEND_EVENT":"PASS" if match["status"]=="PASS" else "BLOCKED",
          "SPY_ADJCLOSE_ARITHMETIC":"PASS" if adjustment["status"]=="PASS" else "BLOCKED",
          "SPY_TARGET_BOUNDARY":"PASS" if spy_stat["post_target_rows"]==0 else "BLOCKED",
          "SOURCE_RAW_HASH":"PASS",
          "MACRO_READONLY_EVIDENCE":"PASS" if macro_pass else "BLOCKED",
          "FAST_SOURCE_CUTOFF_ALIGNED":"PASS" if known_before_fast else "BLOCKED",
          "OFFICIAL_SPY_HISTORIC_ADJ_VINTAGE":"BLOCKED",
          "SHILLER_BIS_HISTORIC_VINTAGE_OR_POLICY":"BLOCKED",
          "INDEPENDENT_20_LIVE_SESSIONS":"BLOCKED",
          "REAL_CALCULATED_APPROVED_BASE_CANDIDATE":"BLOCKED",
        },
        "production_release":"NO_GO",
        "market_regime_claim":"NOT_AUTHORIZED_BY_STAGE5",
        "policy":"IVY11 market regime is diagnostic only; IVY4-out-of-11 and gold untouched.",
    }

def save_result(r:dict,out:Path):
    need(not any(part in ("docs","data","src","runtime") for part in out.parts),
         "Do not publish to any production directory")
    out.mkdir(parents=True,exist_ok=True)
    (out/"STAGE5_QUELLEN_UND_RELEASE_GATES.json").write_text(
        json.dumps(r,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    with (out/"SPY_EXDATE_VERGLEICH.csv").open("w",newline="",encoding="utf-8") as f:
        keys=("date","yahoo_dividend_usd","ssga_dividend_usd","abs_diff_usd",
              "tolerance_usd","match","capital_gains_in_official_file")
        w=csv.DictWriter(f,fieldnames=keys)
        w.writeheader();w.writerows(r["dividend_matches"])
    lines=["# IVY11 Stage 5 – echter SPY-Quellenabgleich","",
           f"- Marktstichtag: **{r['target_market_date']}**",
           f"- BASE-Datenstand: **{r['base_data_date']}**",
           f"- FRED/Makroquellen: **{r['macro']['status']}**",
           f"- SPY-Dividende vs. State Street: **{r['dividend_reconciliation']['status']}**",
           f"- SPY-Adjusted-Close-Arithmetik: **{r['adjusted_close_consistency']['status']}**",
           f"- Nach dem Marktstichtag archivierte SPY-Zeilen: **{r['spy']['post_target_rows']}**",
           f"- BASE-Veröffentlichung: **{r['production_release']}**","",
           "## Gates",""]
    lines.extend(f"- {name}: **{status}**" for name,status in r["gates"].items())
    lines+=["","Die Quelle State Street belegt die Dividende, nicht die gesamten "
            "historischen SPY-Adjusted-Close-Versionen.",
            "Ein grüner GitHub-Workflow ersetzt keine Modellfreigabe. "
            "Es wird weder in docs/ noch in data/ geschrieben.",""]
    (out/"STAGE5_STATUS.md").write_text("\n".join(lines),encoding="utf-8")

def fail_report(ex:Exception,out:Path):
    out.mkdir(parents=True,exist_ok=True)
    r={"schema":SCHEMA,"production_release":"NO_GO",
       "fatal_error_class":type(ex).__name__,"fatal_error":str(ex)[:250],
       "note":"The audit blocked. No market-regime publication attempted."}
    (out/"STAGE5_FEHLER.json").write_text(json.dumps(r,indent=2,ensure_ascii=False))
    return r

REGIMES={
  1:"Kapitulation / Bodenbildung",2:"Frühe Erholung",
  3:"Gesunder Bullenmarkt",4:"Reifer Bullenmarkt",
  5:"Überhitzung / Fragilität",6:"Stress / beginnender Bärenmarkt",
  7:"Systemischer Crash",
}
FAST_FIELDS=("MSP_MARKET","MSP_CREDIT_CONTROL","MSP_VOL","CLEAN3F_VOTE_PCT",
             "FAST_SENTINEL_65_70","SENTINEL_70_75","EQ_MA200_GAP_PCT",
             "EQ_DRAWDOWN_PCT","EQ_RET_5D_PCT","EQ_RET_20D_PCT")
def audit_gpt_runtime_snapshot(full:dict)->dict:
    """Validate FULL shape & computational consistency. An intrinsically
    consistent 'CURRENT' flag never proves a real approved BASE pipeline."""
    need(full.get("schema")==FULL_SCHEMA,"FULL schema mismatch")
    need(full.get("runtime_revision")==REVISION,"Runtime revision missing/incorrect")
    need(full.get("authority")=="DIAGNOSTIC_ONLY / NO_PORTFOLIO_ACTION",
         "Portfolio action not permitted")
    target=date(full.get("target_market_date"))
    created=aware(full["generated_utc"])
    need(created.date()>=target,"FULL generated before target date")
    bp=full["base_policy"]
    base=date(bp["base_data_date"])
    need(base<=target,"BASE date after target date")
    age=(target-base).days
    need(age==bp["base_staleness_calendar_days"],"BASE staleness inconsistent")
    bs=bp["status"]
    if bs=="CURRENT":need(base==target,"CURRENT uses wrong BASE date")
    elif bs=="VALID_STALE_WITHIN_CALM_POLICY":
        need(base<target and age<=7,"Invalid calm-mode BASE staleness")
    elif bs=="BASE_REFRESH_REQUIRED":
        need(bp["base_valid_for_management"] is False,
             "Stale BASE cannot be management-valid")
    else:
        raise AuditError("Unknown BASE policy status")
    baseobj=full["base"]
    regime=int(baseobj["REGIME"])
    need(1<=regime<=7 and str(baseobj["REGIME_NAME"])==REGIMES[regime],
         "BASE regime or translated regime name mismatch")
    vp=float(baseobj["VP"]);msp=float(baseobj["MSP"])
    need(all(math.isfinite(v) and 0<=v<=100 for v in (vp,msp)),
         "VP/MSP outside scale")
    fast=full["fast_action"]
    need(all(k in fast for k in FAST_FIELDS),"FULL risk indicators incomplete")
    market=float(fast["MSP_MARKET"]);credit=float(fast["MSP_CREDIT_CONTROL"])
    vol=float(fast["MSP_VOL"])
    need(all(math.isfinite(v) and 0<=v<=100 for v in (market,credit,vol)),
         "Risk input outside valid score range")
    pr=full["risk_pressure"]
    expected=0.45*vp+0.25*market+0.2*credit+0.1*vol
    score=float(pr["score"])
    need(math.isfinite(score) and abs(score-expected)<1e-7,
         "Risk Pressure does not match frozen diagnostic formula")
    need(pr.get("action_authority")=="NONE","Risk Pressure has trading authority")
    need(str(full.get("assets",{}).get("boom_bust_image_url","")).startswith(
         "https://raw.githubusercontent.com/"),"Missing official image reference")
    need(pr["classification"]==("NIEDRIG" if score<=25 else
         "NORMAL" if score<=45 else "ERHÖHT" if score<=60 else
         "HOCH" if score<=75 else "SEHR HOCH" if score<=90 else
         "AKUTER SYSTEMISCHER STRESS"),"Risk Pressure classification mismatch")
    previous=pr.get("month_end_reference")
    delta=pr.get("delta_vs_month_end")
    if previous is not None and delta is not None:
        need(abs((score-float(previous["score"]))-float(delta))<1e-7,
             "Risk Pressure reference delta inconsistent")
    # We have no external certified release manifest yet. This is deliberately
    # a stricter UI determination than a bare base_policy.status claim.
    actionable=False
    if bs=="BASE_REFRESH_REQUIRED": reason="BASE_REFRESH_REQUIRED"
    elif bs=="CURRENT": reason="CURRENT_CLAIM_REQUIRES_EXTERNAL_PRODUCTION_PROVENANCE"
    else: reason="CALM_POLICY_REQUIRES_EXTERNAL_PRODUCTION_PROVENANCE"
    return {
      "schema":"IVY11_GPT_RUNTIME_DATA_CONTRACT_AUDIT_V1",
      "gpt_version":"V8.1",
      "runtime_methodology":"V6.3_CACHE_SAFE",
      "target_market_date":str(target),
      "generated_utc":created.isoformat(),
      "base_data_date":str(base),
      "base_age_calendar_days":age,
      "reported_base_status":bs,
      "reference_regime_number":regime,
      "reference_regime_name":REGIMES[regime],
      "risk_pressure_score":score,
      "risk_pressure_classification":pr["classification"],
      "shape_and_arithmetic_valid":True,
      "base_regime_current_certified":False,
      "reason_not_released":reason,
      "portfolio_action_authority":"NONE",
      "gpt_button_must_label_base_as_current":False,
      "explanation":"Data can be displayed as diagnostic with explicit date and stale warnings; no current BASE claim.",
    }

def run_runtime_file(inp:Path,out:Path)->dict:
    try:
        full=json.loads(inp.read_text(encoding="utf-8"))
        result=audit_gpt_runtime_snapshot(full)
        out.mkdir(parents=True,exist_ok=True)
        (out/"GPT_RUNTIME_DATENVERTRAG.json").write_text(
           json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        return result
    except Exception as exc:
        fail_report(exc,out)
        raise

def main(argv=None)->int:
    p=argparse.ArgumentParser()
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--shadow-zip",type=Path)
    mode.add_argument("--shadow-dir",type=Path)
    mode.add_argument("--runtime-json",type=Path)
    p.add_argument("--out",type=Path,default=Path("stage5_results"))
    args=p.parse_args(argv)
    try:
        if args.runtime_json:
            result=run_runtime_file(args.runtime_json,args.out)
            print(json.dumps({"gpt_snapshot_contract":"PASS",
                "base_current_certified":result["base_regime_current_certified"],
                "target":result["target_market_date"]}))
            return 0
        inputs=(shadow_inputs_from_zip(args.shadow_zip.read_bytes())
                if args.shadow_zip else shadow_inputs_from_directory(args.shadow_dir))
        result=examine(*inputs)
        save_result(result,args.out)
        print(json.dumps({"production_release":"NO_GO","gates":result["gates"],
                          "official_spy_match":result["dividend_reconciliation"]["status"]}))
        return 0
    except Exception as e:
        print("Stage5 input failure:",type(e).__name__,str(e)[:250],file=sys.stderr)
        print(json.dumps(fail_report(e,args.out)),file=sys.stderr)
        return 1

if __name__=="__main__":
    raise SystemExit(main())
