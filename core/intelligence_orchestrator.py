"""Multi-model cloud reasoning for Brahma Evo."""
from __future__ import annotations
import json, logging, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional
from core.user_paths import get_user_data_dir
from or_client import client as cloud_client

log=logging.getLogger("BrahmaIntelligence")
ROOT=Path(__file__).resolve().parent.parent
REPO_CFG=ROOT/"config"/"intelligence.json"
USER_CFG=get_user_data_dir()/"config"/"intelligence.json"
DEFAULTS={
    "enabled": True, "default_profile":"smart", "simple_profile":"fast",
    "parallel_workers":4, "max_specialists":2, "max_context_chars":14000, "simple_max_chars":220,
    "simple_keywords":("hello","hi","hey","thanks","thank you","what time","what day"),
    "profiles":{
        "fast":{"model":"auto/fast","temperature":0.35,"max_tokens":2048,"specialists":0},
        "smart":{"model":"auto/smart","temperature":0.35,"max_tokens":4096,"specialists":2,"synthesis_model":"auto/smart"},
        "coding":{"model":"auto/coding","temperature":0.2,"max_tokens":8192,"specialists":2,"synthesis_model":"auto/coding"},
        "vision":{"model":"auto/vision","temperature":0.2,"max_tokens":2048,"specialists":2,"synthesis_model":"auto/smart"},
        "maintenance":{"model":"auto/smart","temperature":0.2,"max_tokens":4096,"specialists":2,"synthesis_model":"auto/smart"},
    },
}
CODING=("code","coding","program","python","javascript","typescript","bug","debug","implement","refactor","repository","github","function","class","api","script","build","compile","test")
MAINT=("windows","pc","computer","cpu","ram","memory","disk","driver","process","crash","startup","repair","performance","fix my computer")
VISION=("image","picture","photo","screenshot","screen","camera","visual")

def merge(a:dict,b:dict)->dict:
    out=dict(a)
    for k,v in b.items(): out[k]=merge(out[k],v) if isinstance(v,dict) and isinstance(out.get(k),dict) else v
    return out

def load_config()->dict:
    cfg=dict(DEFAULTS)
    for p in (REPO_CFG,USER_CFG):
        try:
            if p.is_file():
                d=json.loads(p.read_text(encoding="utf-8"))
                if isinstance(d,dict): cfg=merge(cfg,d)
        except Exception as e: log.warning("intelligence config load failed: %s",e)
    return cfg

def allowed()->bool:
    p=get_user_data_dir()/"config"/"app_settings.json"
    try:
        d=json.loads(p.read_text(encoding="utf-8"))
        return not bool(d.get("offline_mode_enabled",False)) and d.get("intelligence_mode")!="off" and bool(d.get("intelligence_orchestration_enabled",True))
    except Exception:return True

def profile_for(prompt:str,requested:Optional[str],cfg:dict)->str:
    if requested:return str(requested).lower().strip()
    t=prompt.casefold()
    if any(x in t for x in CODING):return "coding"
    if any(x in t for x in MAINT):return "maintenance"
    if any(x in t for x in VISION):return "vision"
    sw=tuple(str(x).casefold() for x in cfg.get("simple_keywords",()))
    if len(t.strip())<=int(cfg.get("simple_max_chars",220)) and any(t.strip().startswith(x) for x in sw):return str(cfg.get("simple_profile","fast"))
    return str(cfg.get("default_profile","smart"))

def trim(s:str,n:int)->str:
    s=str(s or "")
    return s if len(s)<=n else s[:n]+"\n[truncated]"

