/* ═══════════ secpt 指挥台 V2（对齐高保真原型，接真实 API） ═══════════
   数据层：全部来自后端单点事实源，轮询增量：
   - 事件流：/api/jobs/{id}/events?after=seq（1.5s 增量，断点续传）
   - 作战面板：/api/jobs/{id}/workspace（4s）
   - 任务/资产/发现/经验/配置：8s 或进视图时拉取
   交互：追问当前任务 → 人工直投意图入池（source=direct）；
        下达新任务 → 后台线程跑 dag.run_task。 */
"use strict";
const $=(s,el=document)=>el.querySelector(s), $$=(s,el=document)=>[...el.querySelectorAll(s)];
const esc=t=>{const d=document.createElement("div");d.textContent=t==null?"":String(t);return d.innerHTML};
/* markdown 渲染（ARTEX markdown.tsx 对齐）：GFM 表格/代码块；先转义再解析，
   避免 XSS；marked 存在才启用，缺失回落 esc 纯文本。 */
function mdRender(t){
  const txt=String(t==null?"":t);
  if(window.marked&&window.marked.parse){
    try{
      window.marked.setOptions({gfm:true,breaks:true});
      const html=marked.parse(txt);
      const box=document.createElement("div");
      box.className="md";
      box.innerHTML=html;  // marked 输出；来源是自身事件流（智能体产出），非用户输入直插
      box.querySelectorAll("a").forEach(a=>{a.target="_blank";a.rel="noreferrer"});
      return box.outerHTML;
    }catch(e){}
  }
  return `<div class="md">${esc(txt)}</div>`;
}
const fmtT=ts=>{const d=new Date(ts*1000);return d.toTimeString().slice(0,8)};
const fmtDur=s=>s<60?`${s.toFixed(0)}s`:`${Math.floor(s/60)}m${Math.round(s%60)}s`;
const fmtSec=s=>{s=Math.max(0,s);if(s<0.05)return "<0.1s";return s<60?`${s.toFixed(1)}s`:`${Math.floor(s/60)}m${Math.round(s%60)}s`};
const fmtTok=n=>n>=1000?(n/1000).toFixed(1)+"k":""+n;
const fmtCny=n=>"¥"+(n||0).toFixed(4);

const M={jobs:[],events:[],workspace:{},findings:[],assets:[],
  tools:{builtin:[],plugins:[],skills:[]},experiences:[],config:{saved:{},effective:{}}};
const state={view:"workspace",jobId:"",filter:"all",follow:true,mode:"followup",taskFilter:"all",
  labels:JSON.parse(localStorage.getItem("secpt-labels")||"{}"),
  assetTypes:[],assetQ:"",assetPage:1,findSev:["critical","high","medium","low"],findQ:"",
  expTech:"",expQ:"",toolQ:"",composerFiles:[],cursor:0,newTaskMode:false,
  openKeys:new Set(),closedKeys:new Set(),roundOpen:{},workOpen:{},toolOpen:{}};
const AGENT_IC="⬡";
const ACOLORS=["#9E8CF0","#6BA8E8","#3ECF8E","#E8792B","#E86AA6","#E0B04A"];
/* 终端/文件卡片的高保真资源：内联 SVG（不引外链，离线可用） */
const ROBOT_SVG=`<svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true"><rect x="3.5" y="7.5" width="17" height="12" rx="3.5" fill="none" stroke="currentColor" stroke-width="1.6"/><circle cx="9" cy="13.4" r="1.35" fill="currentColor"/><circle cx="15" cy="13.4" r="1.35" fill="currentColor"/><path d="M9.5 16.6h5M12 7.5V4.2M8.6 4.2h6.8" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>`;
const BRAIN_SVG=`<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><path d="M9.5 4.5A3 3 0 0 0 6.6 7a3.2 3.2 0 0 0-1.8 2.8c0 1 .4 1.9 1.1 2.5a3 3 0 0 0 .1 4.9 3 3 0 0 0 2.5 2.3A3 3 0 0 0 12 21V4.8a3 3 0 0 0-2.5-.3Z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M14.5 4.5A3 3 0 0 1 17.4 7a3.2 3.2 0 0 1 1.8 2.8c0 1-.4 1.9-1.1 2.5a3 3 0 0 1-.1 4.9 3 3 0 0 1-2.5 2.3A3 3 0 0 1 12 21" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>`;
const TOOL_SVG=`<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><path d="M14.8 6.2a4.3 4.3 0 0 0-5.5 5.5L4.2 16.8a2 2 0 0 0 2.8 2.8l5.1-5.1a4.3 4.3 0 0 0 5.5-5.5l-2.4 2.4-2.6-.6-.6-2.6 2.8-2Z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>`;
const USER_SVG=`<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><circle cx="12" cy="8" r="3.6" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M4.8 20c.6-3.7 3.6-5.8 7.2-5.8s6.6 2.1 7.2 5.8" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>`;
const FILE_SVG=`<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><path d="M13.5 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8.5L13.5 3Z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M13.5 3v5.5H19" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>`;
/* 卡片展开态 + 复制缓冲：key = job:seq（避免把大段代码塞进 data-* 属性） */
const FILE_BUF=new Map();
const COPY_BUF=new Map();
function parseArgs(a){if(a&&typeof a==="object")return a;try{return JSON.parse(a||"{}")}catch{return {}}}
function copyText(txt,msg){
  const done=()=>toast(msg||"已复制到剪贴板");
  const fallback=()=>{
    const ta=document.createElement("textarea");
    ta.value=txt;ta.style.position="fixed";ta.style.left="-9999px";ta.style.opacity="0";
    document.body.appendChild(ta);ta.select();
    try{document.execCommand("copy");done()}catch(e){toast("复制失败")}
    document.body.removeChild(ta);
  };
  if(navigator.clipboard&&window.isSecureContext)navigator.clipboard.writeText(txt).then(done).catch(fallback);
  else fallback();
}
function acolor(n){let h=0;for(const ch of String(n))h=(h*31+ch.charCodeAt(0))>>>0;return ACOLORS[h%ACOLORS.length]}
const jobOf=id=>M.jobs.find(j=>j.job_id===id);
const curJob=()=>jobOf(state.jobId)||M.jobs[0]||{job_id:"",status:"—",task_type:"",objective:""};

/* ── 数据加载 ── */
async function api(path,opts){const r=await fetch(path,opts);if(!r.ok)throw new Error(`HTTP ${r.status}`);return r.json()}

async function loadJobs(){
  const d=await api("/api/jobs");
  M.jobs=(d.jobs||[]).map(j=>({job_id:j.job_id,status:j.status,task_type:j.task_type||"",
    objective:j.objective||"",created_at:j.created_at?new Date(j.created_at).getTime()/1000:0}));
  if(!M.jobs.length){
    // 列表被清空（删光任务）：重置选中态，回到"新建任务"空工作台
    const had=!!state.jobId;
    state.jobId="";state.cursor=0;M.events=[];M.workspace={};
    dockState.mapSig="";dockState.outSig="";
    state.newTaskMode=true;state.mode="new";
    if(had&&state.view==="workspace")renderBody();
    return;
  }
  if(!state.jobId){
    // 默认选中"对话根任务"：子任务（-iXXXX 意图段/-r2 续段/-rs）与父任务共享同一张
    // 作战地图，选中子任务会让地图/成果面板变空——对话视角应落在父任务上
    const root=M.jobs.find(j=>!_CHILD_RE.test(j.job_id));
    state.jobId=(root||M.jobs[0]).job_id;
  }
}
async function loadEvents(full){
  const jid=state.jobId;if(!jid)return;
  const after=full?0:state.cursor;
  const d=await api(`/api/jobs/${jid}/events?after=${after}`);
  if(full)M.events=[];
  M.events=M.events.concat(d.events||[]);
  state.cursor=d.cursor;
}
async function loadWorkspace(){if(state.jobId)M.workspace=await api(`/api/jobs/${state.jobId}/workspace`)}
/* ── 流式输出：SSE 实时订阅事件流，轮询降级为兜底 ──
   EventSource 断线自动重连（Last-Event-ID 续传）；连接成功后停用 1.5s 事件
   轮询（新事件即时到达、即时渲染），workspace/任务列表仍走低频轮询。 */
const stream={es:null,jobId:"",live:false};
function streamRender(){
  if(state.view==="workspace")renderFeed();
  renderDock();
}
function streamStop(){if(stream.es){try{stream.es.close()}catch(e){}stream.es=null}stream.live=false}
function streamStart(jid){
  if(stream.es&&stream.jobId===jid)return;      // 已在订阅
  streamStop();stream.jobId=jid;if(!jid)return;
  if(typeof EventSource==="undefined")return;   // 老浏览器回落轮询
  try{
    const es=new EventSource(`/api/jobs/${encodeURIComponent(jid)}/stream?after=${state.cursor||0}`);
    stream.es=es;
    es.onopen=()=>{stream.live=true};
    es.onmessage=ev=>{
      try{
        const e=JSON.parse(ev.data);
        if(!e||!(e.seq>0))return;
        if(state.jobId!==jid)return;             // 已切走：丢弃
        if(e.seq<=state.cursor)return;
        state.cursor=e.seq;
        M.events.push(e);
        streamRender();
      }catch(err){}
    };
    es.addEventListener("bye",()=>{streamStop();refreshJobData(true)});
    es.onerror=()=>{/* EventSource 自动重连；期间 1.5s 轮询兜底 */}
  }catch(e){stream.live=false}
}
async function loadAssets(){M.assets=(await api("/api/assets")).assets||[]}
async function loadFindings(){M.findings=(await api("/api/findings")).findings||[]}
async function loadTools(){const d=await api("/api/tools");
  M.tools={builtin:d.builtin||[],plugins:d.plugins||[],skills:d.skills||[]}}
async function loadExperience(){
  M.experiences=(await api("/api/experiences")).experiences.map(x=>({
    ...x,hits:(x.usage&&x.usage.hit)||0,injections:(x.usage&&x.usage.total)||0}));
}
async function loadConfig(){M.config=await api("/api/config")}

/* ── 侧栏导航 ── */
function navGroups(){
  const running=M.jobs.filter(j=>j.status==="running").length;
  return [
    {title:"", items:[["workspace","◈","工作台",running,""]]},
    {title:"运营", items:[
      ["dashboard","◱","总览",0,""],
      ["assets","▣","资产",M.assets.length,""],
      ["findings","⚑","发现",M.findings.length,"warn"],
    ]},
    {title:"能力", items:[
      ["tools","⚒","工具",0,""],
      ["experience","❄","经验",M.experiences.length,""],
    ]},
    {title:"系统", items:[["config","⚙","配置",0,""]]},
  ];
}
function navCounts(){return navGroups().flatMap(g=>g.items).map(([id,ic,label,n])=>[id,ic,label,n])}
function renderNav(){
  $("#navbtns").innerHTML=navGroups().map(g=>`
    ${g.title?`<div class="nav-group-cap">${g.title}</div>`:""}
    ${g.items.map(([id,ic,label,n,badge])=>`
      <button class="nav-btn ${state.view===id?"active":""}" data-v="${id}">
        <span class="ic">${ic}</span>${label}${n?`<span class="bdg ${badge}">${n>99?"99+":n}</span>`:""}
      </button>`).join("")}`).join("");
  $$("#navbtns .nav-btn").forEach(b=>b.onclick=()=>setView(b.dataset.v));
}
function setTask(jid){state.jobId=jid;state.cursor=0;M.events=[];state.newTaskMode=false;
  state.openKeys.clear();state.closedKeys.clear();state.roundOpen={};state.workOpen={};state.toolOpen={};FILE_BUF.clear();COPY_BUF.clear();
  M.workspace={};dockState.mapSig="";dockState.outSig="";
  streamStart(jid);
  renderDock();
  if(state.view!=="workspace")setView("workspace");else renderBody();refreshJobData(true)}
function setView(v){state.view=v;renderNav();renderBody()}

/* ── 轮次分组 ── */
function buildRounds(events){
  const rounds=new Map(),calls=new Map();
  for(const e of events){
    if(!rounds.has(e.round))rounds.set(e.round,{round:e.round,events:[],tokens:0,cost:0,tools:0,flags:0,errors:0,t0:e.ts,t1:e.ts,summary:""});
    const r=rounds.get(e.round);r.events.push(e);r.t1=Math.max(r.t1,e.ts);
    if(e.type==="tokens"){r.tokens+=(e.payload.prompt||0)+(e.payload.completion||0);r.cost+=e.payload.cost_cny||0}
    if(e.type==="tool_call"){r.tools++;calls.set(e.payload.call_id,{call:e})}
    if(e.type==="tool_result"){const c=calls.get(e.payload.call_id);if(c){c.result=e;if(e.payload.status!=="ok")r.errors++}}
    if(e.type==="assistant"&&e.payload.content)r.summary=e.payload.content.split("\n")[0].slice(0,88);
    if(e.type==="finding"&&e.payload.kind==="flag"){r.flags++}
  }
  return [...rounds.values()].sort((a,b)=>a.round-b.round||a.t0-b.t0);
}
function tryFmt(s){try{return JSON.stringify(JSON.parse(s),null,1)}catch{return s}}
function actorOf(e){return esc(actorName(e.actor||"agent"))}
/* 职名映射（ARCHITECTURE.md 规范）：actor 原始串 → 职名·说明；未知 actor 视为操作员实例（A/B/C…并行班次） */
function actorName(raw){
  const r=String(raw).toLowerCase();
  if(r.includes("commander"))return "指挥官";
  if(r.includes("sentinel")||r==="orchestrator")return "SNT 值守";
  if(r.includes("logistics")||r.includes("quartermaster"))return "QM 军需";
  if(r.includes("coach"))return "COH 教练";
  if(r.includes("verifier")||r.includes("delivery")||r.includes("gate"))return "VRF 核验";
  return "操作员 " + String(raw).slice(0,10);  // 操作员实例（job_id 班次）
}

