"""Multi-model cloud reasoning for Brahma Evo."""
from __future__ import annotations
import json, logging, time, threading
import os
import requests
from config import get_config
from core.omniroute import gateway
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional
from core.user_paths import get_user_data_dir
from core.runtime_paths import APP_SETTINGS_PATH
from or_client import client as cloud_client

log=logging.getLogger("BrahmaIntelligence")
ROOT=Path(__file__).resolve().parent.parent
REPO_CFG=ROOT/"config"/"intelligence.json"
USER_CFG=get_user_data_dir()/"config"/"intelligence.json"
_CFG_LOCK = threading.RLock()
_CFG_CACHE = None
_RUNTIME_CACHE = None

DEFAULTS={
    "enabled": True, "default_profile":"smart", "simple_profile":"fast",
    "parallel_workers":4, "max_specialists":2, "ensemble_enabled":True, "ensemble_max_providers":8, "ensemble_model_cache_seconds":300, "max_context_chars":14000, "simple_max_chars":220,
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

def _mtime_ns(path: Path):
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _runtime_settings() -> dict:
    global _RUNTIME_CACHE
    mtime = _mtime_ns(APP_SETTINGS_PATH)
    with _CFG_LOCK:
        if _RUNTIME_CACHE is not None and _RUNTIME_CACHE[0] == mtime:
            return dict(_RUNTIME_CACHE[1])
        try:
            from memory.config_manager import load_settings
            data = load_settings()
        except Exception:
            data = {}
        result = dict(data) if isinstance(data, dict) else {}
        _RUNTIME_CACHE = (mtime, result)
        return dict(result)


def load_config()->dict:
    global _CFG_CACHE
    mtimes = (_mtime_ns(REPO_CFG), _mtime_ns(USER_CFG))
    with _CFG_LOCK:
        if _CFG_CACHE is not None and _CFG_CACHE[0] == mtimes:
            return dict(_CFG_CACHE[1])

        cfg=dict(DEFAULTS)
        for p in (REPO_CFG,USER_CFG):
            try:
                if p.is_file():
                    d=json.loads(p.read_text(encoding="utf-8"))
                    if isinstance(d,dict): cfg=merge(cfg,d)
            except Exception as e: log.warning("intelligence config load failed: %s",e)
        _CFG_CACHE = (mtimes, cfg)
        return dict(cfg)

def allowed()->bool:
    d = _runtime_settings()
    return (
        not bool(d.get("offline_mode_enabled", False))
        and d.get("intelligence_mode") != "off"
        and bool(d.get("intelligence_orchestration_enabled", True))
    )


def _runtime_intelligence_mode() -> str:
    data = _runtime_settings()
    return str(data.get("intelligence_mode", "smart") or "smart").strip().lower()


def profile_for(prompt:str,requested:Optional[str],cfg:dict)->str:
    if requested:
        return str(requested).lower().strip()
    t=prompt.casefold()
    mode = _runtime_intelligence_mode()
    if mode == "fast":
        return "fast"
    if any(x in t for x in CODING):
        return "coding"
    if any(x in t for x in MAINT):
        return "maintenance"
    # Vision remains an explicit profile until the request carries actual image data.
    sw=tuple(str(x).casefold() for x in cfg.get("simple_keywords",()))
    if len(t.strip())<=int(cfg.get("simple_max_chars",220)) and any(t.strip().startswith(x) for x in sw):
        return str(cfg.get("simple_profile","fast"))
    return str(cfg.get("default_profile","smart"))

def trim(s:str,n:int)->str:
    s=str(s or "")
    return s if len(s)<=n else s[:n]+"\n[truncated]"


_MODEL_CACHE = None
_PROVIDER_KEYS = {
    "openai":"openai_api_key", "anthropic":"anthropic_api_key", "gemini":"gemini_api_key",
    "openrouter":"openrouter_api_key", "groq":"groq_api_key", "xai":"xai_api_key",
    "cerebras":"cerebras_api_key", "deepseek":"deepseek_api_key",
    "mistral":"mistral_api_key", "cohere":"cohere_api_key",
}
_PROVIDER_PREFIXES = {
    "openai":("openai","oai"), "anthropic":("anthropic","claude","cc"),
    "gemini":("gemini","google"), "openrouter":("openrouter",), "groq":("groq",),
    "xai":("xai",), "cerebras":("cerebras",), "deepseek":("deepseek",),
    "mistral":("mistral",), "cohere":("cohere",)
}
_QUALITY_HINTS = ("opus","sonnet","reasoning","thinking","pro","ultra","max","flagship","large","gpt-6-astra","gpt-5","gpt-4","gemini-3","gemini-2","o3","o4","o1","r1","v5","v4","v3")

def _configured_providers()->tuple[str,...]:
    data=get_config(); return tuple(p for p,k in _PROVIDER_KEYS.items() if str(data.get(k) or '').strip())

def _catalog_models()->tuple[str,...]:
    global _MODEL_CACHE
    providers=_configured_providers(); now=time.monotonic(); ttl=max(30,int(load_config().get('ensemble_model_cache_seconds',300)))
    if _MODEL_CACHE and now-_MODEL_CACHE[0] < ttl and _MODEL_CACHE[1] == providers: return _MODEL_CACHE[2]
    models=[]
    try:
        gw=gateway()
        if gw.ensure_ready():
            headers={'Accept':'application/json'}
            key=os.environ.get('BRAHMA_OMNIROUTE_API_KEY','').strip()
            if key: headers['Authorization']='Bearer '+key
            resp=requests.get(gw.base_url+'/models',params={'prefix':'alias'},headers=headers,timeout=5)
            if resp.ok:
                rows=resp.json().get('data',[])
                models=[str(row.get('id')).strip() for row in rows if isinstance(row,dict) and str(row.get('id') or '').strip()]
    except Exception as exc: log.debug('ensemble model catalog unavailable: %s',exc)
    _MODEL_CACHE=(now,providers,tuple(dict.fromkeys(models)))
    return _MODEL_CACHE[2]

def _select_provider_model(provider:str, models:tuple[str,...], overrides:dict, profile:str="smart")->str|None:
    override=str(overrides.get(provider) or '').strip()
    if override: return override if '/' in override else provider+'/'+override
    prefixes={x.casefold() for x in _PROVIDER_PREFIXES.get(provider,(provider,))}
    candidates=[m for m in models if m.split('/',1)[0].casefold() in prefixes]
    if not candidates: return None
    def score(m):
        low=m.casefold()
        base=sum(2 for h in _QUALITY_HINTS if h in low)
        try:
            from core.model_performance import routing_bonus
            learned=routing_bonus(provider=provider, model=m, profile=profile)
        except Exception:
            learned=0.0
        return (base + learned, len(m))
    return max(candidates,key=score)

def _ensemble_models(cfg:dict, profile_cfg:dict, profile:str="smart")->list[tuple[str,str]]:
    if not bool(cfg.get('ensemble_enabled',True)) or not bool(profile_cfg.get('ensemble',True)): return []
    providers=_configured_providers()
    if len(providers)<2: return []
    overrides=dict(cfg.get('ensemble_models') or {}) if isinstance(cfg.get('ensemble_models'),dict) else {}
    if isinstance(profile_cfg.get('ensemble_models'),dict): overrides.update(profile_cfg['ensemble_models'])
    models=_catalog_models(); selected=[]
    for provider in providers:
        model=_select_provider_model(provider,models,overrides,profile)
        if model: selected.append((provider,model))
    return selected[:max(2,int(cfg.get('ensemble_max_providers',8)))]

def _ensemble_roles(profile:str,count:int)->tuple[str,...]:
    roles={
        'coding':('implementation architect','adversarial code reviewer','test strategist','edge-case debugger'),
        'maintenance':('systems diagnostician','failure-mode analyst','independent verifier','performance specialist'),
        'vision':('visual analyst','detail checker','context analyst','independent verifier'),
    }.get(profile,('independent reasoner','skeptical reviewer','alternative-solution analyst','constraint checker'))
    return tuple(roles[i%len(roles)] for i in range(count))

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
        panel=_ensemble_models(c,pc,p) if p != "fast" else []
        if panel:
            ctx=trim(context,int(c.get("max_context_chars",14000)))
            roles=_ensemble_roles(p,len(panel))
            futures={}
            ensemble_workers = max(
                1,
                min(
                    len(panel),
                    int(c.get("parallel_workers", 4)),
                ),
            )
                expert_system=(
                    system+
                    "\n\nYou are one independent member of Brahma Evo's cross-provider reasoning panel. "
                    "Solve the original request yourself. Do not assume another model is correct or defer "
                    "to provider reputation. State important assumptions, evidence, and uncertainty. "
                    f"Specialist focus: {role}."
                )
                expert_prompt=(
                    f"Original request:\n{prompt}\n\nContext:\n{ctx or '[none]'}\n\n"
                    "Produce your best complete analysis/answer independently. Do not discuss the panel."
                )
                futures[(index,provider,model)] = (
                    expert_prompt,
                    expert_system,
                    model,
                    int(pc.get("max_tokens",4096)),
                    float(pc.get("temperature",0.35)),
                    history,
                )
            panel_results=[]
            with ThreadPoolExecutor(
                max_workers=ensemble_workers,
                thread_name_prefix="BrahmaEnsemble",
            ) as executor:
                submitted = {
                    executor.submit(self._call, *spec): meta
                    for meta, spec in futures.items()
                }
                for future in as_completed(submitted):
                    index,provider,model=submitted[future]
                    try:
                    answer=str(future.result() or "").strip()
                    if answer:
                        panel_results.append((index,provider,model,answer))
                except Exception as exc:
                    log.warning("ensemble provider %s failed: %s",provider,exc)
            panel_results.sort(key=lambda row:row[0])
            if panel_results:
                evidence="\\n\\n".join(
                    f"=== Source {i} ===\\n{trim(answer,9000)}"
                    for i,_provider,_model,answer in panel_results
                )
                judge_model=str(pc.get("synthesis_model","auto/smart"))
                synth_system=(
                    system+
                    "\n\nYou are Brahma Evo's independent consensus judge. The sources are anonymized. "
                    "Judge substance rather than model brand. Compare agreements and contradictions, "
                    "identify unique useful insights and missing evidence, and resolve conflicts using "
                    "logic and evidence rather than majority vote. Return one authoritative answer to "
                    "the original request."
                )
                synth_prompt=(
                    f"Original request:\n{prompt}\n\nContext:\n{ctx or '[none]'}\n\n"
                    f"Independent panel analyses:\n{evidence}"
                )
                # Cross-examination happens only for the expensive, non-fast path.
                # Critics see anonymized evidence and target contradictions rather than
                # blindly generating another duplicate answer.
                critique_prompt = (
                    f"Original request:\n{prompt}\n\nContext:\n{ctx or '[none]'}\n\n"
                    f"Candidate analyses:\n{evidence}\n\n"
                    "Act as an adversarial cross-examiner. Identify concrete contradictions, "
                    "unsupported claims, missing constraints, and the single highest-value "
                    "correction or additional verification the final answer needs. Do not "
                    "rewrite the whole answer."
                )
                critique_system = (
                    system +
                    "\n\nYou are Brahma Evo's adversarial cross-examination layer. "
                    "Be skeptical, evidence-driven, and concise. Focus on falsifiable issues."
                )
                critique_models = []
                try:
                    critique_models.append(
                        _select_provider_model(
                            "openai", _catalog_models(), {},
                        ) or judge_model
                    )
                except Exception:
                    critique_models.append(judge_model)
                try:
                    critique_models.append(
                        _select_provider_model(
                            "anthropic", _catalog_models(), {},
                        ) or judge_model
                    )
                except Exception:
                    critique_models.append(judge_model)
                critiques = []
                critic_specs = list(enumerate(tuple(dict.fromkeys(critique_models))[:2], 1))
                critic_specs = list(enumerate(tuple(dict.fromkeys(critique_models))[:2], 1))
                critic_workers = max(1, min(len(critic_specs), int(c.get("parallel_workers", 4))))
                with ThreadPoolExecutor(
                    max_workers=critic_workers,
                    thread_name_prefix="BrahmaEnsembleCritic",
                ) as executor:
                    critic_futures = {
                        executor.submit(
                            self._call,
                            critique_prompt,
                            critique_system + f"\nCritic slot: {critique_index}.",
                            critique_model,
                            max(1024, int(pc.get("max_tokens",4096)) // 2),
                            0.1,
                            None,
                        ): critique_index
                        for critique_index, critique_model in critic_specs
                    }
                    for future in as_completed(critic_futures):
                        try:
                            critique = str(future.result() or "").strip()
                            if critique:
                                critiques.append((critic_futures[future], trim(critique, 7000)))
                        except Exception as exc:
                            log.debug("ensemble critic failed: %s", exc)
                critiques.sort(key=lambda item: item[0])
                critiques = [value for _index, value in critiques]

                critique_evidence = (
                    "\n\n".join(
                        f"=== Cross-examination {i} ===\n{value}"
                        for i, value in enumerate(critiques, 1)
                    )
                    or "[no additional critique available]"
                )
                final_prompt = (
                    f"Original request:\n{prompt}\n\nContext:\n{ctx or '[none]'}\n\n"
                    f"Independent panel analyses:\n{evidence}\n\n"
                    f"Cross-examination:\n{critique_evidence}\n\n"
                    "Synthesize one answer. Resolve contradictions instead of majority-voting. "
                    "Apply useful corrections from the cross-examiners. Do not mention the internal panel."
                )
                try:
                    result=self._call(
                        final_prompt,
                        synth_system,
                        judge_model,
                        int(pc.get("max_tokens",4096)),
                        0.15,
                        None,
                    )
                    if result and result.strip():
                        log.info(
                            "cross-provider debate profile=%s panel=%d providers=%s critics=%d",
                            p,len(panel_results),",".join(row[1] for row in panel_results),len(critiques)
                        )
                        return result.strip()
                except Exception as exc:
                    log.warning("ensemble synthesis failed: %s",exc)
                # Graceful degradation: one surviving expert is still better than a failed request.
                return panel_results[0][3]
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
        pc=self._cfg(profile,c)
        panel=_ensemble_models(c,pc,profile) if profile != "fast" else []
        if panel:
            futures={}
            roles=_ensemble_roles(profile,len(panel))
            for index,((provider,model),role) in enumerate(zip(panel,roles),1):
                futures[_ENSEMBLE_EXECUTOR.submit(
                    cloud_client.chat,
                    f"{prompt}\n\nSpecialist role: {role}.\nReturn ONLY one valid JSON object. Solve independently and do not discuss other models.",
                    system=system+" You are an independent structured-reasoning specialist.",
                    model=model,max_tokens=max_tokens,temperature=float(pc.get("temperature",0.2)),
                )]=(index,provider,model)
            drafts=[]
            for future in as_completed(futures):
                try:
                    value=str(future.result() or "").strip()
                    if value: drafts.append((futures[future],value))
                except Exception as exc:
                    log.debug("structured ensemble member failed: %s",exc)
            drafts.sort(key=lambda item:item[0][0])
            if drafts:
                evidence="\n\n".join(f"=== Draft {i} ===\n{trim(value,10000)}" for (i,_provider,_model),value in drafts)
                judge_model=str(pc.get("synthesis_model","auto/smart"))
                raw=cloud_client.chat(
                    prompt=f"Task:\n{prompt}\n\nIndependent structured drafts:\n{evidence}",
                    system=system+" Reconcile contradictions and return ONLY one valid JSON object.",
                    model=judge_model,max_tokens=max_tokens,temperature=0.1,
                )
                clean=str(raw or "").strip()
                if clean.startswith("```"):
                    parts=clean.split("```"); clean=parts[1] if len(parts)>1 else clean
                    if clean.lstrip().startswith("json"): clean=clean.lstrip()[4:]
                try:
                    return json.loads(clean.strip().strip("`"))
                except json.JSONDecodeError:
                    log.debug("structured ensemble judge returned invalid JSON; falling back")
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