class IntelligenceOrchestrator:
    def _cfg(self,p:str,c:dict)->dict:
        return dict(c.get("profiles",{}).get(p) or c["profiles"]["smart"])
    def _roles(self,p:str)->tuple[str,...]:
        if p=="coding":return ("senior implementation engineer","independent code reviewer")
        if p=="maintenance":return ("systems diagnostician","independent verifier")
        if p=="vision":return ("visual analyst","independent visual verifier")
        return ("primary reasoner","critical reasoner")
    def _call(self,prompt:str,system:str,model:str,max_tokens:int,temp:float,history=None)->str:
        return cloud_client.chat(prompt=prompt,system=system,history=history,model=model,max_tokens=max_tokens,temperature=temp)
    def respond(self,prompt:str,*,system="You are Brahma Evo, a precise and helpful assistant.",history=None,context="",profile=None)->str:
        prompt=(prompt or "").strip()
        if not prompt:raise ValueError("A prompt is required.")
        c=load_config()
        if not bool(c.get("enabled",True)) or not allowed():
            return self._call(prompt,system,"auto",4096,0.5,history)
        p=profile_for(prompt,profile,c); pc=self._cfg(p,c); count=int(pc.get("specialists",0))
        if count<=0:return self._call(prompt,system,str(pc.get("model","auto/fast")),int(pc.get("max_tokens",2048)),float(pc.get("temperature",0.35)),history)
        count=min(count,int(c.get("max_specialists",2)),max(1,int(c.get("parallel_workers",4))))
        roles=self._roles(p)[:count]; ctx=trim(context,int(c.get("max_context_chars",14000))); results=[]
        def task(i,role):
            sp=f"You are specialist {i} of {count} for Brahma Evo. Role: {role}. Analyze the request independently, identify intent, constraints, ambiguity, evidence, and the best answer/action. Do not assume another specialist is correct.\n\nRequest:\n{prompt}\n\nContext:\n{ctx or '[none]'}"
            return i,self._call(sp,system,str(pc.get("model","auto/smart")),int(pc.get("max_tokens",4096)),float(pc.get("temperature",0.35)),history)
        started=time.perf_counter()
        with ThreadPoolExecutor(max_workers=count) as ex:
            fs=[ex.submit(task,i,r) for i,r in enumerate(roles,1)]
            for f in as_completed(fs):
                try:results.append(f.result())
                except Exception as e:log.warning("specialist failed: %s",e)
        results.sort(key=lambda x:x[0]); usable=[x[1].strip() for x in results if str(x[1]).strip()]
        if not usable:return self._call(prompt,system,str(pc.get("model","auto/smart")),int(pc.get("max_tokens",4096)),float(pc.get("temperature",0.35)),history)
        evidence="\n\n".join(f"=== Specialist {i} ===\n{trim(v,9000)}" for i,v in results if str(v).strip())
        synth_system=system+"\n\nYou are Brahma Evo's final synthesis layer. Reconcile the independent analyses, resolve contradictions, preserve important constraints, and never invent missing evidence. Return only the final user-facing answer."
        synth_prompt=f"Original request:\n{prompt}\n\nContext:\n{ctx or '[none]'}\n\nIndependent analyses:\n{evidence}"
        try:
            out=self._call(synth_prompt,synth_system,str(pc.get("synthesis_model","auto/smart")),int(pc.get("max_tokens",4096)),0.25)
            log.info("profile=%s specialists=%d total_ms=%.0f",p,len(usable),(time.perf_counter()-started)*1000)
            return out
        except Exception as e:
            log.warning("synthesis failed: %s",e); return usable[0]
    def respond_json(self,prompt:str,*,system="Return ONLY valid JSON.",profile="smart",max_tokens=8192)->dict[str,Any]:
        c=load_config()
        if not bool(c.get("enabled",True)) or not allowed():return cloud_client.chat_json(prompt,system=system,model="auto",max_tokens=max_tokens)
        pc=self._cfg(profile,c); count=min(max(1,int(pc.get("specialists",1))),int(c.get("max_specialists",2)),max(1,int(c.get("parallel_workers",4))))
        model=str(pc.get("model","auto/smart")); roles=self._roles(profile)[:count]; drafts=[]
        def task(i,role):return i,cloud_client.chat(prompt=f"{prompt}\n\nRole: {role}\nReturn ONLY valid JSON.",system=system+" Return one valid JSON object.",model=model,max_tokens=max_tokens,temperature=float(pc.get("temperature",0.2)))
        with ThreadPoolExecutor(max_workers=count) as ex:
            fs=[ex.submit(task,i,r) for i,r in enumerate(roles,1)]
            for f in as_completed(fs):
                try:drafts.append(f.result())
                except Exception as e:log.warning("JSON specialist failed: %s",e)
        if not drafts:return cloud_client.chat_json(prompt,system=system,model=model,max_tokens=max_tokens)
        drafts.sort(key=lambda x:x[0]); evidence="\n\n".join(f"=== Draft {i} ===\n{trim(v,14000)}" for i,v in drafts)
        raw=cloud_client.chat(prompt=f"Task:\n{prompt}\n\nIndependent drafts:\n{evidence}",system=system+" Reconcile the drafts and return ONLY one valid JSON object.",model=str(pc.get("synthesis_model","auto/smart")),max_tokens=max_tokens,temperature=0.15)
        clean=raw.strip()
        if clean.startswith("```"):
            parts=clean.split("```"); clean=parts[1] if len(parts)>1 else clean
            if clean.lstrip().startswith("json"):clean=clean.lstrip()[4:]
        try:return json.loads(clean.strip().strip("`"))
        except json.JSONDecodeError:return cloud_client.chat_json(prompt,system=system,model=model,max_tokens=max_tokens)

orchestrator=IntelligenceOrchestrator()