/* ── 对话流渲染 ── */
function renderFeed(){
  const feed=$("#feed");if(!feed)return;
  const rounds=buildRounds(M.events);
  const last=rounds[rounds.length-1];
  const now=Date.now()/1000;
  const running=viewRunning();
  /* 面包屑与时间线保持同一口径：轮询期间持续更新，避免首屏旧值 */
  const _evs=M.events||[];
  if(_evs.length){
    const _t0=Math.min(..._evs.map(e=>e.ts));
    const _t1=Math.max(..._evs.map(e=>e.ts));
    const _set=(id,v)=>{const el=document.getElementById(id);if(el)el.textContent=v};
    _set("crumb-rounds",rounds.length);
    _set("crumb-dur",fmtDur((running?Math.max(now,_t1):_t1)-_t0));
    _set("crumb-tok",fmtTok(rounds.reduce((a,r)=>a+r.tokens,0)));
    _set("crumb-cost",fmtCny(rounds.reduce((a,r)=>a+r.cost,0)));
  }
  /* 展开态持久化：轮询每 1.5s 重建 DOM，用户的收起/展开不能被冲掉 */
  rounds.forEach(r=>{r.open=state.roundOpen[r.round]!==undefined?state.roundOpen[r.round]:(r.flags>0||r===last)});
  let html="";
  for(const r of rounds){
    const calls=r.events.filter(e=>e.type==="tool_call");
    const results=new Map(r.events.filter(e=>e.type==="tool_result").map(e=>[e.payload.call_id,e]));
    const st=roundStatus(r,r===last,running,curJob().status,now);
    let body="";
    if(r.open){
      const items=[
        ...calls.map(c=>({t:"tool",o:{call:c,result:results.get(c.payload.call_id)},seq:c.seq})),
        ...r.events.filter(e=>["assistant","user_msg","finding","error","loop_phase","status_change","plan_event","intent_event"].includes(e.type)&&(e.type!=="assistant"||e.payload.content)).map(e=>({t:"ev",o:e,seq:e.seq}))
      ].sort((a,b)=>a.seq-b.seq);
      /* 一轮内三块：用户输入（对话侧） / 思考与工作 / 工具与结果 */
      const users=[],work=[],tools=[];
      for(const i of items){
        if(i.t==="ev"&&isUserEvent(i.o))users.push(i.o);
        else if(i.t==="tool"||(i.t==="ev"&&["finding","error"].includes(i.o.type)))tools.push(i);
        else work.push(i);
      }
      if(users.length||r===rounds[0])body+=userGroupHTML(users);
      body+=sysTurnHeader(r,tools,work);
      if(work.length)body+=workPanelHTML(r,work,
        state.workOpen[r.round]!==undefined?state.workOpen[r.round]:true);
      if(tools.length)body+=panelHTML("tool",r.round,tools,
        state.toolOpen[r.round]!==undefined?state.toolOpen[r.round]:(running&&r===last));
    }
    html+=`<section class="round-panel ${r.open?"open":""}" data-round="${r.round}" data-anchor="round-${r.round}">
      <div class="round-head" data-act="r-toggle" data-r="${r.round}">
        <span class="ricon">▤</span><b>ROUND ${r.round}</b>
        <span class="rstat ${st.cls}"><i></i>${esc(st.text)}</span>
        <span class="rmeta">${fmtTok(r.tokens)} tok</span><span class="rdot">·</span>
        <span class="rmeta">${fmtCny(r.cost)}</span><span class="rdot">·</span>
        <span class="rmeta">${r.tools} tools</span>
        ${r.flags?`<span class="rflag">⚑${r.flags}</span>`:""}
        <span class="rsum">${esc(r.summary||"(无文本输出)")}</span>
        <span class="arr">▼</span>
      </div>
      ${r.open?`<div class="round-body">${body||'<div class="faint" style="font-size:12px">（本轮无展开内容）</div>'}</div>`:""}
    </section>`;
  }
  feed.innerHTML=`<div class="feed-inner">${html||`<div class="empty"><b>暂无轨迹</b>该任务尚未产生事件</div>`}</div>
    <div class="feed-live" id="feed-live"><span class="dots"><i></i><i></i><i></i></span>实时流式中…</div>`;
  renderTimeline(rounds,now,running);
  $$("[data-act=r-toggle]",feed).forEach(h=>h.onclick=()=>{
    const rid=+h.dataset.r,panel=h.closest(".round-panel");
    const open=!panel.classList.contains("open");
    if(!open){panel.classList.remove("open");state.roundOpen[rid]=false;return}
    state.roundOpen[rid]=true;
    const top=feed.scrollTop,follow=state.follow;
    state.follow=false;renderFeed();feed.scrollTop=top;state.follow=follow;
  });
  $$("[data-act=panel-toggle]",feed).forEach(h=>h.onclick=()=>{
    const rid=+h.dataset.r,kind=h.dataset.panel;
    const panel=h.closest(".work-panel,.tool-panel");
    const open=panel.classList.contains("collapsed");
    panel.classList.toggle("collapsed",!open);
    if(kind==="work")state.workOpen[rid]=open;else state.toolOpen[rid]=open;
  });
  $$("[data-act=msgg]",feed).forEach(h=>h.onclick=e=>{
    const k=h.dataset.key;
    const box=h.closest(".msg-group");
    const open=!box.classList.contains("open");
    box.classList.toggle("open",open);
    if(open)state.openKeys.add(k);else state.openKeys.delete(k);
  });
  $$("[data-act=trow3]",feed).forEach(h=>h.onclick=e=>{
    if(e.target.closest("[data-copy-key],[data-detail]"))return;
    const k=h.dataset.key;
    const box=h.closest(".term-card,.trow3");
    const open=!box.classList.contains("open");
    box.classList.toggle("open",open);
    if(open)state.openKeys.add(k);else state.openKeys.delete(k);
  });
  $$("[data-act=file-toggle]",feed).forEach(h=>h.onclick=e=>{
    if(e.target.closest("[data-copy-key]"))return;
    toggleFileCard(h.closest(".file-card"));
  });
  $$("[data-approve]",feed).forEach(b=>b.onclick=async()=>{
    b.disabled=true;
    try{
      const r=await api(`/api/jobs/${encodeURIComponent(b.dataset.job)}/approve`,{
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({pending_id:b.dataset.approve})});
      if(r.ok){
        appDecided.set(b.dataset.approve,"approved");
        const card=b.closest(".approval-card");
        if(card){
          card.querySelector(".ap-btns")?.remove();
          card.querySelector(".ap-row").insertAdjacentHTML("beforeend",'<span class="ap-badge approved">已允许</span>');
        }
        toast("已允许执行");
      }else toast(r.error||"操作失败");
    }catch(err){toast("操作失败："+err.message);b.disabled=false}
  });
  $$("[data-deny]",feed).forEach(b=>b.onclick=async()=>{
    b.disabled=true;
    try{
      const r=await api(`/api/jobs/${encodeURIComponent(b.dataset.job)}/deny`,{
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({pending_id:b.dataset.deny})});
      if(r.ok){
        appDecided.set(b.dataset.deny,"denied");
        const card=b.closest(".approval-card");
        if(card){
          card.querySelector(".ap-btns")?.remove();
          card.querySelector(".ap-row").insertAdjacentHTML("beforeend",'<span class="ap-badge denied">已拒绝</span>');
        }
        toast("已拒绝执行");
      }else toast(r.error||"操作失败");
    }catch(err){toast("操作失败："+err.message);b.disabled=false}
  });
  $$("[data-detail]",feed).forEach(b=>b.onclick=async()=>{
    const seq=+b.dataset.detail;
    b.disabled=true;
    try{
      const d=await api(`/api/jobs/${encodeURIComponent(state.jobId)}/event/${seq}`);
      if(d.ok){
        const box=b.previousElementSibling;
        const pre=document.createElement("pre");
        pre.className="term-out detail-full";
        pre.textContent=d.detail||"(空)";
        b.replaceWith(pre);
      }else toast(d.error||"读取失败");
    }catch(err){toast("读取失败："+err.message);b.disabled=false}
  });
  $$("[data-copy-key]",feed).forEach(b=>b.onclick=e=>{
    e.stopPropagation();
    const card=b.closest(".file-card");
    if(card){
      (async()=>{
        try{
          const d=await fetchFileCard(card);
          if(d.ok)copyText(d.content,b.dataset.copyMsg||"文件内容已复制");
          else toast(d.error||"读取失败");
        }catch(err){toast("读取失败："+err.message)}
      })();
      return;
    }
    const t=COPY_BUF.get(b.dataset.copyKey);
    if(t==null)return;
    copyText(t,b.dataset.copyMsg||"已复制到剪贴板");
  });
  /* ARTEX live 指示：流式在线且任务运行中 → feed 底部三点跳动"实时流式中…" */
  const liveEl=$("#feed-live");
  if(liveEl)liveEl.classList.toggle("show",!!(stream.live&&stream.jobId===state.jobId&&viewRunning()));
  applyFilter();
  scheduleTimelineSpy();
  if(state.locate&&Date.now()<state.locate.until){
    const sel=state.locate.kind==="round"
      ?`.round-panel[data-round="${state.locate.round}"]`
      :`[data-anchor="${state.locate.seq}"]`;
    const t=$(sel,feed);if(t)t.classList.add("flash");
  }
  if(state.follow)feed.scrollTop=feed.scrollHeight;
}
function roundStatus(r,isLast,running,jobStatus,now){
  const dur=(isLast&&running?now:r.t1)-r.t0;
  if(isLast&&running)return {cls:"run",text:`进行中，已用时 ${fmtSec(dur)}`};
  if(isLast&&["failed","cancelled","stalled"].includes(jobStatus))return {cls:"bad",text:`已结束，用时 ${fmtSec(dur)}`};
  return {cls:"done",text:`已完成，用时 ${fmtSec(dur)}`};
}
/* 用户直投消息（coach/budget/info 是系统注入，不算用户侧） */
function isUserEvent(e){
  if(e.type!=="user_msg")return false;
  const k=String((e.payload||{}).kind||"msg");
  return !["coach","budget","info"].includes(k);
}
const USER_KIND_LBL={direct:"追问/指令",new_task:"下达任务",steer:"操舵",msg:"消息"};
function userGroupHTML(list){
  const w=M.workspace||{};
  const items=list.map(e=>{
    const p=e.payload||{};
    const lbl=USER_KIND_LBL[p.kind]||(p.kind||"消息");
    return `<div class="ubub" data-seq="${e.seq}" data-anchor="user-${e.seq}"><span class="ubub-k">${esc(lbl)} · ${fmtT(e.ts)}</span>${esc(p.content)}</div>`;
  });
  /* 归档/续接任务本轮没有 user_msg：回填任务目标，保证"用户输入"始终可见 */
  if(!items.length){
    const obj=(w.goal&&w.goal.objective)||curJob().objective||"";
    if(obj)items.push(`<div class="ubub"><span class="ubub-k">任务目标</span>${esc(String(obj).replace(/^【任务目标】\n?/,"").slice(0,400))}</div>`);
  }
  if(!items.length)return "";
  return `<div class="turn user user-group">
    <div class="turn-body">
      <div class="turn-cap right"><b>用户输入</b><span>${items.length} 条</span></div>
      ${items.join("")}
    </div>
    <div class="turn-rail"><span class="turn-avatar user">${USER_SVG}</span></div>
  </div>`;
}
/* 系统响应 turn 头：机器人头像 +「本轮返回了什么」一句话摘要（ANSWER/成果/交付判定） */
function sysTurnHeader(round,tools,work){
  let out="";
  for(let i=work.length-1;i>=0;i--){
    const e=work[i].o,p=e.payload||{};
    if(e.type==="assistant"&&p.content){out=p.content;break}
  }
  if(!out){
    for(let i=tools.length-1;i>=0;i--){
      const it=tools[i];
      if(it.t==="ev"&&it.o.type==="finding"){out=`⚑ ${it.o.payload.value}`;break}
    }
  }
  const w=M.workspace||{};
  if(!out&&w.delivery)out=`交付判定：${w.delivery.accepted?"通过":"未通过"} — ${w.delivery.reason||""}`;
  if(!out&&round.round===0&&w.goal)out=`开始执行：${String(w.goal.objective||"").replace(/^【任务目标】\n?/,"").slice(0,120)}`;
  if(!out)out="";
  return `<div class="turn sys sys-head">
    <div class="turn-rail"><span class="turn-avatar sys">${ROBOT_SVG}</span></div>
    <div class="turn-body">
      <div class="turn-cap"><b>系统响应</b><span>${tools.length+work.length} 条过程记录</span></div>
      ${out?`<div class="sys-return">${esc(out.slice(0,300))}</div>`:""}
    </div></div>`;
}
function panelHTML(kind,round,items,open){
  const work=kind==="work";
  return `<section class="${work?"work-panel":"tool-panel"} ${open?"":"collapsed"}" data-r="${round}" data-panel="${kind}">
    <div class="panel-head" data-act="panel-toggle" data-r="${round}" data-panel="${kind}">
      <span class="panel-ic">${work?BRAIN_SVG:TOOL_SVG}</span>
      <b>${work?"系统响应 · 推理与规划":"系统响应 · 工具与结果"}</b>
      <span class="panel-n">${items.length} ${work?"条记录":"项调用/结果"}</span>
      <span class="arr">▼</span>
    </div>
    <div class="panel-body">${items.map(itemHTML).join("")}</div>
  </section>`;
}
/* ── ARTEX MessageBlock 语义：同一 worker 连续 assistant 事件合并为单个
   消息块（正文取最后一条结论），块头"· 含思考"提示；点击展开全部逐条。
   相比逐条平铺，长任务对话从 44 行折叠成十余个可扫读的消息块。 ── */
function workPanelHTML(round,items,open){
  /* 重排：把连续 assistant（同 worker 相邻、中间无工具/结算/规划事件）合并 */
  const groups=[];
  for(const i of items){
    const isAsst=i.t==="ev"&&i.o.type==="assistant";
    const last=groups[groups.length-1];
    if(isAsst&&last&&last.type==="msg"&&last.items[last.items.length-1].o.actor===i.o.actor)
      last.items.push(i);
    else groups.push(isAsst?{type:"msg",items:[i]}:{type:"one",item:i});
  }
  const html=groups.map(g=>{
    if(g.type==="one")return itemHTML(g.item);
    const its=g.items, last=its[its.length-1].o, first=its[0].o;
    const key=`${state.jobId}:msg:${first.seq}`;
    const open2=state.openKeys.has(key);
    const body=last.payload||{};
    const nearCap=(body.content||"").length>=1990||its.some(x=>(x.o.payload.content||"").length>=1990);
    return `<div class="msg-group" data-key="${key}">
      <button class="msgg-head" data-act="msgg" data-key="${key}">
        <span class="arr">▸</span>
        <span class="robot">${ROBOT_SVG}</span>
        <span class="abadge">${actorOf(last)}</span>
        <span class="t">${fmtT(first.ts)}${its.length>1?`–${fmtT(last.ts)}`:""} · ${its.length} 段思考</span>
        <span class="msgg-sum">${esc((body.content||"").split("\n")[0].slice(0,110))}</span>
        <span class="hint">· 含推理</span>
      </button>
      <div class="msgg-body">
        ${its.map(x=>{const p=x.o.payload||{};
          return `<div class="msgg-item"><div class="a-head"><span class="robot">${ROBOT_SVG}</span><span class="abadge">${actorOf(x.o)}</span><span class="t">${fmtT(x.o.ts)} · 思考</span></div><div class="a-body">${mdRender(p.content)}</div>${(p.content||"").length>=1990?`<button class="detail-btn" data-detail="${x.o.seq}">查看全文</button>`:""}</div>`}).join("")}
      </div>
      ${nearCap?`<button class="detail-btn" data-detail="${last.seq}">查看末段全文</button>`:""}
    </div>`;
  }).join("");
  return `<section class="work-panel ${open?"":"collapsed"}" data-r="${round.round}" data-panel="work">
    <div class="panel-head" data-act="panel-toggle" data-r="${round.round}" data-panel="work">
      <span class="panel-ic">${BRAIN_SVG}</span>
      <b>系统响应 · 推理与规划</b>
      <span class="panel-n">${items.length} 条记录</span>
      <span class="arr">▼</span>
    </div>
    <div class="panel-body">${html}</div>
  </section>`;
}
/* 审批卡（ARTEX InterceptCard 对齐）：拦截 ask 请求内嵌对话流，
   允许/拒绝直接在对话里点；按钮点击后立即翻转为决策态。 */
