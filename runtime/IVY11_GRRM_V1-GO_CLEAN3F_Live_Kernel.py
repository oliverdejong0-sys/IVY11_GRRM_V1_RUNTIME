
"""
IVY11_GRRM_V1-GO — Frozen CLEAN3F Live Kernel

Purpose:
- exact incremental continuation of the frozen 243-model V3 CLEAN3F ensemble;
- source-independent: consumes already-normalized MSP_MARKET, MSP_CREDIT_CONTROL, MSP_VOL;
- no NFCI, no OAS archive, no slow-VP dependence in the action signal;
- today's observation determines only the next trading day's shadow budget.

This module contains no data-download logic and no live-capital authority.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from collections import deque
import itertools, json, math
from pathlib import Path
import numpy as np
import pandas as pd

PARAM_GRID=[
    (float(ms),float(sy),float(cf),float(fall),float(r2x))
    for ms,sy,cf,fall,r2x in itertools.product(
        [60,65,70],[80,85,90],[65,70,75],[-1,-3,-5],[40,45,50]
    )
]

@dataclass
class ParamState:
    prev_regime: int = 3
    fallrun: int = 0

def clean_msp(market,credit,vol):
    vals=[float(x) for x in [market,credit,vol] if pd.notna(x)]
    return float(np.mean(vals)) if vals else np.nan

def classify_one(msp, vp, d20, market, credit, vol, param, state):
    ms,sy,cf,fall,r2x=param
    if pd.isna(msp):
        return state.prev_regime, state

    fam=0
    other=0
    if pd.notna(market) and market>=cf: fam+=1
    if pd.notna(credit) and credit>=cf: fam+=1; other+=1
    if pd.notna(vol) and vol>=cf: fam+=1; other+=1

    systemic=(msp>=sy and fam>=3)
    r6=(msp>=ms and pd.notna(market) and market>=cf and other>=1)

    falling=(pd.notna(d20) and d20<=fall)
    fallrun=state.fallrun+1 if falling else 0
    prev=state.prev_regime

    if prev==7:
        reg=1 if fallrun>=3 else 7
    elif prev==1:
        if systemic and (pd.isna(d20) or d20>=3): reg=7
        elif r6 and (pd.isna(d20) or d20>=3): reg=6
        elif msp<=65 and (pd.isna(market) or market<=70): reg=2
        else: reg=1
    elif prev==2:
        if systemic: reg=7
        elif r6 and (pd.isna(d20) or d20>=3): reg=6
        elif msp<=r2x and (pd.isna(market) or market<=55):
            if pd.notna(vp) and vp>=70: reg=5
            elif pd.notna(vp) and vp>=55: reg=4
            else: reg=3
        else: reg=2
    else:
        if systemic: reg=7
        elif r6: reg=6
        elif prev==6 and msp<ms and (pd.isna(market) or market<cf) and (pd.isna(d20) or d20<0):
            reg=2
        elif pd.notna(vp) and vp>=70 and msp<ms: reg=5
        elif (pd.notna(vp) and vp>=55) or (45<=msp<ms): reg=4
        else: reg=3

    return int(reg), ParamState(int(reg),int(fallrun))

class Clean3FLiveKernel:
    def __init__(self, states=None, last_msp=None, vp_constant=50.0):
        self.states=states or [ParamState() for _ in PARAM_GRID]
        if len(self.states)!=len(PARAM_GRID):
            raise ValueError("Need one state per frozen parameter combination.")
        self.last_msp=deque(maxlen=20)
        if last_msp:
            for x in last_msp[-20:]:
                self.last_msp.append(float(x))
        self.vp_constant=float(vp_constant)

    def update(self, market, credit, vol):
        msp=clean_msp(market,credit,vol)
        d20=np.nan
        if len(self.last_msp)==20 and pd.notna(msp):
            d20=float(msp-self.last_msp[0])

        regs=[]
        newstates=[]
        for param,state in zip(PARAM_GRID,self.states):
            reg,ns=classify_one(
                msp,self.vp_constant,d20,
                float(market) if pd.notna(market) else np.nan,
                float(credit) if pd.notna(credit) else np.nan,
                float(vol) if pd.notna(vol) else np.nan,
                param,state
            )
            regs.append(reg)
            newstates.append(ns)

        self.states=newstates
        if pd.notna(msp):
            self.last_msp.append(float(msp))

        stress=np.isin(np.asarray(regs),[6,7])
        vote=float(stress.mean())
        return {
            "CLEAN3F_MSP":msp,
            "CLEAN3F_MSP_D20":d20,
            "CLEAN3F_VOTE":vote,
            "CLEAN3F_STRESS":bool(vote>=0.25),
            "R6_R7_COUNT":int(stress.sum()),
        }

    def to_seed_dict(self, anchor_date=None):
        return {
            "kernel":"IVY11_GRRM_V1-GO",
            "anchor_date":anchor_date,
            "vp_constant":self.vp_constant,
            "last_msp":list(self.last_msp),
            "param_states":[asdict(s) for s in self.states],
        }

    @classmethod
    def from_seed_dict(cls,d):
        return cls(
            states=[ParamState(**x) for x in d["param_states"]],
            last_msp=d.get("last_msp",[]),
            vp_constant=d.get("vp_constant",50.0)
        )

    @classmethod
    def from_seed_file(cls,path):
        return cls.from_seed_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def save_seed(self,path,anchor_date=None):
        Path(path).write_text(
            json.dumps(self.to_seed_dict(anchor_date),indent=2),
            encoding="utf-8"
        )

def build_seed_from_score_history(scores, anchor_date):
    x=scores.loc[:pd.Timestamp(anchor_date)].copy()
    kernel=Clean3FLiveKernel()
    # start from empty and replay frozen score history
    for _,r in x.iterrows():
        kernel.update(r["MSP_MARKET"],r["MSP_CREDIT_CONTROL"],r["MSP_VOL"])
    return kernel