function approvalCardHTML(e){
  const p=e.payload||{};
  const pid=esc(p.pending_id||"");
  const state=state2=>state2;
  const decided=appDecided.get(p.pending_id||"");
  return `<div class="approval-card turn-item" data-seq="${e.seq}" data-pending="${pid}">
    <div class="ap-row">
      <span class="ap-ic">🛡</span>
      <div class="ap-main">
        <div class="ap-title"><b>审批请求</b><code>${esc(p.tool||"")}</code><span class="ap-id">#${esc(String(p.pending_id||"").replace(/^icp-/,""))}</span></div>
        <pre class="ap-cmd">${esc(p.command||"")}</pre>
      </div>
      ${decided?`<span class="ap-badge ${decided}">${decided==="approved"?"已允许":"已拒绝"}</span>`
        :`<div class="ap-btns">
            <button class="ap-allow" data-approve="${pid}" data-job="${esc(e.job_id||state.jobId)}">允许</button>
            <button class="ap-deny" data-deny="${pid}" data-job="${esc(e.job_id||state.jobId)}">拒绝</button>
          </div>`}
    </div>
    <div class="ap-foot">规则：${esc(p.rule||"")} · 等待审批中（超时按规则处理）</div>
  </div>`;
}
const appDecided=new Map();  // pending_id → approved|denied（页面内存；刷新后靠回执事件）
function renderFeed2(){}
function itemHTML(i){
  if(i.t==="tool")return toolItemHTML(i.o);
  const e=i.o,p=e.payload||{};
  if(e.type==="assistant"){
    const nearCap=(p.content||"").length>=1990;
    return `<div class="msg-agent thought turn-item" data-seq="${e.seq}"><div class="a-head"><span class="robot">${ROBOT_SVG}</span><span class="abadge">${actorOf(e)}</span><span class="t">${fmtT(e.ts)} · 思考</span></div><div class="a-body">${mdRender(p.content)}</div>${nearCap?`<button class="detail-btn" data-detail="${e.seq}">查看全文</button>`:""}</div>`;
  }
  if(e.type==="user_msg"){
    const kindLabel={coach:"教练注入",budget:"预算警告",info:"系统注入"}[p.kind]||(p.kind||"系统消息");
    return `<div class="inject-note turn-item" data-seq="${e.seq}"><span class="k">${esc(kindLabel)} · ${fmtT(e.ts)}</span>${esc(p.content)}</div>`;
  }
  if(e.type==="finding"){
    return `<div class="milestone turn-item" data-seq="${e.seq}"><span class="mk">${p.kind==="flag"?"⚑":"◈"}</span><span class="mv">${esc(p.value)}</span>${p.evidence?`<div class="sub">evidence · ${esc(p.evidence)}</div>`:""}</div>`;
  }
  if(e.type==="error"){
    return `<div class="milestone finding turn-item" data-seq="${e.seq}"><span class="mk">⚠</span><span class="mv">${esc(p.message||JSON.stringify(p))}</span></div>`;
  }
  if(e.type==="loop_phase"){
    return `<div class="sysnote turn-item" data-seq="${e.seq}">phase → ${esc(p.phase)}${p.reason?`（${esc(p.reason)}）`:""}</div>`;
  }
  if(e.type==="approval_request"){
    return approvalCardHTML(e);
  }
  if(e.type==="approval_decision"){
    // 决策回执：更新同 pending 的卡片状态（若在视口内）
    return `<div class="sysnote turn-item" data-seq="${e.seq}" data-approval="${esc(p.pending_id)}">审批 ${p.decision==="approved"?"已允许":"已拒绝"} · ${fmtT(e.ts)}</div>`;
  }
  if(e.type==="status_change"){
    return `<div class="sysnote turn-item" data-seq="${e.seq}">status → ${esc(p.status)}</div>`;
  }
  if(e.type==="plan_event"){
    const w=e.payload.wake||{};const wakeTxt=w.detail||w.kind||"常规规划";
    const bits=[`决策 <b>${esc(p.decision||"continue")}</b>`,`派 <b>${p.tasks||0}</b> 个任务`];
    if(p.beliefs)bits.push(`信念结算 ${p.beliefs}`);
    if(p.hypotheses)bits.push(`新假设 ${p.hypotheses}`);
    if(p.drift_rejected)bits.push(`<span style="color:var(--c-bad)">防漂移拒收</span>`);
    const planned=(p.planned||[]).map(t=>`<li>${esc(t.title)}<span class="lane ${t.lane==="main"?"lane-main":"lane-logistics"}">${t.lane==="main"?"主":"后勤"}</span></li>`).join("");
    return `<div class="msg-agent thought plan turn-item" data-seq="${e.seq}"><div class="a-head"><span class="robot">${ROBOT_SVG}</span><span class="abadge">${actorOf({actor:"commander"})}</span><span class="t">${fmtT(e.ts)} · 规划</span></div><div class="a-body">${bits.join(" · ")}<div class="sub" style="color:var(--c-time);font-size:11.5px;margin-top:3px">唤醒：${esc(wakeTxt)}</div>${planned?`<ul class="planlist">${planned}</ul>`:""}</div></div>`;
  }
  if(e.type==="intent_event"){
    const st=e.payload.status;
    const verb={claimed:"▸ 领取执行",open:"↩ 复核回队",blocked:"⏸ 挂起",done:"✓ 完成",cancelled:"◼ 已取消",failed:"✗ 失败",exhausted:"▽ 耗尽"}[st]||"✗ 结算";
    const laneTxt=e.payload.lane==="logistics"?" · 后勤":"";
    return `<div class="msg-agent act turn-item" data-seq="${e.seq}" data-anchor="intent-${e.payload.iid||e.seq}"><div class="a-head"><span class="abadge">${AGENT_IC} ${actorOf(e)}</span><span class="t">${fmtT(e.ts)} · 任务结算</span></div><div class="a-body actline"><b>${verb}</b>「${esc(e.payload.title||"")}」${laneTxt}${e.payload.reason?`<span class="sub"> · ${esc(e.payload.reason)}</span>`:""}</div></div>`;
  }
  return "";
}
/* ── 对话时间线（迷你轨）：用户 / 轮次 / 意图 三个层级，工具明细留在对话流 ── */
function renderTimeline(rounds,now,running){
  const el=$("#feed-timeline");if(!el)return;
  const evs=M.events||[];
  if(!evs.length){el.innerHTML="";return}
  const t0=Math.min(...evs.map(e=>e.ts));
  const tLast=Math.max(...evs.map(e=>e.ts));
  const t1=Math.max(tLast,t0+1,running?now:tLast);
  const pos=ts=>2+96*Math.max(0,Math.min(1,(ts-t0)/(t1-t0)));
  /* 刻度样式：节点=横线段（子任务刻度），长度按时长缩放；hover 提示，点击定位 */
  const scale=d=>{  // 时长→线宽：0-8px 短刻度，长任务最长 30px（右对齐伸出）
    if(d<=0.05)return 8;
    return Math.min(30,8+Math.sqrt(d)*3);
  };
  let nodes="";
  for(const e of evs){
    if(e.type==="user_msg"&&isUserEvent(e)){
      const lbl=USER_KIND_LBL[e.payload.kind]||e.payload.kind||"消息";
      nodes+=`<button class="tl-node user tick" style="top:${pos(e.ts)}%;width:${scale(0)}px" data-kind="user" data-round="${e.round}" data-anchor="user-${e.seq}" data-tip="${esc(`用户 · ${fmtT(e.ts)} · ${lbl}`)}" aria-label="用户输入"></button>`;
    }
  }
  for(const r of rounds){
    const dur=(running&&r===rounds[rounds.length-1]?now:r.t1)-r.t0;
    const st=roundStatus(r,r===rounds[rounds.length-1],running,curJob().status,now);
    nodes+=`<button class="tl-node round tick ${st.cls}" style="top:${pos(r.t0)}%;width:${scale(dur)}px" data-kind="round" data-round="${r.round}" data-anchor="round-${r.round}" data-tip="${esc(`ROUND ${r.round} · ${fmtT(r.t0)}–${fmtT(r.t1)} · ${st.text}`)}" aria-label="轮次"></button>`;
  }
  /* 子任务刻度：同一 iid 的 claimed/done/exhausted 聚合；线宽=时长，末态决定颜色 */
  const intents=new Map();
  for(const e of evs){
    if(e.type!=="intent_event")continue;
    const iid=e.payload.iid||("s"+e.seq);
    const cur=intents.get(iid);
    if(cur){cur.last=e;if(e.payload.status)cur.status=e.payload.status}
    else intents.set(iid,{first:e,last:e,status:e.payload.status});
  }
  for(const [iid,x] of intents){
    const f=x.first,p=f.payload||{};
    const dur=Math.max(0,x.last.ts-f.ts);
    const stCls=x.status==="done"?"ok":x.status==="exhausted"||x.status==="failed"?"bad":x.status==="running"?"run":"";
    const tip=`意图 · ${p.title||iid} · ${x.status||""} · ${fmtT(f.ts)}${dur>0.05?` · 用时 ${fmtSec(dur)}`:""}`;
    nodes+=`<button class="tl-node intent tick ${stCls}" style="top:${pos(f.ts)}%;width:${scale(dur)}px" data-kind="intent" data-round="${f.round}" data-anchor="intent-${esc(iid)}" data-tip="${esc(tip)}" aria-label="意图"></button>`;
  }
  const total=(running?now:tLast)-t0;
  const bad=["failed","cancelled","stalled"].includes(curJob().status);
  const tCls=running?"run":bad?"bad":"done";
  const tK=running?"● 进行中":bad?"✕ 已结束":"✓ 任务完成";
  const tV=fmtSec(total);
  const tTitle=running?`进行中 · 累计 ${tV}`:(bad?`任务结束 · 总耗时 ${tV}`:`任务完成 · 总耗时 ${tV}`);
  el.innerHTML=`<div class="tl-wrap">
    <div class="tl-side-head">
      <span class="tl-title">时间线</span>
      <div class="tl-total ${tCls}" title="${esc(tTitle)}"><span class="tl-total-k">${tK}</span><b>${tV}</b></div>
    </div>
    <div class="tl-track">${nodes}<div class="tl-tip" id="tl-tip"></div></div>
  </div>`;
  const track=$(".tl-track",el),tip=$("#tl-tip",el);
  $$(".tl-node",el).forEach(n=>{
    n.onmousemove=()=>{
      const nr=n.getBoundingClientRect(),tr=track.getBoundingClientRect();
      tip.textContent=n.dataset.tip||"";
      tip.style.top=Math.max(10,Math.min(tr.height-10,nr.top-tr.top+nr.height/2))+"px";
      tip.classList.add("show");
      const p=$(`.round-panel[data-round="${n.dataset.round}"]`);
      if(p)p.classList.add("linked");
    };
    n.onmouseleave=()=>{
      tip.classList.remove("show");
      const p=$(`.round-panel[data-round="${n.dataset.round}"]`);
      if(p)p.classList.remove("linked");
    };
    n.onclick=()=>locateTimelineNode(n);
  });
  requestAnimationFrame(updateTimelineActive);
}
/* ── 迷你轨 scroll spy：随对话流滚动高亮当前锚点（不联动滚动，天然无同步问题） ── */
let _tlRaf=0;
function scheduleTimelineSpy(){
  if(_tlRaf)return;
  _tlRaf=requestAnimationFrame(()=>{_tlRaf=0;updateTimelineActive()});
}
function updateTimelineActive(){
  const feed=$("#feed"),rail=$("#feed-timeline");
  if(!feed||!rail)return;
  const nodes=$$(".tl-node",rail);if(!nodes.length)return;
  const anchors=$$("[data-anchor]",feed);if(!anchors.length)return;
  const line=feed.getBoundingClientRect().top+80;
  let key=anchors[0].dataset.anchor;
  for(const a of anchors){
    const r=a.getBoundingClientRect();
    if(!r.width&&!r.height)continue;  // 折叠面板内的隐藏锚点不参与高亮
    if(r.top<=line)key=a.dataset.anchor;
    else break;
  }
  if(feed.scrollTop+feed.clientHeight>=feed.scrollHeight-4)key=anchors[anchors.length-1].dataset.anchor;
  if(state.tlPin){
    if(Date.now()<state.tlPin.until)key=state.tlPin.anchor;
    else state.tlPin=null;
  }
  nodes.forEach(n=>n.classList.toggle("active",n.dataset.anchor===key));
}
function locateTimelineNode(n){
  const feed=$("#feed");if(!feed)return;
  const kind=n.dataset.kind,round=+n.dataset.round||0,anchor=n.dataset.anchor||"";
  if(state.filter!=="all"){  // 过滤态下目标可能被隐藏：定位前恢复全量视图
    state.filter="all";
    $$(".chip[data-f]").forEach(x=>x.classList.toggle("on",x.dataset.f==="all"));
  }
  state.roundOpen[round]=true;
  if(kind==="intent")state.workOpen[round]=true;
  state.follow=false;
  const wf=$("#ws-follow");if(wf)wf.checked=false;
  state.locate={kind,round,seq:anchor,until:Date.now()+1800};
  state.tlPin={anchor,until:Date.now()+1500};
  renderFeed();
  const target=feed.querySelector(`[data-anchor="${anchor}"]`)
    ||(kind==="round"?feed.querySelector(`.round-panel[data-round="${round}"]`):null);
  if(!target){toast("该节点未在当前视图（可能被过滤或未渲染）");return}
  target.scrollIntoView({behavior:"smooth",block:"center"});
  target.classList.add("flash");
  setTimeout(()=>target.classList.remove("flash"),1600);
  setTimeout(updateTimelineActive,450);
}
/* 文件卡展开：正文按需回源（大段代码不进首屏 DOM） */
async function toggleFileCard(card){
  if(!card)return;
  const key=card.dataset.key;
  const body=card.querySelector(".file-body");
  const open=!card.classList.contains("open");
  if(!open){
    card.classList.remove("open");state.openKeys.delete(key);return;
  }
  card.classList.add("open");state.openKeys.add(key);
  if(body.dataset.filled==="1")return;
  body.innerHTML=`<div class="file-loading">加载文件内容…</div>`;
  try{
    const d=await fetchFileCard(card);
    if(d.ok){
      body.innerHTML=`<pre class="code"><code>${esc(d.content)}</code></pre>`;
      body.dataset.filled="1";COPY_BUF.set(key,d.content);
    }else body.innerHTML=`<div class="file-err">${esc(d.error||"读取失败")}</div>`;
  }catch(e){body.innerHTML=`<div class="file-err">${esc("读取失败："+e.message)}</div>`}
}

/* ── 任务列表（工作台左列）：父子任务合并为一行 ──
   一个任务下达后编排层会派生多个子任务（-iXXXX 意图段 / -r2 续段 / -rs RESET），
   原样平铺会出现"一次下达冒出 N 个任务"。这里按任务树合并：
   - 父任务行 = 树的展示单位（名称/目标取父，状态聚合：有 running 即 running）
   - 子任务折叠进父行，悬浮可见清单，点击 hover 展开的 ⋚ 切换展开
   - 无父（列表被截断）的子任务独立成行 */
const _CHILD_RE=/-(?:i[0-9a-fA-F]{4,8}|r\d+|rs)$/;
function parentOf(jid){return jid.replace(_CHILD_RE,"")}
function buildTaskTree(jobs){
  const byId=new Map(jobs.map(j=>[j.job_id,j]));
  const groups=new Map();  // parentId -> {parent, children[]}
  const order=[];
  for(const j of jobs){
    const pid=parentOf(j.job_id);
    const parent=byId.get(pid);
    if(parent&&pid!==j.job_id){
      if(!groups.has(pid)){groups.set(pid,{parent,children:[]});order.push(pid)}
      groups.get(pid).children.push(j);
    }else if(pid!==j.job_id){
      // 孤儿子任务（父任务已删除）：不再平铺成独立任务——跟随"删除整树"语义隐藏。
      // 兜底：若它已是当前选中任务，仍归属到祖先根下展示。
      const gp=byId.get(pid)?null:pid;
      if(gp&&state.jobId===j.job_id){
        if(!groups.has(j.job_id)){groups.set(j.job_id,{parent:j,children:[]});order.push(j.job_id)}
      }
      // 其余孤儿直接不渲染（历史已随树删除；这里只是列表残留的防御）
    }else{
      if(!groups.has(j.job_id)){groups.set(j.job_id,{parent:j,children:[]});order.push(j.job_id)}
    }
  }
  return order.map(pid=>groups.get(pid));
}
function treeStatus(g){
  const all=[g.parent,...g.children];
  if(all.some(j=>j.status==="running"))return "running";
  if(all.some(j=>j.status==="failed"||j.status==="error"))return "failed";
  if(all.every(j=>j.status==="succeeded"||j.status==="completed"||j.status==="done"))return "succeeded";
  return g.parent.status;
}
/* 状态灯：只有 running 是绿；终态成功=橙、失败/取消=红、其他=灰 */
function dotClass(status){
  const s=(status||"").toLowerCase();
  if(s==="awaiting_confirm")return "warn";
  if(s==="running")return "running";
  if(s==="failed"||s==="error"||s==="cancelled"||s==="exhausted"||s==="stalled")return "bad";
  if(s==="succeeded"||s==="completed"||s==="done")return "okwarn";
  return "off";
}
function taskRunning(jid){
  const j=jobOf(jid);
  if(!j)return false;
  if(j.status==="running")return true;
  // 整树语义：任一子任务（-iXXXX/-r2 派生）在跑即算运行中
  const prefix=j.job_id+"-";
  return M.jobs.some(x=>x.job_id.startsWith(prefix)&&x.status==="running");
}
const viewRunning=()=>taskRunning(state.jobId);
function syncSendBtn(){
  const run=viewRunning();
  const bi=$("#btn-interrupt");if(bi)bi.classList.toggle("show",run);
  const gd=$("#gen-dots");if(gd)gd.classList.toggle("show",run);
  const btn=$("#btn-send");if(btn)btn.title="Ctrl+Enter 发送";
}
function labelOf(j){return state.labels[j.job_id]||j.job_id}
function renameTask(jid){
  const cur=state.labels[jid]||"";
  const nm=prompt("为任务重命名：",cur);
  if(nm===null)return;
  const v=nm.trim();
  if(!v){delete state.labels[jid];toast("已恢复默认名称")}
  else{state.labels[jid]=v;toast("已重命名为「"+v+"」")}
  localStorage.setItem("secpt-labels",JSON.stringify(state.labels));
  renderTaskPanel();
}
async function downloadLog(jid){
  const job=jobOf(jid);if(!job)return;
  let evs=[];
  try{const d=await api(`/api/jobs/${jid}/events?after=0`);evs=d.events||[]}catch(e){}
  const name=state.labels[jid]||jid;
  let txt="=== secpt 运行日志 ===\n";
  txt+=`任务: ${name} (${jid})\n`;
  txt+=`目标: ${job.objective.replace(/\s+/g," ").slice(0,200)}\n`;
  txt+=`状态: ${job.status}  类型: ${job.task_type}\n`;
  txt+=`生成时间: ${new Date().toLocaleString()}\n`;
  txt+=`事件数: ${evs.length}\n`;
  txt+="\n--- 事件流 ---\n";
  evs.forEach(e=>{
    const p=e.payload||{};
    txt+=`[${String(e.seq).padStart(4,"0")}] R${e.round} · ${fmtT(e.ts)} · ${e.type} · ${e.actor||"agent"}\n`;
    if(e.type==="tool_call")txt+=`  tool: ${p.tool}\n  args: ${tryFmt(typeof p.args==="string"?p.args:JSON.stringify(p.args))}\n`;
    else if(e.type==="tool_result")txt+=`  status: ${p.status} · ${p.bytes||0}B\n  output: ${String(p.preview||"(空)").slice(0,400)}\n`;
    else if(e.type==="assistant")txt+=`  思考: ${String(p.content||"").slice(0,500)}\n`;
    else if(e.type==="user_msg")txt+=`  用户(${p.kind||"msg"}): ${String(p.content||"").slice(0,300)}\n`;
    else if(e.type==="finding")txt+=`  成果(${p.kind||""}): ${p.value}\n`;
    else if(["error","loop_phase","status_change","verdict"].includes(e.type))txt+=`  ${JSON.stringify(p).slice(0,200)}\n`;
  });
  const blob=new Blob([txt],{type:"text/plain;charset=utf-8"});
  const url=URL.createObjectURL(blob);
  const a=document.createElement("a");
  a.href=url;a.download=`${name}_${jid}_log.txt`;
  document.body.appendChild(a);a.click();document.body.removeChild(a);
  setTimeout(()=>URL.revokeObjectURL(url),1000);
  toast(`已下载日志 · ${evs.length} 条事件`);
}
function renderTaskPanel(){
  const el=$("#hist-list");if(!el)return;
  const st=M.jobs.reduce((a,j)=>(a[j.status]=(a[j.status]||0)+1,a),{});
  const tf=$("#t-filters");
  if(tf){
    tf.innerHTML=[["all","全部"],["running","●"+(st.running||0)]].map(([v,l])=>`<button class="chip ${state.taskFilter===v?"on":""}" data-tf="${v}">${l}</button>`).join("");
    $$(".chip",tf).forEach(c=>c.onclick=()=>{state.taskFilter=c.dataset.tf;renderTaskPanel()});
  }
  let list=[...M.jobs].sort((a,b)=>((b.status==="running")-(a.status==="running"))||(b.created_at||0)-(a.created_at||0));
  let tree=buildTaskTree(list);
  if(state.taskFilter==="running")tree=tree.filter(g=>treeStatus(g)==="running");
  if(state.expandedTrees===undefined)state.expandedTrees=new Set();
  el.innerHTML=tree.map(g=>{
    const st=treeStatus(g);
    const dc=dotClass(st);
    const lab=state.labels[g.parent.job_id];
    const hasKids=g.children.length>0;
    const expanded=state.expandedTrees.has(g.parent.job_id);
    const sel=[g.parent.job_id,...g.children.map(c=>c.job_id)].includes(state.jobId);
    const runN=g.children.filter(c=>c.status==="running").length;
    const isRunning=st==="running";
    const kids=hasKids?g.children.map(c=>{
      const cd=dotClass(c.status);
      return `<div class="trow child ${c.job_id===state.jobId?"sel":""}" data-j="${c.job_id}" style="display:${expanded?"":"none"}">
        <div class="t-id"><span class="dot ${cd}"></span><div class="t-name-col"><span class="t-name">${esc(c.job_id.split("-").slice(-2).join("-"))}</span></div></div>
        <div class="t-obj">${esc((c.objective||"").replace(/\s+/g," ").replace(/^【总目标】\n?/,"").slice(0,36))}</div>
      </div>`}).join(""):"";
    return `${kids}<div class="trow ${sel?"sel":""}" data-j="${g.parent.job_id}" data-tree="${hasKids?1:0}" title="${hasKids?esc(g.children.map(c=>c.job_id+" · "+c.status).join("\n")):""}">
      <div class="t-id"><span class="dot ${dc}"></span><div class="t-name-col"><span class="t-name">${esc(lab||g.parent.job_id)}</span></div></div>
      <div class="t-obj">${esc((g.parent.objective||"").replace(/\s+/g," ").slice(0,42))}</div>
      <div class="t-acts">
        ${hasKids?`<span class="twbadge" data-act="tw" title="子任务数${runN?` · ${runN} 执行中`:""}">⚑${g.children.length}${runN?`<span class="subn" style="color:var(--ok)"> ●${runN}</span>`:""}</span>`:""}
        <button class="t-act" data-act="menu" title="任务操作">⋯</button>
      </div></div>`}).join("")
    ||`<div class="empty" style="padding:18px 6px">无任务</div>`;
  $$(".trow",el).forEach(r=>{
    r.onclick=e=>{
      if(e.target.closest(".t-act"))return;
      setTask(r.dataset.j);
    };
    $$(".t-act",r).forEach(b=>b.onclick=e=>{
      e.stopPropagation();
      if(b.dataset.act==="tw"){
        const pid=r.dataset.j;
        state.expandedTrees.has(pid)?state.expandedTrees.delete(pid):state.expandedTrees.add(pid);
        renderTaskPanel();
      }
      else if(b.dataset.act==="menu")openTaskMenu(r);
    });
  });
}

/* ── 对话条目分派：bash→终端卡 / write_file→文件卡 / 其余→通用工具日志 ── */
function toolItemHTML(o){
  const name=o.call.payload.tool;
  if(name==="bash")return termRowHTML(o.call,o.result);
  if(name==="write_file")return fileCardHTML(o.call,o.result);
  return toolRowHTML(o.call,o.result,name);
}
/* 工具命令摘要（ARTEX toolInputText 语义）：bash 抽 command 字段显示命令本身 */
function toolCmdText(tool,args){
  const a=parseArgs(args);
  if(tool==="bash")return String(a.command||"");
  if(tool==="read_file"||tool==="list_dir")return String(a.path||"");
  return "";
}
/* 单行工具行（ARTEX ToolBlock 语义）：收起时一行摘要，点击展开参数+输出全文 */
function toolRowHTML(call,result,tool){
  const name=tool||call.payload.tool;
  const key=`${state.jobId}:tl:${call.seq}`;
  const open=state.openKeys.has(key);
  const a=parseArgs(call.payload.args);
  const cmd=toolCmdText(name,a);
  const st=result?(result.payload.status==="ok"?"ok":"err"):"wait";
  const stTxt=result?(result.payload.status==="ok"?"完成":result.payload.status):"执行中…";
  const bytes=result?(result.payload.bytes||0):0;
  const argsTxt=tryFmt(typeof call.payload.args==="string"?call.payload.args:JSON.stringify(call.payload.args));
  const out=result?String(result.payload.preview||""):"";
  const full=result?(result.payload.detail!=null?String(result.payload.detail):out):"";
  COPY_BUF.set(key,`$ ${cmd}\n${out}`);
  return `<div class="trow3 ${open?"open":""} ${result?"":"pending"}" data-key="${key}" data-seq="${call.seq}">
    <button class="trow3-head" data-act="trow3" data-key="${key}">
      <span class="arr">▸</span>
      <span class="tname">${esc(name)}</span>
      ${cmd?`<span class="tcmd" title="${esc(cmd)}">${esc(cmd)}</span>`:""}
      <span class="tstat ${st}">${esc(stTxt)}${result?` · ${bytes}B`:""}</span>
    </button>
    <div class="trow3-body">
      <span class="lbl">args · 调用参数</span><pre>${esc(argsTxt)}</pre>
      ${result?`<span class="lbl">output · 响应结果</span><pre>${esc(out||"(空)")}</pre>${(result.payload.bytes||0)>480?`<button class="detail-btn" data-detail="${call.seq}">查看全文（${result.payload.bytes}B）</button>`:""}`:`<span class="lbl">executing · 执行中</span><pre>等待工具返回…</pre>`}
    </div>
  </div>`;
}
/* 终端执行行：与 toolRow 同一单行语义，但保留黑底终端体 */
function termRowHTML(call,result){
  const key=`${state.jobId}:tm:${call.seq}`;
  const open=state.openKeys.has(key);
  const a=parseArgs(call.payload.args);
  const cmd=String(a.command||(typeof call.payload.args==="string"?call.payload.args:"")||"");
  const st=result?(result.payload.status==="ok"?"ok":"err"):"wait";
  const stTxt=result?(result.payload.status==="ok"?"完成":result.payload.status):"执行中…";
  const bytes=result?(result.payload.bytes||0):0;
  const out=result?String(result.payload.preview||""):"";
  COPY_BUF.set(key,`$ ${cmd}\n${out}`);
  return `<div class="term-card ${open?"open":""} ${result?"":"pending"}" data-key="${key}" data-seq="${call.seq}">
    <button class="term-head" data-act="trow3" data-key="${key}">
      <span class="arr">▸</span>
      <span class="tlights"><i></i><i></i><i></i></span>
      <span class="cmd-name">bash</span>
      <span class="tcmd" title="${esc(cmd)}">${esc(cmd)}</span>
      <span class="term-st ${st}">${esc(stTxt)}${result?` · ${bytes}B`:""}</span>
      <span class="term-time">${fmtT(call.ts)}</span>
      <span class="copy-btn" data-copy-key="${key}" data-copy-msg="终端内容已复制">复制</span>
    </button>
    <div class="term-body">
      <pre class="term-cmd"><span class="ps1">$</span>${esc(cmd)}</pre>
      ${result?`<pre class="term-out">${esc(out||"(无输出)")}</pre>${(result.payload.bytes||0)>480?`<button class="detail-btn" data-detail="${call.seq}">查看全文（${result.payload.bytes}B，输出已截断）</button>`:""}`:`<div class="term-wait"><span class="dots"><i></i><i></i><i></i></span>执行中…</div>`}
    </div>
  </div>`;
}
/* 终端执行：黑底 + 绿色左边框 + 等宽字体 + Copy（#0d1117 / font-mono） */
function termCardHTML(call,result){
  const a=parseArgs(call.payload.args);
  const cmd=String(a.command||(typeof call.payload.args==="string"?call.payload.args:"")||"");
  const st=result?(result.payload.status==="ok"?"ok":"err"):"wait";
  const stTxt=result?(result.payload.status==="ok"?"完成":result.payload.status):"执行中…";
  const bytes=result?(result.payload.bytes||0):0;
  const out=result?String(result.payload.preview||""):"";
  const key=`${state.jobId}:${call.seq}`;
  COPY_BUF.set(key,`$ ${cmd}\n${out}`);
  return `<div class="term-card turn-item ${result?"":"pending"}" data-seq="${call.seq}">
    <div class="term-head">
      <span class="tlights"><i></i><i></i><i></i></span>
      <span class="cmd-name">bash</span>
      <span class="term-st ${st}">${esc(stTxt)}${result?` · ${bytes}B`:""}</span>
      <span class="term-time">${fmtT(call.ts)}</span>
      <button class="copy-btn" data-copy-key="${key}" data-copy-msg="终端内容已复制">复制</button>
    </div>
    <pre class="term-cmd"><span class="ps1">$</span>${esc(cmd)}</pre>
    ${result?`<pre class="term-out">${esc(out||"(无输出)")}</pre>${(result.payload.bytes||0)>480?`<button class="detail-btn" data-detail="${call.seq}">查看全文（${result.payload.bytes}B，输出已截断）</button>`:""}`:`<div class="term-wait"><span class="dots"><i></i><i></i><i></i></span>执行中…</div>`}
  </div>`;
}
/* 写文件：可折叠卡片（头部=路径+状态，正文=代码块，默认收起）
   tool_call.args 在事件流里被截断到 500 字符 → 路径/大小取自 tool_result
   的 "wrote N bytes to …/jobs/<sub>/ws/<path>"，正文展开时回磁盘读。 */
function fileCardHTML(call,result){
  const key=`${state.jobId}:${call.seq}`;
  const pv=result?String(result.payload.preview||""):"";
  let sub=String(call.payload.subjob||""),path="",size=null;
  const m=pv.match(/wrote\s+(\d+)\s+bytes\s+to\s+(.+?)\s*$/);
  if(m){
    size=+m[1];
    const mm=m[2].match(/jobs\/([^/]+)\/ws\/(.+)$/);
    if(mm){sub=mm[1];path=mm[2]}
  }
  if(!path){
    const a=parseArgs(call.payload.args);
    path=String(a.path||"");
    if(size==null&&a.content!=null)size=String(a.content).length;
    if(!path){
      const sm=String(call.payload.args||"").match(/"path"\s*:\s*"((?:[^"\\]|\\.)*)"/);
      if(sm){try{path=JSON.parse(`"${sm[1]}"`)}catch{path=sm[1]}}
    }
  }
  const ok=!!result&&result.payload.status==="ok";
  const stTxt=result?(ok?"成功":"失败"):"写入中…";
  const open=state.openKeys.has(key);
  const cached=FILE_BUF.get(key);
  const bodyHTML=open?(cached!=null?`<pre class="code"><code>${esc(cached)}</code></pre>`:`<div class="file-loading">加载文件内容…</div>`):"";
  return `<div class="file-card turn-item ${open?"open":""}" data-seq="${call.seq}" data-key="${key}" data-sub="${esc(sub)}" data-path="${esc(path)}" data-full="${cached!=null?"1":"0"}">
    <div class="file-head" data-act="file-toggle">
      <span class="ficon">${FILE_SVG}</span>
      <span class="fpath" title="${esc(path||"(未知路径)")}">${esc(path||"(未知路径)")}</span>
      <span class="fstat ${ok?"ok":"err"}">${esc(stTxt)}</span>
      <span class="fmeta">${size!=null?size+"B":"—"} · ${fmtT(call.ts)}</span>
      <button class="copy-btn" data-copy-key="${key}" data-copy-msg="文件内容已复制">复制</button>
      <span class="arr">▼</span>
    </div>
    <div class="file-body" data-filled="${cached!=null?"1":"0"}">${bodyHTML}</div>
  </div>`;
}
/* 文件正文按需回源：GET /api/jobs/{id}/file?sub=&path=（服务端做归属/穿越校验） */
async function fetchFileCard(card){
  const key=card.dataset.key;
  if(card.dataset.full==="1"&&FILE_BUF.has(key))return {ok:true,content:FILE_BUF.get(key)};
  const qs=`sub=${encodeURIComponent(card.dataset.sub||"")}&path=${encodeURIComponent(card.dataset.path||"")}`;
  const d=await api(`/api/jobs/${encodeURIComponent(state.jobId)}/file?${qs}`);
  if(d&&d.ok){
    FILE_BUF.set(key,d.content);COPY_BUF.set(key,d.content);
    card.dataset.full="1";card.dataset.filled="1";
  }
  return d||{error:"读取失败"};
}
function toolLogHTML(c){
  const st=c.result?(c.result.payload.status==="ok"?"ok":"err"):"wait";
  const stTxt=c.result?c.result.payload.status:"执行中…";
  const bytes=c.result?(c.result.payload.bytes||0):0;
  const args=c.call.payload.args;
  const key=`${state.jobId}:t:${c.call.seq}`;
  const open=!state.closedKeys.has(key);
  return `<div class="tlog turn-item ${open?"open":""} ${c.result?"":"pending"}" data-seq="${c.call.seq}">
    <div class="tl-head" data-act="tl-toggle" data-k="${c.call.seq}"><span class="arr">▼</span><span>⚙</span><span class="tname">${esc(c.call.payload.tool)}</span><span class="abadge" style="--ac:${acolor(c.call.actor)}">${AGENT_IC} ${actorOf(c.call)}</span>
      <span class="st ${st}">${stTxt} · ${bytes}B · ${fmtT(c.call.ts)}</span></div>
    <div class="tl-body">
      <span class="lbl">args · 调用参数</span><pre>${esc(tryFmt(typeof args==="string"?args:JSON.stringify(args)))}</pre>
      ${c.result?`<span class="lbl">output · 响应结果（${esc(stTxt)} · ${bytes}B）</span><pre>${esc(c.result.payload.preview||"(空)")}</pre>`:`<span class="lbl">executing · 执行中</span><pre>等待工具返回…</pre>`}
    </div></div>`;
}
function applyFilter(){
  const f=state.filter,feed=$("#feed");if(!feed)return;
  $$(".round-panel",feed).forEach(p=>{
    if(f==="all"){
      p.style.display="";
      $$(".work-panel,.tool-panel",p).forEach(x=>x.style.display="");
      $$(".turn-item",p).forEach(x=>x.style.display="");
      return;
    }
    let any=false;
    $$(".work-panel,.tool-panel",p).forEach(panel=>{
      const isTool=panel.classList.contains("tool-panel");
      let has=false;
      $$(".turn-item",panel).forEach(x=>{
        let show=false;
        if(f==="tool")show=isTool&&x.matches(".tlog,.term-card,.file-card");
        else if(f==="agent")show=!isTool&&x.matches(".msg-agent");
        else if(f==="milestone")show=x.matches(".milestone");
        x.style.display=show?"":"none";
        if(show)has=true;
      });
      panel.style.display=has?"":"none";
      if(has)any=true;
    });
    p.style.display=any?"":"none";
  });
}
/* ── 工作台骨架 ── */
function workspaceHTML(){
  if(state.newTaskMode){
    $("#k-run").textContent=M.jobs.filter(x=>x.status==="running").length;
    $("#k-flag").textContent=0;
    $("#k-cost").textContent=fmtCny(0);
    $("#k-model").textContent=M.config.effective.model_primary||"model";
    return `<div class="ws2">
    <aside class="hist">
      <button class="newtask" id="btn-camp-new">＋ 新建任务</button>
      <div class="hist-cap"><span>任务 · ${M.jobs.length}</span><span style="flex:1"></span><span id="t-filters" style="display:flex;gap:5px"></span></div>
      <div class="hist-list" id="hist-list"></div>
    </aside>
    <div class="stage new-mode">
      <div class="center-hero">
        <div class="hero-brand">${AGENT_IC} secpt <span class="muted">· 下达新任务</span></div>
        <div class="composer center">
          <div class="gen-dots" id="gen-dots"><i></i><i></i><i></i><span>智能体生成中…</span></div>
          <div class="attach-strip" id="attach-strip"></div>
          <textarea id="c-in" rows="3" placeholder="输入安全任务，或回复智能体…（Enter 发送，Shift+Enter 换行）" title="目标 / 范围 / 约束 / 交付要求…（附件点 ＋，Ctrl+Enter 发送）"></textarea>
          <div class="composer-foot">
            <button class="cbtn" id="btn-attach" title="附件 / 图片">＋</button>
            <input type="file" id="c-files" multiple hidden>
            <div class="mode-switch">
              <button data-m="followup" class="${state.mode==="followup"?"on":""}">追问当前任务</button>
              <button data-m="new" class="on">下达新任务</button>
            </div>
            <label class="follow" title="默认勾选：任务确认后立即派单执行；取消勾选=指挥官先产《接战单》，你在作战地图确认后才派单"><input type="checkbox" id="c-auto" checked>自动下发</label>
            <span class="cmodel">${AGENT_IC} ${esc(M.config.effective.model_primary||"model")}</span>
            <button class="btn-interrupt" id="btn-interrupt" title="停止任务（协作式取消，引擎下一步优雅退出）">中断任务</button>
            <button class="cbtn send" id="btn-send" title="Enter 发送 · Shift+Enter 换行">发送</button>
          </div>
        </div>
        <div class="hero-hint">任务流：接战受理（接战单确认）→ Commander 编成派单 → 执行体 ReAct 并行 → 后勤后台 → 交付门验收 → 经验沉淀</div>
      </div>
    </div>
  </div>`;
  }
  const j=curJob();
  const rounds=buildRounds(M.events);
  const cost=rounds.reduce((a,r)=>a+r.cost,0);
  const tokens=rounds.reduce((a,r)=>a+r.tokens,0);
  const dur=rounds.length?(rounds[rounds.length-1].t1-rounds[0].t0):0;
  const flags=(M.workspace.flags||[]).length;
  $("#k-cost").textContent=fmtCny(cost);
  $("#k-flag").textContent=flags;
  $("#k-run").textContent=M.jobs.filter(x=>x.status==="running").length;
  $("#k-model").textContent=M.config.effective.model_primary||"model";
  const title=(state.labels[j.job_id]||(j.objective||"").replace(/^【总目标】\n?/,"")).split("\n")[0].slice(0,46);
  return `<div class="ws2">
    <aside class="hist">
      <button class="newtask" id="btn-camp-new">＋ 新建任务</button>
      <div class="hist-cap"><span>任务 · ${M.jobs.length}</span><span style="flex:1"></span><span id="t-filters" style="display:flex;gap:5px"></span></div>
      <div class="hist-list" id="hist-list"></div>
    </aside>
    <div class="stage">
      <div class="crumb-bar">
        <span class="dot ${dotClass(j.status)}"></span>
        <span class="ic">◈</span>
        <span class="title">${esc(title)||"（无标题）"}</span>
        <span class="meta">${esc(j.job_id)} · <b>${esc(j.status)}</b> · ${esc(j.task_type)}</span>
        <div class="crumb-metrics"><span><b id="crumb-rounds">${rounds.length}</b> rounds</span><span>⏱ <b id="crumb-dur">${fmtDur(dur)}</b></span><span><b id="crumb-tok">${fmtTok(tokens)}</b> tok</span><span><b id="crumb-cost">${fmtCny(cost)}</b></span><span class="${flags?"gold":""}">⚑ <b>${flags}</b></span></div>
      </div>
      <div class="anchor-bar">
        <span class="anchor flag">⚑ <span class="n">${flags}</span> 成果</span>
        <span class="anchor err">⚠ <span class="n">${rounds.reduce((a,r)=>a+r.errors,0)}</span> 错误</span>
        ${[["all","全部"],["tool","工具"],["agent","智能体"],["milestone","成果"]].map(([v,l])=>`<button class="chip ${state.filter===v?"on":""}" data-f="${v}">${l}</button>`).join("")}
        <label class="follow"><input type="checkbox" id="ws-follow" ${state.follow?"checked":""}>跟随</label>
      </div>
      <div class="feed-row">
        <aside class="feed-timeline" id="feed-timeline" aria-label="对话时间线"></aside>
        <div class="feed" id="feed"></div>
      </div>
      <div class="composer-float">
        <div class="composer">
          <div class="gen-dots" id="gen-dots"><i></i><i></i><i></i><span>智能体生成中…</span></div>
          <div class="attach-strip" id="attach-strip"></div>
          <textarea id="c-in" rows="3" placeholder="输入安全任务，或回复智能体…（Enter 发送，Shift+Enter 换行）" title="${state.mode==="followup"?(taskRunning(j.job_id)?"追问当前任务：发送即入作战地图（人工直投，规划让路）…":"任务已收敛 · 发送将以同一张作战地图续接编排（不新开任务）…"):"目标 / 范围 / 约束 / 交付要求…（将新建独立任务）"}"></textarea>
          <div class="composer-foot">
            <button class="cbtn" id="btn-attach" title="附件（下达新任务时随任务上传）">＋</button>
            <input type="file" id="c-files" multiple hidden>
            <div class="mode-switch">
              <button data-m="followup" class="${state.mode==="followup"?"on":""}" title="消息进入当前任务的作战地图：运行中=人工直探；已收敛=自动续接编排">对话中发布</button>
              <button data-m="new" class="${state.mode==="new"?"on":""}" title="新建独立任务（新对话、新地图）">下达新任务</button>
            </div>
            <label class="follow" title="默认勾选：任务确认后立即派单执行；取消勾选=指挥官先产《接战单》，你在作战地图确认后才派单"><input type="checkbox" id="c-auto" checked>自动下发</label>
            <span class="cmodel">${AGENT_IC} ${esc(M.config.effective.model_primary||"model")}</span>
            <button class="btn-interrupt" id="btn-interrupt" title="停止任务（协作式取消，引擎下一步优雅退出）">中断任务</button>
            <button class="cbtn send" id="btn-send" title="Enter 发送 · Shift+Enter 换行">发送</button>
          </div>
        </div>
      </div>
    </div>
  </div>`;
}

/* ── 工作台绑定（真实 API） ── */
function bindWorkspace(){
  const body=$("#body");
  renderFeed();
  renderTaskPanel();
  $$(".chip[data-f]",body).forEach(c=>c.onclick=()=>{state.filter=c.dataset.f;$$(".chip[data-f]",body).forEach(x=>x.classList.toggle("on",x===c));applyFilter()});
  const wf=$("#ws-follow");if(wf)wf.onchange=e=>{state.follow=e.target.checked};
  /* 手动上滚 → 暂停跟随（否则 1.5s 轮询重建会把视图拽回底部）；回到底部自动恢复 */
  const feedEl=$("#feed");
  if(feedEl)feedEl.addEventListener("scroll",()=>{
    const near=feedEl.scrollHeight-feedEl.scrollTop-feedEl.clientHeight<48;
    if(!near&&state.follow){state.follow=false;if(wf)wf.checked=false}
    scheduleTimelineSpy();
  },{passive:true});
  // 新建模式无 feed：renderFeed/renderTaskPanel 对缺失元素有守卫，安全
  $$(".mode-switch button",body).forEach(b=>b.onclick=()=>{
    if(b.dataset.m==="followup"&&!state.jobId){toast("还没有可追问的任务，请先下达新任务");return}
    state.mode=b.dataset.m;
    if(state.newTaskMode&&b.dataset.m==="followup")state.newTaskMode=false;
    renderBody()});
  const cin=$("#c-in");
  cin.addEventListener("keydown",e=>{
    if(e.key==="Enter"&&!e.shiftKey&&!e.isComposing){e.preventDefault();sendComposer()}
  });
  $("#btn-send").onclick=()=>sendComposer();
  const bi=$("#btn-interrupt");if(bi)bi.onclick=()=>stopComposer();
  $("#btn-attach").onclick=()=>$("#c-files").click();
  $("#c-files").onchange=async e=>{
    for(const f of e.target.files)state.composerFiles.push(await toB64(f));
    e.target.value="";renderAttachStrip();
  };
  syncSendBtn();
  const cn=$("#btn-camp-new");if(cn)cn.onclick=()=>{setView("workspace");state.mode="new";state.newTaskMode=true;renderBody();$("#c-in").focus()};
}
async function stopComposer(){
  const jid=state.jobId;if(!jid)return;
  try{
    await api(`/api/jobs/${jid}/cancel`,{method:"POST"});
    toast(`已请求停止 ${jid}（整树协作式取消，引擎下一步优雅退出）`);
    await loadJobs();renderBody();syncSendBtn();
  }catch(e){toast(`停止失败：${e.message}`)}
}
function toB64(file){
  return file.arrayBuffer().then(buf=>{
    let bin="";const b=new Uint8Array(buf);
    for(let i=0;i<b.length;i+=0x8000)bin+=String.fromCharCode(...b.subarray(i,i+0x8000));
    return {name:file.name,data_b64:btoa(bin),isImage:(file.type||"").startsWith("image/")};
  });
}
function renderAttachStrip(){
  const bar=$("#attach-strip");if(!bar)return;
  bar.innerHTML=state.composerFiles.map((f,i)=>
    `<span class="attach-chip">${f.isImage?`<img src="data:;base64,${f.data_b64}" alt="">`:"📄"}${esc(f.name)}<b data-rm="${i}">✕</b></span>`).join("");
  bar.style.display=state.composerFiles.length?"flex":"none";
  $$("[data-rm]",bar).forEach(x=>x.onclick=()=>{state.composerFiles.splice(+x.dataset.rm,1);renderAttachStrip()});
}
function openTaskMenu(row){
  const jid=row.dataset.j;
  const old=$(".tmenu");if(old)old.remove();
  const menu=document.createElement("div");
  menu.className="tmenu";
  const running=taskRunning(jid);
  menu.innerHTML=`
    <button data-m="ren">✎ 重命名</button>
    <button data-m="tw">⚑ 子任务展开/收起</button>
    <button data-m="dl">⤓ 下载运行日志</button>
    ${running?'<button data-m="stop" class="danger">◼ 停止任务</button>':""}
    <button data-m="del" class="danger">🗑 删除任务</button>`;
  document.body.appendChild(menu);
  const r=row.getBoundingClientRect();
  menu.style.top=Math.min(innerHeight-menu.offsetHeight-8,r.bottom+2)+"px";
  menu.style.left=Math.min(innerWidth-menu.offsetWidth-8,r.right-menu.offsetWidth)+"px";
  const close=()=>{menu.remove();document.removeEventListener("click",onDoc)};
  const onDoc=e=>{if(!menu.contains(e.target))close()};
  setTimeout(()=>document.addEventListener("click",onDoc),0);
  menu.onclick=async e=>{
    const act=e.target.dataset.m;if(!act)return;close();
    if(act==="ren")renameTask(jid);
    else if(act==="tw"){state.expandedTrees.has(jid)?state.expandedTrees.delete(jid):state.expandedTrees.add(jid);renderTaskPanel()}
    else if(act==="dl")downloadLog(jid);
    else if(act==="stop"){
      try{await api(`/api/jobs/${jid}/cancel`,{method:"POST"});toast(`已请求停止 ${jid}`);loadJobs().then(renderTaskPanel)}
      catch(err){toast(`停止失败：${err.message}`)}
    }
    else if(act==="del"){
      if(!confirm(`删除任务 ${jid}？运行中会先停止；将整树移除（含派生子任务），历史目录保留。`))return;
      try{await api(`/api/jobs/${jid}`,{method:"DELETE"});
        toast("已删除（整树）");await loadJobs();
        // 选中态可能落在被删树的任一成员上（父/子）：全部清空重置
        if(state.jobId===jid||state.jobId.startsWith(jid+"-")||jid.startsWith(state.jobId+"-")){
          streamStop();
          state.jobId=M.jobs[0]?M.jobs[0].job_id:"";state.cursor=0;M.events=[];refreshJobData(true);
        }
        else renderTaskPanel();
      }catch(err){toast(`删除失败：${err.message}`)}
    }
  };
}
async function sendComposer(){
  const cin=$("#c-in");const text=cin.value.trim();
  if(!text){toast("内容为空");return}
  cin.value="";
  try{
    if(state.mode==="followup"&&state.jobId){
      const r=await api(`/api/jobs/${state.jobId}/message`,{
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({content:text})});
      if(r.resumed){
        toast(`已发布并续接编排（同一作战地图）· ${r.intent_id||"direct"}`);
        await loadJobs();
      }else{
        toast(`已注入任务池（${r.intent_id||"direct"}）· 规划让路，执行体接单`);
      }
      refreshJobData(true);
    }else{
      const auto=(document.getElementById("c-auto")||{}).checked||false;
      const r=await api("/api/jobs",{
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({objective:text,profile:(state.jobId&&curJob().task_type)||"auto",
          files:state.composerFiles,fake:false,auto_dispatch:auto})});
      state.composerFiles=[];
      state.newTaskMode=false;
      toast(auto?`新任务已启动：${r.job_id}（${r.mode}）`
                :`接战单生成中：${r.job_id} · 请在右侧作战地图确认开战`);
      await loadJobs();setTask(r.job_id);
    }
  }catch(e){toast(`下发失败：${e.message}`)}
}

/* ── 右上角悬浮手风琴坞（作战地图 / 产出成果）──
   交互：点击只切换本地 open 类（CSS 过渡），不发请求、不重建 DOM；
   数据：按签名比对，只有工作区数据确实变化时才更新对应面板，轮询空转
        不产生任何 DOM 写入；展开态与面板滚动位置保持不变。 */
const dockState={open:new Set(),mapSig:"",outSig:"",built:false,bound:false};

function closeDock(){
  dockState.open.clear();
  $$("#dock .dock-item").forEach(it=>{
    it.classList.remove("open");
    const b=it.querySelector(".dock-pill");
    if(b)b.setAttribute("aria-expanded","false");
  });
}

/* 骨架只建一次：标题栏（概要）+ 可折叠内容区 */
function dockSkeleton(){
  const dock=$("#dock");if(!dock||dockState.built)return;
  dock.innerHTML=`
    <div class="dock-item" data-d="tasks" style="display:none">
      <button class="dock-pill map" data-act="dock" aria-expanded="false">
        <span class="dot" id="dock-map-dot"></span>
        <span class="dp-title">作战地图</span>
        <span class="dp-meta" id="dock-map-meta"></span>
        <span class="dp-state" id="dock-map-state"></span>
        <span class="caret-ic">▼</span>
      </button>
      <div class="dock-panel"><div class="dock-panel-inner" id="dock-map-body"></div></div>
    </div>
    <div class="dock-item" data-d="outcome" style="display:none">
      <button class="dock-pill outcome" data-act="dock" aria-expanded="false">
        <span class="dp-ic">⚑</span>
        <span class="dp-title">产出成果</span>
        <span class="dp-meta" id="dock-out-meta"></span>
        <span class="dp-state" id="dock-out-state"></span>
        <span class="caret-ic">▼</span>
      </button>
      <div class="dock-panel"><div class="dock-panel-inner" id="dock-out-body"></div></div>
    </div>`;
  dockState.built=true;
}

function dockSyncOpen(item,key){
  if(!item)return;
  const open=dockState.open.has(key);
  item.classList.toggle("open",open);
  const b=item.querySelector(".dock-pill");
  if(b)b.setAttribute("aria-expanded",String(open));
}

function dockMapBody(w,intents,hyps,hc,running,open,done,settledN){
  const stColor={open:"var(--info)",running:"var(--ok)",done:"var(--fg3)",exhausted:"var(--bad)"};
  const hypColor={confirmed:"var(--ok)",refuted:"var(--fg3)",dead:"var(--fg3)",testing:"var(--info)",proposed:"var(--warn)"};
  const hypLbl={confirmed:"已证实",refuted:"已证伪",dead:"已放弃",testing:"验证中",proposed:"新提出"};
  const intentRow=i=>`<div class="kcard" style="display:flex;gap:9px;align-items:flex-start">
      <span class="dot" style="margin-top:5px;${i.status==="running"?"animation:pulse 1.8s infinite;":""}background:${stColor[i.status]||"var(--fg3)"}"></span>
      <div class="kmain">${i.source==="direct"?'<span class="badge b-type">人工</span>':""}${esc(i.title)}<span class="lane ${i.lane==="main"?"lane-main":"lane-logistics"}">${i.lane==="main"?"主":"后勤"}</span>
      <div class="ksub"><span>${esc(i.status)}</span>${i.hypothesis?`<span>→ ${esc(i.hypothesis)}</span>`:""}${i.fail_reason?`<span>${esc(i.fail_reason.slice(0,60))}</span>`:""}</div></div></div>`;
  return `
    ${w.status==="awaiting_confirm"?`<div class="accept-card">
      <div class="ac-cap">📋 接战单 · 待确认</div>
      ${w.goal&&w.goal.restatement?`<div class="ac-row"><span class="mlbl">目标重述</span>${esc(w.goal.restatement)}</div>`:""}
      ${w.goal?`<div class="ac-row"><span class="mlbl">任务目标</span>${esc((w.goal.objective||"").slice(0,140))}</div>`:""}
      ${w.goal&&(w.goal.success_criteria||[]).length?`<div class="ac-row"><span class="mlbl">成功标准</span><ul>${w.goal.success_criteria.slice(0,5).map(c=>`<li>${esc(c)}</li>`).join("")}</ul></div>`:""}
      ${w.goal&&(w.goal.constraints||[]).length?`<div class="ac-row"><span class="mlbl">操作约束</span>${esc(w.goal.constraints.slice(0,3).join("；"))}</div>`:""}
      <div class="ac-btns">
        <button class="btn primary" data-act="confirm-accept">✔ 确认开战</button>
        <button class="btn" data-act="cancel-accept">✘ 取消</button>
      </div>
      <div class="ac-hint">确认后 Commander 才编成派单；取消可重发修正后的任务</div>
    </div>`:""}
    ${w.focus?`<div class="mapsec focus"><span class="mlbl">主攻方向</span>${esc(w.focus)}</div>`:""}
    ${w.goal?`<div class="mapsec"><span class="mlbl">总目标</span>${esc((w.goal.objective||"").slice(0,90))}${(w.goal.asset_scope||[]).length?`<span class="sub">${esc(w.goal.asset_scope.slice(0,4).join("、"))}</span>`:""}</div>`:""}
    <div class="mapbar"><i style="width:${intents.length?done/intents.length*100:0}%"></i></div>
    <div class="dockstat"><span class="ds-running">${running.length} 执行中</span><span class="ds-open">${open.length} 待执行</span><span class="ds-done">${done} 完成</span>${settledN?`<span class="ds-exhausted">${settledN} 结算</span>`:""}<span>${intents.length} 项</span></div>
    ${Object.keys(hc).filter(k=>hc[k]>0&&hypLbl[k]).map(k=>`<span class="hpchip" style="color:${hypColor[k]};border-color:${hypColor[k]}">${hypLbl[k]} ${hc[k]}</span>`).join("")}
    ${hyps.filter(h=>h.state==="testing"||h.state==="proposed").slice(0,4).map(h=>`<div class="hypline"><span class="hpchip sm" style="color:${hypColor[h.state]};border-color:${hypColor[h.state]}">${hypLbl[h.state]||h.state}</span><span class="htxt" title="${esc(h.test||"")}">${esc(h.title)}</span><span class="conf">${Math.round((h.confidence||0)*100)}%</span></div>`).join("")}
    <div class="mapsec-cap">执行中</div>
    ${running.map(intentRow).join("")||'<div class="faint" style="font-size:11px;margin:2px 0 6px">—</div>'}
    <div class="mapsec-cap">待执行</div>
    ${open.slice(0,6).map(intentRow).join("")||'<div class="faint" style="font-size:11px;margin:2px 0 6px">—</div>'}`;
}

function dockOutBody(flags,w,finds){
  return `
    ${w.result?`<div class="resultbox"><div class="rb-cap">最终结果 · result.md</div><div class="rb-body">${mdRender(w.result)}</div></div>`:""}
    ${flags.map(f=>`<div class="flagbox"><span>${esc(f.payload.value)}</span><button data-act="copy">复制</button></div>`).join("")}
    ${w.delivery?`<div class="deliveryline ${w.delivery.accepted?"ok":"bad"}">交付判定：${esc((w.delivery.reason||"").slice(0,80))}</div>`:""}
    <h5 style="${flags.length?"margin-top:15px;":""}">FINDINGS · 本任务 ${finds.length}</h5>
    ${finds.slice(0,8).map(f=>`<div class="hyp" data-f="${f.id}"><div class="h-top"><span class="badge b-${f.severity}">${f.severity}</span><span class="conf">${((f.confidence||0)*100).toFixed(0)}%</span></div><div class="h-title">${esc(f.title)}</div></div>`).join("")||'<div class="faint" style="font-size:11.5px">本任务暂无发现</div>'}`;
}

function renderDock(force=false){
  const dock=$("#dock");if(!dock)return;
  dockSkeleton();
  const w=M.workspace||{},intents=w.intents||[];
  const hyps=w.hypotheses||[],hc=w.hypothesis_counts||{};
  const done=intents.filter(i=>i.status==="done").length;
  const running=intents.filter(i=>i.status==="running");
  const open=intents.filter(i=>i.status==="open");
  const settledN=intents.length-done-running.length-open.length;
  const flags=(M.events||[]).filter(e=>e.type==="finding"&&e.payload.kind==="flag");
  const finds=(M.findings||[]).filter(f=>f.job_id===state.jobId||String(f.job_id||"").startsWith((state.jobId||"⌂")+"-"));
  const hasMap=!!(intents.length||hyps.length||w.focus||w.status==="awaiting_confirm");
  const hasOut=!!(flags.length||finds.length||w.delivery||w.result);

  const mapItem=dock.querySelector('[data-d="tasks"]');
  const outItem=dock.querySelector('[data-d="outcome"]');
  if(mapItem){mapItem.style.display=hasMap?"":"none";dockSyncOpen(mapItem,"tasks")}
  if(outItem){outItem.style.display=hasOut?"":"none";dockSyncOpen(outItem,"outcome")}

  /* 数据签名：只比对内容字段，不含时间戳，轮询空转时零 DOM 写入 */
  const mapSig=JSON.stringify([w.status,w.focus,
    w.goal?[w.goal.objective,w.goal.restatement,w.goal.asset_scope,w.goal.constraints,w.goal.success_criteria]:null,
    intents.map(i=>[i.id,i.title,i.status,i.lane,i.hypothesis,i.fail_reason,i.source]),
    hyps.map(h=>[h.id,h.state,h.title,h.test,Math.round((h.confidence||0)*100)]),hc]);
  const outSig=JSON.stringify([flags.map(f=>f.payload.value),w.delivery,
    finds.map(f=>[f.id,f.severity,f.title,f.confidence]),w.result||""]);

  if(hasMap&&(force||mapSig!==dockState.mapSig)){
    const dot=$("#dock-map-dot",dock),meta=$("#dock-map-meta",dock),st=$("#dock-map-state",dock);
    if(dot)dot.className="dot "+dotClass(w.status||"");
    if(meta)meta.textContent=w.status==="awaiting_confirm"?"接战单待确认"
      :`${intents.length} 项${running.length?` · ${running.length} 执行中`:""}${done?` · ${done} 完成`:""}`;
    if(st){
      const L={running:"运行中",awaiting_confirm:"待确认",succeeded:"已完成",failed:"失败",cancelled:"已取消",stalled:"已停滞"}[w.status]||"";
      st.textContent=L;
      st.className="dp-state "+(w.status==="running"?"run":w.status==="awaiting_confirm"?"warn":"done");
    }
    const body=$("#dock-map-body",dock);
    if(body){const sc=body.scrollTop;body.innerHTML=`<div class="dock-pad">${dockMapBody(w,intents,hyps,hc,running,open,done,settledN)}</div>`;body.scrollTop=sc}
    dockState.mapSig=mapSig;
  }else if(!hasMap){dockState.mapSig=""}

  if(hasOut&&(force||outSig!==dockState.outSig)){
    const meta=$("#dock-out-meta",dock),st=$("#dock-out-state",dock);
    if(meta)meta.textContent=[w.result?"结果就绪":"",flags.length?`${flags.length} 旗`:"",finds.length?`${finds.length} 发现`:""].filter(Boolean).join(" · ")||"—";
    if(st){
      st.textContent=w.delivery?(w.delivery.accepted?"已交付":"未通过"):"收集中";
      st.className="dp-state "+(w.delivery?(w.delivery.accepted?"done":"bad"):"run");
    }
    const body=$("#dock-out-body",dock);
    if(body){const sc=body.scrollTop;body.innerHTML=`<div class="dock-pad">${dockOutBody(flags,w,finds)}</div>`;body.scrollTop=sc}
    dockState.outSig=outSig;
  }else if(!hasOut){dockState.outSig=""}
}

/* 事件委托只绑一次：点击不触发任何渲染，仅切换本地 open 类 */
function bindDockEvents(){
  const dock=$("#dock");if(!dock||dockState.bound)return;
  dockState.bound=true;
  dock.addEventListener("click",async e=>{
    const pill=e.target.closest("[data-act=dock]");
    if(pill){
      const item=pill.closest(".dock-item"),key=item.dataset.d;
      const open=!item.classList.contains("open");
      item.classList.toggle("open",open);
      pill.setAttribute("aria-expanded",String(open));
      if(open)dockState.open.add(key);else dockState.open.delete(key);
      return;
    }
    const acc=e.target.closest("[data-act=confirm-accept]");
    if(acc){
      acc.disabled=true;
      try{
        const r=await api(`/api/jobs/${state.jobId}/confirm`,{method:"POST"});
        if(r.ok){toast("接战单已确认 · Commander 开始编成派单");await loadWorkspace();renderDock(true);refreshJobData(true);}
        else toast(r.error||"确认失败");
      }catch(err){toast(`确认失败：${err.message}`)}
      acc.disabled=false;
      return;
    }
    const cancel=e.target.closest("[data-act=cancel-accept]");
    if(cancel){
      cancel.disabled=true;
      try{
        const r=await api(`/api/jobs/${state.jobId}`,{method:"DELETE"});
        toast(r.ok?"已取消 · 可修正任务后重发":(r.error||"取消失败"));
        await loadJobs();
      }catch(err){toast(`取消失败：${err.message}`)}
      cancel.disabled=false;
      return;
    }
    const copy=e.target.closest("[data-act=copy]");
    if(copy){
      const val=copy.closest(".flagbox")?.querySelector("span")?.textContent;
      if(val)copyText(val);
      return;
    }
    const hyp=e.target.closest(".hyp[data-f]");
    if(hyp){
      const f=(M.findings||[]).find(x=>x.id===hyp.dataset.f);
      if(f)openFinding(f);
    }
  });
}

/* ── 其他视图 ── */
function toolUsage(){const m={};M.events.forEach(e=>{if(e.type==="tool_call")m[e.payload.tool]=(m[e.payload.tool]||0)+1});return m}

function renderBody(){
  const body=$("#body");
  if(state.view==="workspace"){body.innerHTML=workspaceHTML();bindWorkspace();return}

  if(state.view==="dashboard"){
    const rounds=buildRounds(M.events);
    const cost=rounds.reduce((a,r)=>a+r.cost,0),tokens=rounds.reduce((a,r)=>a+r.tokens,0);
    const running=M.jobs.filter(j=>j.status==="running");
    const sev={critical:0,high:0,medium:0,low:0};M.findings.forEach(f=>{if(sev[f.severity]!=null)sev[f.severity]++});
    const sevColor={critical:"bad",high:"high",medium:"warn",low:"fg3"};
    const usage=toolUsage();
    const top=Object.entries(usage).sort((a,b)=>b[1]-a[1]).slice(0,5);
    // 资产类型分布（ARTEX dashboard 条形图语义：类型→数量，横向条形可点下钻）
    const assetTypes={};M.assets.forEach(a=>{assetTypes[a.type]=(assetTypes[a.type]||0)+1});
    const assetRows=Object.entries(assetTypes).sort((a,b)=>b[1]-a[1]).slice(0,8);
    const assetMax=Math.max(1,...assetRows.map(r=>r[1]));
    const days=[];
    for(let i=6;i>=0;i--){
      const d=new Date(Date.now()-i*86400000);
      const day0=new Date(d);day0.setHours(0,0,0,0);
      const day1=new Date(day0.getTime()+86400000);
      const js=M.jobs.filter(j=>(j.created_at||0)>=day0.getTime()/1000&&(j.created_at||0)<day1.getTime()/1000);
      days.push([i===0?"今日":`${d.getMonth()+1}/${d.getDate()}`,js.length,js.filter(x=>x.status==="failed"||x.status==="error").length]);
    }
    const dmax=Math.max(1,...days.map(d=>d[1]));
    body.innerHTML=`<div class="view">
      <h2>总览</h2><div class="view-sub">全局态势 · 卡片与图例可点击下钻</div>
      <div class="statgrid">
        <div class="stat link" data-go="workspace"><div class="v" style="color:var(--ok)">${running.length}<span class="faint" style="font-size:14px"> / ${M.jobs.length}</span></div><div class="k">运行中 / 全部任务</div></div>
        <div class="stat link" data-go="findings"><div class="v" style="color:var(--flag)">${M.findings.length}</div><div class="k">累计发现 · ${sev.critical} critical</div></div>
        <div class="stat"><div class="v">${fmtCny(cost)}</div><div class="k">当前任务成本 · ${fmtTok(tokens)} tok</div></div>
        <div class="stat link" data-go="experience"><div class="v">${M.experiences.length}</div><div class="k">沉淀经验</div></div>
      </div>
      <div class="dash2">
        <div class="card"><div class="card-t">资产类型分布 <span class="faint" style="font-size:11px">· 点击条目下钻到资产视图</span></div>
          ${assetRows.map(([t,n])=>`<div class="arow" data-at="${esc(t)}"><span class="aname">${esc(t)}</span><div class="abar"><i style="width:${n/assetMax*100}%"></i></div><span class="mono faint">${n}</span></div>`).join("")||'<div class="faint" style="font-size:11.5px">暂无资产</div>'}
          <div class="card-t" style="margin-top:14px">发现严重度 <span class="faint" style="font-size:11px">· 点击图例下钻到发现视图</span></div>
          <div class="sevbar">${["critical","high","medium","low"].map(k=>`<i style="width:${sev[k]/(M.findings.length||1)*100}%;background:var(--${sevColor[k]})"></i>`).join("")}</div>
          <div class="legend">${["critical","high","medium","low"].map(k=>`<span class="lg" data-sev="${k}"><i style="background:var(--${sevColor[k]})"></i>${k} ${sev[k]}</span>`).join("")}</div></div>
        <div class="card"><div class="card-t">近 7 日任务</div>
          ${days.map(([d,su,fa])=>`
            <div style="display:flex;align-items:center;gap:12px;margin-bottom:8px;font-size:12px">
              <span class="faint" style="width:34px">${d}</span>
              <div style="flex:1;height:12px;border-radius:3px;background:var(--bg3);overflow:hidden;display:flex">
                <i style="width:${su/dmax*100}%;background:var(--ok);opacity:.55"></i>
                <i style="width:${fa/dmax*100}%;background:var(--bad);opacity:.55"></i></div>
              <span class="mono faint" style="width:64px;text-align:right">${su}✓ ${fa}✗</span></div>`).join("")}
          <div class="legend"><span><i style="background:var(--ok);opacity:.55"></i>任务</span><span><i style="background:var(--bad);opacity:.55"></i>失败</span></div></div>
        <div class="card"><div class="card-t">工具调用 TOP 5 · 当前任务</div>
          ${top.map(([t,n])=>`<div style="display:flex;gap:12px;align-items:center;padding:4px 0;font-size:12.5px"><span class="mono" style="width:120px;flex:none;overflow:hidden;text-overflow:ellipsis">${esc(t)}</span><div style="flex:1;height:3px;border-radius:2px;background:var(--bg3)"><i style="display:block;height:100%;width:${n/top[0][1]*100}%;background:var(--info);border-radius:2px"></i></div><span class="mono faint">${n}</span></div>`).join("")||'<div class="faint" style="font-size:11.5px">暂无调用</div>'}</div>
        <div class="card"><div class="card-t">运行中任务</div>
          ${running.map(j=>`<div class="hyp link" data-job="${esc(j.job_id)}"><div class="h-top"><span class="dot running"></span><span class="mono" style="font-size:11.5px">${esc(j.job_id)}</span></div><div class="h-title">${esc((j.objective||"").slice(0,50))}</div></div>`).join("")||'<div class="faint" style="font-size:11.5px">暂无运行中任务</div>'}</div>
        <div class="card"><div class="card-t">最新发现 <span class="faint" style="font-size:11px">· 点击查看详情</span></div>
          ${M.findings.slice(0,5).map(f=>`<div class="hyp link" data-fid="${esc(f.id)}"><div class="h-top"><span class="badge b-${f.severity}">${f.severity}</span><span class="conf">${((f.confidence||0)*100).toFixed(0)}%</span></div><div class="h-title">${esc(f.title)}</div></div>`).join("")||'<div class="faint" style="font-size:11.5px">暂无发现</div>'}</div>
        <div class="card"><div class="card-t">最新经验 <span class="faint" style="font-size:11px">· 点击查看全文</span></div>
          ${M.experiences.slice(0,3).map(x=>`<div class="hyp link" data-eid="${esc(x.id)}"><div class="h-top"><span class="badge b-type">${esc(x.technique||"generic")}</span><span class="conf">${x.hits}/${x.injections}</span></div><div class="h-title">${esc(x.title)}</div></div>`).join("")||'<div class="faint" style="font-size:11.5px">暂无经验</div>'}</div>
      </div></div>`;
    $$("[data-go]",body).forEach(el=>el.onclick=()=>setView(el.dataset.go));
    $$("[data-at]",body).forEach(el=>el.onclick=()=>{
      state.assetTypes=[el.dataset.at];state.assetQ="";setView("assets");
    });
    $$("[data-sev]",body).forEach(el=>el.onclick=()=>{state.findSev=[el.dataset.sev];state.findQ="";setView("findings")});
    $$("[data-job]",body).forEach(el=>el.onclick=()=>setTask(el.dataset.job));
    $$("[data-fid]",body).forEach(el=>el.onclick=()=>openFinding(M.findings.find(f=>f.id===el.dataset.fid)));
    $$("[data-eid]",body).forEach(el=>el.onclick=()=>openExperience(M.experiences.find(x=>x.id===el.dataset.eid)));
    return;
  }

  if(state.view==="assets"){
    const types={};M.assets.forEach(a=>types[a.type]=(types[a.type]||0)+1);
    const tlist=Object.entries(types).sort((a,b)=>b[1]-a[1]);
    body.innerHTML=`<div class="view"><h2>资产图谱</h2><div class="view-sub"><span id="a-count"></span> · 点击值复制 · 来源任务可跳转回工作台</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px;align-items:center">
        <button class="chip ${!state.assetTypes.length?"on":""}" data-at="__all">全部</button>
        ${tlist.map(([t,n])=>`<button class="chip ${state.assetTypes.includes(t)?"on":""}" data-at="${esc(t)}">${esc(t)} <span class="mono faint" style="font-size:10px">${n}</span></button>`).join("")}
        <input id="a-search" placeholder="搜索值 / 来源任务…" style="margin-left:auto;width:230px" value="${esc(state.assetQ)}">
      </div>
      <div class="card" style="padding:0;max-height:calc(100vh - 300px);overflow:auto"><table class="tbl"><thead><tr><th style="width:100px">类型</th><th>值</th><th style="width:130px">来源任务</th><th style="width:150px">首次发现</th></tr></thead><tbody id="a-tbody"></tbody></table></div></div>`;
    $$("[data-at]",body).forEach(c=>c.onclick=()=>{
      const t=c.dataset.at;
      if(t==="__all")state.assetTypes=[];
      else state.assetTypes=state.assetTypes.includes(t)?state.assetTypes.filter(x=>x!==t):[...state.assetTypes,t];
      renderBody();
    });
    $("#a-search").oninput=e=>{state.assetQ=e.target.value;renderAssets()};
    renderAssets();
    return;
  }

  if(state.view==="findings"){
    const sevAll=["critical","high","medium","low"];
    const sev={critical:0,high:0,medium:0,low:0};M.findings.forEach(f=>{if(sev[f.severity]!=null)sev[f.severity]++});
    const sevColor={critical:"bad",high:"high",medium:"warn",low:"fg3"};
    body.innerHTML=`<div class="view"><h2>发现 / 风险</h2><div class="view-sub">${M.findings.length} 条 · 点击图例筛选严重度 · 点击行查看详情</div>
      <div class="card" style="padding:13px 16px">
        <div class="sevbar">${sevAll.map(k=>`<i style="width:${sev[k]/(M.findings.length||1)*100}%;background:var(--${sevColor[k]})"></i>`).join("")}</div>
        <div class="legend" style="margin-top:10px">${sevAll.map(k=>`<span class="lg ${state.findSev.includes(k)?"":"off"}" data-sev="${k}"><i style="background:var(--${sevColor[k]})"></i>${k} ${sev[k]}</span>`).join("")}</div></div>
      <div style="display:flex;gap:10px;margin-bottom:14px;align-items:center">
        <input id="f-search" placeholder="搜索标题 / CWE / 目标…" style="flex:1;max-width:340px" value="${esc(state.findQ)}">
        <span class="faint mono" style="font-size:11.5px" id="f-count"></span></div>
      <div id="f-list"></div></div>`;
    $$("[data-sev]",body).forEach(l=>l.onclick=()=>{
      const k=l.dataset.sev;
      state.findSev=state.findSev.includes(k)?state.findSev.filter(x=>x!==k):[...state.findSev,k];
      if(!state.findSev.length)state.findSev=[...sevAll];
      renderBody();
    });
    $("#f-search").oninput=e=>{state.findQ=e.target.value;renderFindings()};
    renderFindings();
    return;
  }

  if(state.view==="tools"){
    const usage=toolUsage();
    const groups=[["内置工具",M.tools.builtin],["插件工具",M.tools.plugins],["打法技能",M.tools.skills]];
    const q=(state.toolQ||"").toLowerCase();
    const row=(t)=>{const n=usage[t.name]||0;
      if(q&&!((t.name+" "+(t.description||"")+" "+(t.summary||"")).toLowerCase().includes(q)))return"";
      return `<div class="trow2" data-tool="${esc(t.name)}"><span class="tname2">${esc(t.name)}</span>
        <span class="tdesc">${esc(((t.description||t.summary)||"(无描述)").slice(0,86))}</span>
        ${n?`<span class="badge b-type mono" style="flex:none">${n} 次</span>`:`<span class="faint mono" style="font-size:10.5px;flex:none">未调用</span>`}</div>`};
    body.innerHTML=`<div class="view"><h2>工具与技能</h2><div class="view-sub">调用次数来自当前任务事件流 · 点击工具查看它的调用记录（参数与响应）</div>
      <input id="t-search" placeholder="搜索工具 / 技能…" style="width:280px;margin-bottom:16px" value="${esc(state.toolQ)}">
      ${groups.map(([title,arr])=>`<div class="card"><div class="card-t">${title} <span class="faint mono" style="font-size:11px">${arr.length}</span></div>${arr.map(row).join("")}</div>`).join("")}</div>`;
    $("#t-search").oninput=e=>{state.toolQ=e.target.value;renderBody();$("#t-search").focus()};
    $$("[data-tool]",body).forEach(el=>el.onclick=()=>openTool(el.dataset.tool));
    return;
  }

  if(state.view==="experience"){
    const techs=[...new Set(M.experiences.map(x=>x.technique).filter(Boolean))];
    body.innerHTML=`<div class="view"><h2>沉淀经验</h2><div class="view-sub">${M.experiences.length} 条 · 命中率 = 命中 / 注入 · 点击卡片查看全文与踩坑记录</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px;align-items:center">
        <button class="chip ${!state.expTech?"on":""}" data-et="__all">全部</button>
        ${techs.map(t=>`<button class="chip ${state.expTech===t?"on":""}" data-et="${esc(t)}">${esc(t)}</button>`).join("")}
        <input id="e-search" placeholder="搜索经验…" style="margin-left:auto;width:220px" value="${esc(state.expQ)}"></div>
      <div id="e-list"></div></div>`;
    $$("[data-et]",body).forEach(c=>c.onclick=()=>{state.expTech=c.dataset.et==="__all"?"":c.dataset.et;renderBody()});
    $("#e-search").oninput=e=>{state.expQ=e.target.value;renderExperience()};
    renderExperience();
    return;
  }

  if(state.view==="config"){
    const cfg=M.config.effective;
    const L={model_primary:"主模型",model_fallback:"备模型",logistics_workers:"后勤并发数",orch_commander:"Commander 编排",orch_delivery:"交付门",orch_logistics:"后勤编排",role_templates:"角色模板 legacy",mentor:"导师模型纠偏",delegate:"delegate 子智能体",tool_pool:"备选工具池"};
    const groups=[["模型",["model_primary","model_fallback"]],["编排",["orch_commander","orch_delivery","orch_logistics","role_templates","mentor","delegate","tool_pool"]],["后勤",["logistics_workers"]]];
    body.innerHTML=`<div class="view" style="max-width:720px"><h2>配置</h2>
      <div class="view-sub">运行时覆盖 · 优先级低于 shell export、高于内置默认、不改 .env · <span id="cfg-mod" style="color:var(--warn)"></span></div>
      ${groups.map(([g,keys])=>`<div class="card"><div class="card-t">${g}</div>${keys.map(k=>`
        <div style="margin-bottom:13px"><label class="faint" style="font:10.5px var(--mono);display:block;margin-bottom:5px;letter-spacing:.06em;text-transform:uppercase">${L[k]||k} · ${k}</label>
        <input data-cfg="${k}" data-def="${esc(M.config.saved[k]||"")}" value="${esc(cfg[k]||"")}" placeholder="生效值：${esc(M.config.saved[k]||"(内置默认)")}" style="width:100%"></div>`).join("")}</div>`).join("")}
      <div style="display:flex;gap:10px"><button class="btn primary" id="cfg-save">保存配置</button><button class="btn" id="cfg-reset">恢复上次保存</button></div></div>`;
    const inputs=$$("[data-cfg]",body);
    const upd=()=>{
      const mod=inputs.filter(i=>i.value!==i.dataset.def);
      $("#cfg-mod").textContent=mod.length?`已修改 ${mod.length} 项（未保存）`:"";
      inputs.forEach(i=>i.style.borderColor=mod.includes(i)?"var(--warn)":"");
    };
    inputs.forEach(i=>i.addEventListener("input",upd));
    $("#cfg-save").onclick=async()=>{
      const w=inputs.find(i=>i.dataset.cfg==="logistics_workers");
      if(w&&w.value!==""&&isNaN(Number(w.value))){w.style.borderColor="var(--bad)";toast("后勤并发数必须是数字");return}
      try{
        const cfg={};inputs.forEach(i=>{if(i.value.trim()!=="")cfg[i.dataset.cfg]=i.value.trim()});
        M.config=await api("/api/config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(cfg)});
        toast("已保存并即时生效（新任务起）");
        inputs.forEach(i=>i.dataset.def=i.value);upd();
      }catch(e){toast(`保存失败：${e.message}`)}
    };
    $("#cfg-reset").onclick=()=>{inputs.forEach(i=>i.value=i.dataset.def);upd();toast("已恢复为上次保存值")};
    upd();
    return;
  }
}

/* ── 资产渲染 ── */
function renderAssets(){
  const el=$("#a-tbody");if(!el)return;
  const q=(state.assetQ||"").toLowerCase(),on=state.assetTypes;
  const rows=M.assets.filter(a=>(!on.length||on.includes(a.type))&&(!q||(a.value+" "+(a.first_seen_job||"")).toLowerCase().includes(q)));
  $("#a-count").textContent=`${rows.length} 条资产（库中共 ${M.assets.length}）`;
  // 分页（ARTEX table-pagination 语义）：每页 50，上一页/下一页 + 页码
  const page=Math.max(1,state.assetPage||1),per=50;
  const pages=Math.max(1,Math.ceil(rows.length/per));
  const pg=Math.min(page,pages);
  state.assetPage=pg;
  const view=rows.slice((pg-1)*per,pg*per);
  const typeColor={endpoint:"info",root_domain:"agent",domain:"agent",app:"okwarn",ip:"time",port:"time"};
  el.innerHTML=view.map(a=>`<tr data-val="${esc(a.value)}">
    <td><span class="badge b-${typeColor[a.type]||"type"}">${esc(a.type)}</span></td>
    <td class="mono" style="font-size:12px;max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(a.value)}">${esc(a.value)}</td>
    <td><span class="mono faint" style="font-size:11.5px">${esc(a.first_seen_job||"—")}</span></td>
    <td class="faint" style="font-size:11.5px">${a.ts?new Date(a.ts*1000).toLocaleString("zh-CN",{hour12:false}):"—"}</td></tr>`).join("")
    +(pages>1?`<tr><td colspan="4" style="text-align:center;padding:10px 0">
      <button class="chip" data-apg="${pg-1}" ${pg<=1?"disabled":""}>‹ 上一页</button>
      <span class="mono faint" style="margin:0 12px;font-size:11.5px">第 ${pg} / ${pages} 页 · 共 ${rows.length} 条</span>
      <button class="chip" data-apg="${pg+1}" ${pg>=pages?"disabled":""}>下一页 ›</button></td></tr>`
    :`<tr><td colspan="4" class="faint" style="text-align:center;font-size:11px">共 ${rows.length} 条</td></tr>`);
  $$("[data-apg]",el).forEach(b=>b.onclick=()=>{state.assetPage=+b.dataset.apg;renderAssets()});
  $$("#a-tbody tr[data-val]").forEach(tr=>tr.onclick=()=>{
    copyText(tr.dataset.val,"资产值已复制");
  });
}

/* ── 发现渲染 ── */
function renderFindings(){
  const el=$("#f-list");if(!el)return;
  const q=(state.findQ||"").toLowerCase();
  const order={critical:0,high:1,medium:2,low:3};
  const list=M.findings.filter(f=>state.findSev.includes(f.severity)&&(!q||(f.title+" "+f.target+" "+(f.cwe||"")).toLowerCase().includes(q)))
    .sort((a,b)=>(order[a.severity]??9)-(order[b.severity]??9));
  $("#f-count").textContent=`${list.length} 条匹配`;
  el.innerHTML=list.map(f=>`<div class="row-find" data-id="${esc(f.id)}">
    <div class="sevline" style="background:var(--${{critical:"bad",high:"high",medium:"warn",low:"fg3"}[f.severity]||"fg3"})"></div>
    <div style="flex:1;min-width:0"><div class="t">${esc(f.title)}</div><div class="m">${f.cwe?esc(f.cwe)+" · ":""}${esc(f.target)} · ${esc(f.job_id)}</div></div>
    <span class="badge b-${f.severity}">${f.severity}</span></div>`).join("")
    ||`<div class="empty"><b>◌</b>无匹配发现 · 可点击图例放宽严重度筛选</div>`;
  $$("[data-id]",el).forEach(r=>r.onclick=()=>openFinding(M.findings.find(x=>x.id===r.dataset.id)));
}

/* ── 经验渲染 ── */
function renderExperience(){
  const el=$("#e-list");if(!el)return;
  const q=(state.expQ||"").toLowerCase();
  const list=M.experiences.filter(x=>(!state.expTech||x.technique===state.expTech)&&(!q||(x.title+" "+x.what_worked+" "+x.technique).toLowerCase().includes(q)));
  el.innerHTML=list.map(x=>`<div class="card link" data-eid="${esc(x.id)}">
    <div style="display:flex;gap:10px;align-items:center"><b style="font-size:13.5px">${esc(x.title)}</b><span class="badge b-type" style="margin-left:auto">${esc(x.technique||"generic")}</span></div>
    <div class="muted" style="font-size:12.5px;margin:8px 0;line-height:1.65">${esc(x.what_worked)}</div>
    <div style="display:flex;gap:10px;align-items:center;font-size:11.5px"><span class="faint">命中率</span>
      <div style="flex:1;height:3px;border-radius:2px;background:var(--bg3)"><i style="display:block;height:100%;width:${x.hits/Math.max(1,x.injections)*100}%;background:var(--ok);border-radius:2px"></i></div>
      <span class="mono faint">${x.hits}/${x.injections}</span></div></div>`).join("")
    ||`<div class="empty"><b>◌</b>无匹配经验</div>`;
  $$("[data-eid]",el).forEach(c=>c.onclick=()=>openExperience(M.experiences.find(x=>x.id===c.dataset.eid)));
}

/* ── 详情抽屉 ── */
function openTool(name){
  const t=[...M.tools.builtin,...M.tools.plugins,...M.tools.skills].find(x=>x.name===name);
  if(!t)return;
  const usage=toolUsage()[name]||0;
  const results=new Map(M.events.filter(e=>e.type==="tool_result").map(e=>[e.payload.call_id,e]));
  const recs=M.events.filter(e=>e.type==="tool_call"&&e.payload.tool===name).slice(-3)
    .map(c=>{const r=results.get(c.payload.call_id);
      return `── ${fmtT(c.ts)} · ${r?r.payload.status:"运行中"}${r&&r.payload.bytes?" · "+r.payload.bytes+"B":""} ──\nARGS: ${tryFmt(typeof c.payload.args==="string"?c.payload.args:JSON.stringify(c.payload.args))}\n${r?"OUTPUT: "+String(r.payload.preview||"(空)").slice(0,300):"(运行中)"}`})
    .join("\n\n");
  openDetail(name,`${t.kind||"skill"}${t.source?" · "+t.source:""} · 当前任务调用 ${usage} 次`,
    ((t.description||t.summary)||"(无描述)")+(recs?"\n\n【最近调用记录】\n\n"+recs:"\n\n（当前任务暂无调用记录）"));
}
function openExperience(x){
  if(!x)return;
  openDetail(x.title,`${x.technique||"generic"} · 命中 ${x.hits}/${x.injections}`,
    x.what_worked+"\n\n【pitfalls · 踩坑】\n- "+((x.pitfalls||[]).join("\n- ")||"（无）")+"\n\n【触发指标】"+(x.indicators||[]).join("、"),{md:true});
}
function openFinding(f){
  if(!f)return;
  openDetail(f.title,`${f.id} · ${f.severity} · ${f.status} · 置信度 ${((f.confidence||0)*100).toFixed(0)}%`,
    `${f.description||"(无描述)"}\n\n—— 目标：${f.target}\n来源任务：${f.job_id}${f.cwe?"\nCWE："+f.cwe:""}`,{md:true});
  /* 流量证据查看器（ARTEX traffic-evidence-viewer 语义）：绑定的 HTTP 交换
     以 request/response 原文折叠呈现（role 标注 baseline/proof/…） */
  api(`/api/findings/${encodeURIComponent(f.id)}/evidence`).then(d=>{
    if(!d||!d.ok||!(d.evidence||[]).length)return;
    const body=$("#fd-body");
    if(!body||!body.classList.contains("rich"))return;
    const wrap=document.createElement("div");
    wrap.className="ev-wrap";
    wrap.innerHTML=`<div class="ev-cap">流量证据 · ${d.evidence.length} 条</div>`+
      d.evidence.map((ev,i)=>`<details class="ev-item"${ev.role==="proof"?" open":""}>
        <summary><span class="ev-role r-${esc(ev.role)}">${esc(ev.role)}</span>
          <span class="ev-url">${esc((ev.method||"")+" "+(ev.url||ev.raw?.slice(0,60)||""))}</span></summary>
        ${ev.request?`<pre class="ev-req">${esc(ev.request)}</pre>`:""}
        ${ev.response?`<pre class="ev-res">${esc(ev.response)}</pre>`:""}
        ${!ev.request&&!ev.response&&ev.raw?`<pre class="ev-req">${esc(ev.raw)}</pre>`:""}
      </details>`).join("");
    body.appendChild(wrap);
  }).catch(()=>{});
}
function openDetail(title,meta,bodyText,{md=false}={}){
  $("#fd-title").textContent=title;$("#fd-meta").textContent=meta;
  const body=$("#fd-body");
  if(md){body.classList.add("rich");body.innerHTML=mdRender(bodyText)}
  else{body.classList.remove("rich");body.textContent=bodyText}
  $("#fd-exp").style.display="none";
  $("#fdetail").classList.add("show");$("#scrim").classList.add("show");
}

/* ── 命令面板 ── */
function renderPalette(q){
  const acts=[
    ...navCounts().map(([id,,l])=>({t:"视图",label:l,run:()=>setView(id)})),
    {t:"动作",label:"下达新任务",run:()=>{setView("workspace");state.mode="new";state.newTaskMode=true;renderBody();$("#c-in").focus()}},
    {t:"动作",label:"切换主题",run:toggleTheme},
    ...M.jobs.slice(0,8).map(j=>({t:"任务",label:j.job_id+" · "+(j.objective||"").slice(0,26).replace(/\s+/g," "),run:()=>setTask(j.job_id)})),
  ].filter(a=>!q||a.label.toLowerCase().includes(q.toLowerCase()));
  $("#p-list").innerHTML=acts.slice(0,12).map((a,i)=>`<div class="p-item ${i===0?"sel":""}" data-i="${i}"><span class="faint" style="font:10px var(--mono);width:34px">${a.t}</span>${esc(a.label)}</div>`).join("")||`<div class="empty" style="padding:14px">无匹配</div>`;
  $$("#p-list .p-item").forEach(el=>el.onclick=()=>{acts[+el.dataset.i].run();togglePalette(false)});
}
function togglePalette(show){$("#palette").classList.toggle("show",show);if(show){$("#p-input").value="";renderPalette("");$("#p-input").focus()}else $("#scrim").classList.remove("show")}
function toggleTheme(){
  const cur=document.documentElement.dataset.theme==="light"?"dark":"light";
  document.documentElement.dataset.theme=cur;localStorage.setItem("secpt-theme",cur);
}
let toastTimer;
function toast(msg){const t=$("#toast");t.textContent=msg;t.classList.add("show");clearTimeout(toastTimer);toastTimer=setTimeout(()=>t.classList.remove("show"),2200)}

/* ── 全局绑定 ── */
$("#btn-theme").onclick=toggleTheme;
$("#btn-k").onclick=()=>togglePalette(true);
$("#scrim").onclick=()=>{$("#fdetail").classList.remove("show");togglePalette(false);closeDock()};
$("#p-input").oninput=e=>renderPalette(e.target.value);
$("#fd-exp").onclick=()=>toast("经验由任务结束时自动蒸馏（learning 闭环）");
document.addEventListener("keydown",e=>{
  if(e.key==="Escape"&&state.newTaskMode&&state.view==="workspace"){state.newTaskMode=false;renderBody()}
  if(e.key==="Escape"){$("#fdetail").classList.remove("show");togglePalette(false);closeDock()}
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==="k"){e.preventDefault();togglePalette(!$("#palette").classList.contains("show"))}
});
/* 规格要求暗色面板：默认 dark（仍可用右上角 ◐ 切到 light 并记忆） */
document.documentElement.dataset.theme=localStorage.getItem("secpt-theme")||"dark";

/* ── 启动 + 轮询 ── */
async function refreshJobData(full){
  try{
    await Promise.all([loadJobs(),loadWorkspace(),loadConfig()]);
    if(full)await loadEvents(true);
    if(state.view==="workspace")renderBody();
    renderDock();
  }catch(e){/* 静默重试 */}
}
async function boot(){
  await Promise.all([loadJobs(),loadAssets(),loadFindings(),loadTools(),loadExperience(),loadConfig()]);
  if(state.jobId)try{await loadEvents(true)}catch(e){}
  await loadWorkspace();
  bindDockEvents();
  renderNav();setView("workspace");renderDock();
  if(state.jobId)streamStart(state.jobId);
  setInterval(async()=>{try{
    if(stream.live&&stream.jobId===state.jobId){syncSendBtn();return}  // 流式在线：事件即时达，跳过轮询
    await loadEvents(false);
    if(state.view==="workspace"){renderFeed();renderTaskPanel()}
    syncSendBtn();renderDock()}catch(e){}},1500);
  setInterval(async()=>{try{
    /* 坞收起且任务已收口时不再轮询 workspace：作战地图/成果是静态终态，
       轮询既无收益也会造成无谓刷新；运行中/待确认/坞展开时才保持 4s 更新 */
    const needWs=viewRunning()||curJob().status==="awaiting_confirm"||dockState.open.size>0;
    const ps=[loadJobs(),loadFindings(),loadExperience()];
    if(needWs)ps.push(loadWorkspace());
    await Promise.all(ps);
    if(state.view==="workspace")renderTaskPanel();
    renderNav();renderDock()}catch(e){}},4000);
  setInterval(async()=>{try{await loadAssets();await loadConfig();
    if(state.view==="assets")renderAssets();renderNav()}catch(e){}},8000);
}
boot();
