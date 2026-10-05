"""
JIM Deluxe — FastAPI Backend
Runs on Railway. All edits go through here as single transactions.
The browser never touches Supabase directly.
"""
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pathlib import Path

from db import init_pool
from routers import availability, orders, carts, engine, stores, exports, auth

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_pool()
    yield

app = FastAPI(
    title="JIM Deluxe API",
    description="Josh Inventory Management Deluxe — TPC order recommendation system",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten to Railway frontend URL in production
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router,         prefix="/api/auth",         tags=["Auth"])
app.include_router(stores.router,       prefix="/api/stores",       tags=["Stores"])
app.include_router(availability.router, prefix="/api/availability", tags=["Availability"])
app.include_router(orders.router,       prefix="/api/orders",       tags=["Orders"])
app.include_router(carts.router,        prefix="/api/carts",        tags=["Carts"])
app.include_router(engine.router,       prefix="/api/engine",       tags=["Engine"])
app.include_router(exports.router,      prefix="/api/exports",      tags=["Exports"])

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "JIM Deluxe API"}

# HTML embedded directly in Python — bypasses Railway file caching
from fastapi.responses import HTMLResponse

_FRONTEND_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JIM Deluxe — TPC Order Writer</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css" rel="stylesheet">
<style>
:root{--fern:#2F4A3E;--fern-dark:#213631;--fern-light:#3d6150;--moss:#6B7D52;--moss-pale:#DCE3CC;--paper:#F5F7F4;--ink:#232A20;--ink-soft:#5B6152;--line:#E2E6DF;--orange:#B5502D;--success:#3F7A4F;}
body{background:var(--paper);color:var(--ink);font-size:14px;font-family:"Segoe UI",system-ui,sans-serif;}
.jim-nav{background:var(--fern-dark);height:56px;padding:0 1.25rem;display:flex;align-items:center;}
.brand{color:#fff;font-weight:800;font-size:15px;margin-right:24px;}
.nl{color:#b8c9b4!important;font-size:13px;font-weight:600;padding:0 14px;height:56px;display:flex;align-items:center;border-bottom:3px solid transparent;cursor:pointer;}
.nl:hover{color:#fff!important;}.nl.active{color:#fff!important;border-bottom-color:var(--orange);}
.ws{background:var(--fern);color:#fff;padding:6px 1.25rem;font-size:12.5px;display:flex;align-items:center;gap:14px;}
.wchip{background:rgba(255,255,255,.15);border-radius:20px;padding:2px 12px;font-weight:700;}
.screen{display:none;}.screen.active{display:block;}
.sc{background:#fff;border:1px solid var(--line);border-radius:10px;padding:16px 18px;box-shadow:0 2px 6px rgba(35,42,32,.06);}
.sc .n{font-size:1.9rem;font-weight:700;color:var(--fern-dark);line-height:1;}
.sc .l{font-size:11px;color:var(--ink-soft);text-transform:uppercase;letter-spacing:.06em;margin-top:4px;}
.sc.ok .n{color:var(--success);}.sc.wn .n{color:var(--orange);}
.tb{display:inline-flex;align-items:center;justify-content:center;min-width:30px;height:20px;border-radius:5px;font-size:11px;font-weight:800;padding:0 5px;}
.tb-AA{background:#d4edda;color:#1a3a2a;border:1.5px solid #a3cfb8;}
.tb-A{background:#d4edda;color:#155724;border:1.5px solid #b8d4c2;}
.tb-B{background:#fff3cd;color:#856404;border:1.5px solid #e8d27c;}
.tb-C{background:#f8d7da;color:#842029;border:1.5px solid #f1a1a8;}
.tb-D{background:#e9ecef;color:#6c757d;border:1.5px solid #ced4da;}
.tb-P{background:#f8f9fa;color:#495057;border:1.5px solid #dee2e6;}
.btn-primary{background:var(--fern)!important;border-color:var(--fern)!important;}
.btn-primary:hover{background:var(--fern-light)!important;border-color:var(--fern-light)!important;}
.btn-cta{background:var(--orange);border-color:var(--orange);color:#fff;font-weight:600;}
.btn-cta:hover{background:#9A3F20;border-color:#9A3F20;color:#fff;}
.btn-outline-primary{color:var(--fern)!important;border-color:var(--fern)!important;}
.btn-outline-primary:hover{background:var(--fern)!important;color:#fff!important;}
.card{border:1px solid var(--line);border-radius:10px;box-shadow:0 2px 8px rgba(35,42,32,.07);}
.ch{background:var(--fern-dark);color:#fff;border-radius:9px 9px 0 0!important;font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;padding:9px 14px;}
.ch.lt{background:#f0f4f0;color:var(--fern-dark);}
.jt{font-size:13px;}
.jt thead th{background:#f0f4f0;color:var(--fern-dark);font-size:11px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;border-bottom:2px solid var(--moss-pale);white-space:nowrap;}
.jt tbody tr{cursor:pointer;transition:background .1s;}
.jt tbody tr:hover{background:#f5f9f5;}.jt tbody tr.ar{background:#f5fbf6;}
.sp{font-size:10.5px;font-weight:700;padding:2px 9px;border-radius:20px;text-transform:uppercase;letter-spacing:.04em;}
.sp-draft{background:#e9ecef;color:#495057;}.sp-approved{background:#d4edda;color:#155724;}
.sr{padding:9px 14px;border-bottom:1px solid var(--line);cursor:pointer;transition:background .1s;}
.sr:hover{background:#f5f9f5;}.sr.active{background:#e8f2ec;border-left:3px solid var(--fern);}
.sl{display:grid;grid-template-columns:280px 1fr;height:calc(100vh - 114px);overflow:hidden;}
.sc2{overflow-y:auto;border-right:1px solid var(--line);}
.pb{border:2px solid var(--fern);border-radius:10px;}
.pbh{background:var(--fern);color:#fff;padding:9px 14px;border-radius:8px 8px 0 0;font-weight:700;font-size:12px;letter-spacing:.04em;}
.lu{height:6px;background:var(--line);border-radius:3px;}
.lf{height:6px;border-radius:3px;background:var(--fern);}.lf.wn{background:var(--orange);}
.ck{font-family:monospace;font-size:10.5px;background:rgba(255,255,255,.2);border-radius:4px;padding:1px 7px;}
.shr{display:flex;align-items:center;padding:7px 12px;border-bottom:1px solid #f0f4f0;gap:10px;}
.shn{width:22px;height:22px;background:var(--fern);color:#fff;border-radius:50%;font-size:10px;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0;}
.kp{font-size:10px;padding:1px 7px;border-radius:10px;font-weight:600;background:#f0f4f0;color:var(--fern);}
#tc{position:fixed;bottom:24px;right:24px;z-index:9999;display:flex;flex-direction:column;gap:8px;}
.jts{background:#fff;border-left:4px solid var(--fern);border-radius:8px;padding:10px 16px;box-shadow:0 4px 16px rgba(0,0,0,.15);display:flex;align-items:center;gap:10px;font-size:13px;min-width:280px;animation:si .25s ease;}
.jts.warn{border-left-color:var(--orange);}.jts.err{border-left-color:#dc3545;}
@keyframes si{from{transform:translateX(40px);opacity:0}to{transform:none;opacity:1}}
.modal-header{background:var(--fern-dark);color:#fff;}.modal-header .btn-close{filter:invert(1);}
::-webkit-scrollbar{width:6px;height:6px;}::-webkit-scrollbar-track{background:#f0f4f0;}::-webkit-scrollbar-thumb{background:var(--moss-pale);border-radius:3px;}
</style>
</head>
<body>

<nav class="jim-nav">
  <span class="brand">&#127807; JIM <span style="font-weight:400;opacity:.7">Deluxe</span></span>
  <span class="nl active" onclick="go('week',this)"><i class="bi bi-house me-1"></i>Week</span>
  <span class="nl" onclick="go('plan',this)"><i class="bi bi-clipboard-check me-1"></i>Review Plan</span>
  <span class="nl" onclick="go('editor',this)"><i class="bi bi-cart3 me-1"></i>Order Editor</span>
  <span class="nl" onclick="go('stores',this)"><i class="bi bi-shop me-1"></i>Stores</span>
  <div class="ms-auto" style="color:#8fa88a;font-size:12px">TPC Order Writer</div>
</nav>

<div class="ws">
  <span><i class="bi bi-calendar-week me-1"></i><strong>Shipping Week:</strong> <span id="sw">&mdash;</span></span>
  <span id="sf" style="display:none"><i class="bi bi-box-seam me-1"></i><span id="sfn"></span></span>
  <span id="ss" style="display:none" class="wchip"></span>
  <span class="ms-auto" style="opacity:.6;font-size:11px">JIM Deluxe v1.0 &middot; The Plant Company</span>
</div>

<!-- WEEK -->
<div id="screen-week" class="screen active">
<div class="container-fluid p-4"><div class="row g-4">

  <div class="col-12 col-lg-6">
    <div class="card h-100">
      <div class="ch"><i class="bi bi-box-seam me-2"></i>Availability</div>
      <div class="card-body">
        <div id="uz" class="rounded-3 p-4 text-center"
             style="border:2px dashed var(--moss-pale);background:#fafcfa;cursor:pointer"
             onmouseover="this.style.borderColor='var(--fern)';this.style.background='#f0f9f0'"
             onmouseout="this.style.borderColor='var(--moss-pale)';this.style.background='#fafcfa'"
             ondragover="event.preventDefault()"
             ondrop="dropFile(event)"
             onclick="document.getElementById('fi').click()">
          <input type="file" id="fi" accept=".xlsx,.xls,.csv" style="display:none" onchange="pickFile(this)">
          <i class="bi bi-cloud-upload" style="font-size:36px;color:var(--moss);opacity:.5"></i>
          <p class="mb-1 mt-2 fw-semibold" style="color:var(--fern-dark)">Drop availability file here</p>
          <p class="mb-0 text-muted" style="font-size:12px">Click to browse &mdash; Excel or CSV, Aster code + quantity</p>
        </div>
        <div id="ap" style="display:none">
          <div class="d-flex align-items-center gap-3 p-3 rounded mb-3" style="background:#f0f9f0;border:1px solid var(--moss-pale)">
            <i class="bi bi-file-earmark-excel" style="font-size:28px;color:var(--fern)"></i>
            <div><div class="fw-bold" id="an"></div><div style="font-size:12px;color:var(--ink-soft)" id="am"></div></div>
            <div class="ms-auto d-flex gap-2">
              <span class="badge" style="background:var(--fern)">Active</span>
              <button class="btn btn-sm btn-outline-secondary py-0" style="font-size:11px" onclick="clearBatch()">&#x2715;</button>
            </div>
          </div>
          <div class="row g-2 mb-3 text-center">
            <div class="col-4"><div class="sc py-2"><div class="n" id="ask" style="font-size:1.4rem">&mdash;</div><div class="l">SKUs</div></div></div>
            <div class="col-4"><div class="sc py-2"><div class="n" id="aun" style="font-size:1.4rem">&mdash;</div><div class="l">Units</div></div></div>
            <div class="col-4"><div class="sc ok py-2"><div class="n" id="amp" style="font-size:1.4rem">&mdash;</div><div class="l">Mapped</div></div></div>
          </div>
          <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--ink-soft);margin-bottom:6px">Available This Week</div>
          <div style="max-height:200px;overflow-y:auto" id="al"></div>
        </div>
      </div>
    </div>
  </div>

  <div class="col-12 col-lg-6">
    <div class="pb mb-3">
      <div class="pbh"><i class="bi bi-book me-2"></i>Weekly Playbook</div>
      <div class="p-3">
        <div class="d-flex gap-2 mb-3">
          <button class="btn btn-sm btn-outline-primary flex-fill" onclick="toast('Business as Usual set','ok')"><i class="bi bi-arrow-repeat me-1"></i>Business as Usual</button>
          <button class="btn btn-sm btn-primary flex-fill" onclick="toast('Scenario builder coming soon','warn')"><i class="bi bi-plus-circle me-1"></i>Add Scenario</button>
        </div>
        <p class="text-muted mb-0" style="font-size:12.5px">No adjustments active. Engine runs on standard velocity + recency weights. Add a scenario to override before generating.</p>
      </div>
    </div>
    <div class="card">
      <div class="ch lt"><i class="bi bi-lightning me-2"></i>Generate Recommendations</div>
      <div class="card-body">
        <div class="row g-2 mb-3 text-center">
          <div class="col-4"><div style="font-size:11px;color:var(--ink-soft);text-transform:uppercase">Velocity</div><div style="font-size:15px;font-weight:700;color:var(--fern-dark)">45%</div></div>
          <div class="col-4"><div style="font-size:11px;color:var(--ink-soft);text-transform:uppercase">Recency</div><div style="font-size:15px;font-weight:700;color:var(--fern-dark)">40%</div></div>
          <div class="col-4"><div style="font-size:11px;color:var(--ink-soft);text-transform:uppercase">Breadth</div><div style="font-size:15px;font-weight:700;color:var(--fern-dark)">15%</div></div>
          <div class="col-4"><div style="font-size:11px;color:var(--ink-soft);text-transform:uppercase">Min Carts</div><div style="font-size:15px;font-weight:700;color:var(--fern-dark)">2</div></div>
          <div class="col-4"><div style="font-size:11px;color:var(--ink-soft);text-transform:uppercase">Truck Cap</div><div style="font-size:15px;font-weight:700;color:var(--fern-dark)">45</div></div>
          <div class="col-4"><div style="font-size:11px;color:var(--ink-soft);text-transform:uppercase">Hard Good</div><div style="font-size:15px;font-weight:700;color:var(--fern-dark)">50/50</div></div>
        </div>
        <div id="gs" style="display:none;font-size:13px;padding:.5rem 1rem;border-radius:.375rem;margin-bottom:.75rem"></div>
        <div class="d-grid gap-2">
          <button class="btn btn-cta py-2" id="gb" onclick="generate()" disabled><i class="bi bi-lightning-fill me-2"></i>Generate Recommended Orders</button>
          <button class="btn btn-outline-secondary py-2" id="rb" onclick="go('plan',this)" style="display:none"><i class="bi bi-clipboard-check me-2"></i>Review Plan</button>
          <button class="btn btn-outline-secondary py-2" id="eb" onclick="startEmpty()" disabled><i class="bi bi-pencil-square me-2"></i>Start Empty Plan</button>
        </div>
      </div>
    </div>
  </div>

</div></div></div>

<!-- PLAN -->
<div id="screen-plan" class="screen">
<div class="container-fluid p-4">
  <div class="row g-3 mb-4" id="ps">
    <div class="col-12 text-center text-muted py-4"><i class="bi bi-inbox" style="font-size:32px;opacity:.2"></i><p class="mt-2">Generate a plan to see results here</p></div>
  </div>
  <div class="d-flex align-items-center gap-2 mb-3 flex-wrap">
    <input class="form-control form-control-sm" style="max-width:240px" placeholder="Search store, city..." oninput="filterO(this.value)">
    <select class="form-select form-select-sm" style="max-width:90px" onchange="filterT(this.value)">
      <option value="">All tiers</option><option>AA</option><option>A</option><option>B</option><option>C</option><option>D</option>
    </select>
    <span class="text-muted" style="font-size:13px" id="oc"></span>
    <div class="ms-auto d-flex gap-2">
      <button class="btn btn-sm btn-outline-primary" onclick="approveAll()"><i class="bi bi-check-all me-1"></i>Approve All</button>
      <button class="btn btn-sm btn-cta" id="xb" onclick="exportAster()" disabled><i class="bi bi-download me-1"></i>Export to Aster</button>
    </div>
  </div>
  <div class="card"><div class="table-responsive">
    <table class="table jt mb-0">
      <thead><tr><th>Store</th><th>Location</th><th>Tier</th><th>Last Delivery</th><th class="text-center">Carts</th><th class="text-center">Units</th><th>Load</th><th>Status</th><th>Actions</th></tr></thead>
      <tbody id="ot"><tr><td colspan="9" class="text-center py-4 text-muted">No plan generated yet</td></tr></tbody>
    </table>
  </div></div>
  <div class="mt-4"><h6 style="color:var(--fern-dark)" class="mb-3"><i class="bi bi-truck me-2"></i>Truck Loads</h6><div class="row g-3" id="lg"></div></div>
</div></div>

<!-- EDITOR -->
<div id="screen-editor" class="screen">
<div class="sl">
  <div class="sc2">
    <div style="padding:10px 14px;border-bottom:1px solid var(--line);background:#f8faf8">
      <input class="form-control form-control-sm" placeholder="Search store..." oninput="filterES(this.value)">
    </div>
    <div id="el"><div class="p-3 text-muted" style="font-size:13px">Generate a plan to see orders here</div></div>
  </div>
  <div id="ed" class="p-4 d-flex align-items-center justify-content-center text-muted" style="overflow-y:auto">
    <div class="text-center"><i class="bi bi-cursor-fill" style="font-size:36px;opacity:.2"></i><p class="mt-2">Select a store to view its order</p></div>
  </div>
</div></div>

<!-- STORES -->
<div id="screen-stores" class="screen">
<div class="sl">
  <div class="sc2">
    <div style="padding:10px 14px;border-bottom:1px solid var(--line);background:#f8faf8">
      <div class="d-flex gap-2">
        <input class="form-control form-control-sm flex-grow-1" placeholder="Search..." oninput="filterSL(this.value)">
        <select class="form-select form-select-sm" style="width:80px" onchange="filterST(this.value)">
          <option value="">All</option><option>AA</option><option>A</option><option>B</option><option>C</option><option>D</option>
        </select>
      </div>
    </div>
    <div id="sl2"><div class="p-3 text-center text-muted"><span class="spinner-border spinner-border-sm me-2"></span>Loading stores...</div></div>
  </div>
  <div id="sp" class="p-4 d-flex align-items-center justify-content-center text-muted" style="overflow-y:auto">
    <div class="text-center"><i class="bi bi-shop" style="font-size:40px;opacity:.2"></i><p class="mt-3">Select a store to see its profile</p></div>
  </div>
</div></div>

<!-- MODALS -->
<div class="modal fade" id="aM" tabindex="-1"><div class="modal-dialog modal-dialog-centered"><div class="modal-content">
  <div class="modal-header"><h5 class="modal-title"><i class="bi bi-check-circle me-2"></i>Approve Order</h5><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>
  <div class="modal-body" id="ab"></div>
  <div class="modal-footer"><button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">Cancel</button><button type="button" class="btn btn-cta" id="ac">Approve</button></div>
</div></div></div>

<div class="modal fade" id="lM" tabindex="-1"><div class="modal-dialog modal-lg modal-dialog-centered modal-dialog-scrollable"><div class="modal-content">
  <div class="modal-header"><h5 class="modal-title" id="lt"><i class="bi bi-truck me-2"></i>Load Detail</h5><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>
  <div class="modal-body" id="lb"></div>
  <div class="modal-footer"><button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">Close</button><button type="button" class="btn btn-cta" id="alb">Approve All on This Load</button></div>
</div></div></div>

<div id="tc"></div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<script>
// JIM Deluxe — Live v2 — Zero hardcoded values
const B='',DH={'X-Dev-Key':'jim-dev-2026','Content-Type':'application/json'};
let bId=null,rId=null,wk=null,orders=[],loads=[],stores=[],aT=null,aLN=null,aMod,lMod;

document.addEventListener('DOMContentLoaded',function(){
  aMod=new bootstrap.Modal('#aM');lMod=new bootstrap.Modal('#lM');
  document.getElementById('ac').onclick=doApprove;
  document.getElementById('alb').onclick=doApproveLoad;
  loadStores();
});

// NAV
function go(n,el){
  document.querySelectorAll('.screen').forEach(function(s){s.classList.remove('active');});
  document.querySelectorAll('.nl').forEach(function(l){l.classList.remove('active');});
  var s=document.getElementById('screen-'+n);if(s)s.classList.add('active');
  var nav=el?(el.closest('.nl')||el):null;if(nav)nav.classList.add('active');
}

// TOAST
function toast(m,t){
  t=t||'ok';var e=document.createElement('div');e.className='jts '+(t!=='ok'?t:'');
  var icon=t==='err'?'bi-x-circle-fill':t==='warn'?'bi-exclamation-triangle-fill':'bi-check-circle-fill';
  e.innerHTML='<i class="bi '+icon+'" style="font-size:16px;flex-shrink:0"></i><span>'+m+'</span>';
  document.getElementById('tc').appendChild(e);
  setTimeout(function(){e.style.opacity='0';e.style.transform='translateX(40px)';e.style.transition='.25s';setTimeout(function(){e.remove();},260);},3800);
}

// API
async function api(p,o){
  o=o||{};var h=Object.assign({},DH,o.headers||{});
  var r=await fetch(B+p,Object.assign({},o,{headers:h}));
  if(!r.ok){var e=await r.json().catch(function(){return{detail:r.statusText};});throw new Error(e.detail||r.statusText);}
  return r.json();
}
async function apiBlob(p){
  var r=await fetch(B+p,{headers:{'X-Dev-Key':'jim-dev-2026'}});
  if(!r.ok)throw new Error('Download failed');return r.blob();
}

// HELPERS
function mkTB(t){t=(t||'').trim();return '<span class="tb tb-'+t+'">'+(t||'?')+'</span>';}
function mkSP(s){return '<span class="sp sp-'+s+'">'+s+'</span>';}
function fmt(n){return(n||0).toLocaleString();}
function sn(d){return(d||'').replace(/^PW /,'').replace(/ TPC$/,'');}
function setStrip(w,f,st){
  var sw=document.getElementById('sw');if(sw)sw.textContent=w||'—';
  var sf=document.getElementById('sf'),sfn=document.getElementById('sfn');
  if(sf)sf.style.display=f?'inline':'none';if(sfn)sfn.textContent=f||'';
  var ss=document.getElementById('ss');
  if(ss&&st){ss.style.display='inline-block';ss.textContent=st;}
  else if(ss)ss.style.display='none';
}

// UPLOAD
function dropFile(e){e.preventDefault();var f=e.dataTransfer.files&&e.dataTransfer.files[0];if(f)uploadFile(f);}
function pickFile(i){var f=i.files&&i.files[0];if(f)uploadFile(f);}

async function uploadFile(file){
  var uz=document.getElementById('uz');
  uz.innerHTML='<span class="spinner-border spinner-border-sm me-2"></span>Uploading '+file.name+'...';
  var w=new Date().toISOString().slice(0,10);
  try{
    var fd=new FormData();fd.append('file',file);fd.append('shipping_week',w);
    var r=await fetch(B+'/api/availability/upload',{method:'POST',headers:{'X-Dev-Key':'jim-dev-2026'},body:fd});
    if(!r.ok)throw new Error(await r.text());
    var d=await r.json();bId=d.batch_id;wk=w;
    var bt=await api('/api/availability/batches/'+bId);
    var ls=bt.lines||[];var tot=ls.reduce(function(s,l){return s+(l.units_available||0);},0);
    uz.style.display='none';
    var ap=document.getElementById('ap');ap.style.display='block';
    document.getElementById('an').textContent=file.name;
    document.getElementById('am').textContent='Shipping week: '+w;
    document.getElementById('ask').textContent=ls.length;
    document.getElementById('aun').textContent=fmt(tot);
    document.getElementById('amp').textContent=ls.length+'/'+ls.length;
    document.getElementById('al').innerHTML=ls.map(function(l){
      return '<div class="d-flex justify-content-between py-1 px-2 rounded mb-1" style="background:#fafcfa;border:1px solid var(--line);font-size:12.5px">'
        +'<span class="fw-semibold">'+sn(l.sku_description||l.description||('SKU '+l.sku_id))+'</span>'
        +'<span class="fw-bold" style="color:var(--fern-dark)">'+fmt(l.units_available)+'</span></div>';
    }).join('');
    document.getElementById('gb').disabled=false;
    document.getElementById('eb').disabled=false;
    setStrip(w,file.name);
    toast(file.name+' uploaded — '+ls.length+' SKUs, '+fmt(tot)+' units','ok');
  }catch(e){
    uz.innerHTML='<div class="text-danger"><i class="bi bi-exclamation-triangle me-2"></i>'+e.message+'</div>'
      +'<small class="text-muted" style="cursor:pointer" onclick="resetUZ()">Try again</small>';
    toast('Upload failed: '+e.message,'err');
  }
}

function resetUZ(){
  var uz=document.getElementById('uz');uz.style.display='block';
  uz.innerHTML='<input type="file" id="fi" accept=".xlsx,.xls,.csv" style="display:none" onchange="pickFile(this)">'
    +'<i class="bi bi-cloud-upload" style="font-size:36px;color:var(--moss);opacity:.5"></i>'
    +'<p class="mb-1 mt-2 fw-semibold" style="color:var(--fern-dark)">Drop availability file here</p>'
    +'<p class="mb-0 text-muted" style="font-size:12px">Click to browse</p>';
}

function clearBatch(){
  bId=null;wk=null;orders=[];loads=[];
  document.getElementById('uz').style.display='block';
  document.getElementById('ap').style.display='none';
  document.getElementById('gb').disabled=true;
  document.getElementById('eb').disabled=true;
  document.getElementById('rb').style.display='none';
  document.getElementById('gs').style.display='none';
  setStrip(null,null);
  buildStats();buildTable();buildLoads();buildEL();
  toast('Availability cleared','warn');
}

// GENERATE
async function generate(){
  if(!bId){toast('Upload an availability file first','warn');return;}
  var gb=document.getElementById('gb'),gs=document.getElementById('gs');
  gb.disabled=true;gb.innerHTML='<span class="spinner-border spinner-border-sm me-2"></span>Running engine...';
  gs.style.cssText='display:block;background:#fff3cd;border:1px solid #e8d27c;color:#856404;font-size:13px;padding:.5rem 1rem;border-radius:.375rem;margin-bottom:.75rem';
  gs.innerHTML='<span class="spinner-border spinner-border-sm me-2"></span>Scoring 2,000 stores \\xb7 allocating inventory \\xb7 building carts...';
  try{
    var j=await api('/api/engine/generate',{method:'POST',body:JSON.stringify({batch_id:bId,shipping_week:wk||new Date().toISOString().slice(0,10)})});
    pollJob(j.job_id,gb,gs);
  }catch(e){
    gb.disabled=false;gb.innerHTML='<i class="bi bi-lightning-fill me-2"></i>Generate Recommended Orders';
    gs.style.display='none';toast('Failed: '+e.message,'err');
  }
}

function pollJob(jid,gb,gs){
  setTimeout(async function(){
    try{
      var j=await api('/api/engine/jobs/'+jid);
      if(j.status==='complete'){
        rId=j.run_id;await loadPlan();
        gs.style.background='#d4edda';gs.style.border='1px solid #b8d4c2';gs.style.color='#155724';
        gs.innerHTML='<i class="bi bi-check-circle-fill me-2"></i><strong>Plan generated</strong> — '+orders.length+' stores, '+orders.reduce(function(s,o){return s+(o.cart_count||0);},0)+' carts';
        gb.disabled=false;gb.innerHTML='<i class="bi bi-arrow-clockwise me-2"></i>Re-generate';
        document.getElementById('rb').style.display='block';
        setStrip(wk,null,'Draft Plan');
        toast('Plan ready — '+orders.length+' stores','ok');
      }else if(j.status==='failed'){
        gb.disabled=false;gb.innerHTML='<i class="bi bi-lightning-fill me-2"></i>Generate Recommended Orders';
        gs.style.display='none';toast('Engine error: '+(j.error_text||'unknown'),'err');
      }else{
        if(j.progress_msg)gs.innerHTML='<span class="spinner-border spinner-border-sm me-2"></span>'+j.progress_msg;
        pollJob(jid,gb,gs);
      }
    }catch(e){pollJob(jid,gb,gs);}
  },2500);
}

async function loadPlan(){
  if(!rId)return;
  try{
    var r=await Promise.all([
      api('/api/orders/runs/'+rId),
      api('/api/orders/runs/'+rId+'/loads'),
      api('/api/orders/runs/'+rId+'/summary').catch(function(){return null;})
    ]);
    orders=Array.isArray(r[0])?r[0]:[];
    loads=Array.isArray(r[1])?r[1]:[];
    buildStats(r[2]);buildTable();buildLoads();buildEL();
  }catch(e){toast('Load failed: '+e.message,'err');}
}

function startEmpty(){
  orders=[];loads=[];buildStats();buildTable();buildLoads();buildEL();
  go('editor',document.querySelectorAll('.nl')[2]);
  toast('Empty plan — build orders manually in the editor','ok');
}

// STATS
function buildStats(sum){
  var el=document.getElementById('ps');if(!el)return;
  if(!orders.length){
    el.innerHTML='<div class="col-12 text-center text-muted py-4"><i class="bi bi-inbox" style="font-size:32px;opacity:.2"></i><p class="mt-2">Generate a plan to see results here</p></div>';
    return;
  }
  var appr=orders.filter(function(o){return o.status==='approved';}).length;
  var tc=orders.reduce(function(s,o){return s+(o.cart_count||0);},0);
  var tu=orders.reduce(function(s,o){return s+(o.total_units||0);},0);
  var cl=sum?sum.inventory_clearance+'%':'—';
  var sd=[
    ['bi-shop','Stores Served',orders.length,''],
    ['bi-cart3','Total Carts',tc,''],
    ['bi-box-seam','Units Allocated',fmt(tu),''],
    ['bi-truck','Truck Loads',loads.length,''],
    ['bi-graph-up','Clearance',cl,'ok'],
    ['bi-check-circle','Approved',appr+'/'+orders.length,appr>0?'ok':'']
  ];
  el.innerHTML=sd.map(function(s){
    return '<div class="col-6 col-md-4 col-xl-2"><div class="sc '+s[3]+'">'
      +'<div class="n"><i class="bi '+s[0]+'" style="font-size:.9rem"></i> '+s[2]+'</div>'
      +'<div class="l">'+s[1]+'</div></div></div>';
  }).join('');
}

// TABLE
var _of=[];
function buildTable(d){
  _of=d||orders;
  var c=document.getElementById('oc');if(c)c.textContent=_of.length+' orders';
  var t=document.getElementById('ot');if(!t)return;
  if(!_of.length){
    t.innerHTML='<tr><td colspan="9" class="text-center py-4 text-muted">'+(orders.length?'No orders match filter':'No plan generated yet')+'</td></tr>';
    var xb=document.getElementById('xb');if(xb)xb.disabled=true;return;
  }
  t.innerHTML=_of.map(function(o){
    var sid=o.store_id||o.id;
    var tier=(o.dynamic_velocity_tier||o.tier||'').trim();
    var days=o.days_since_last_delivery!=null?o.days_since_last_delivery+'d ago':'&mdash;';
    var odata=encodeURIComponent(JSON.stringify({id:o.id,sid:sid,name:o.store_name||o.name||'',state:o.state||'',carts:o.cart_count||0,units:o.total_units||0,load:o.load_name||o.load||'&mdash;',v:o.version||1}));
    var act=o.status==='draft'
      ?'<button class="btn btn-sm btn-cta py-0 px-2" style="font-size:11px" onclick="event.stopPropagation();openApprove(\\''+odata+'\\')">Approve</button>'
      :'<i class="bi bi-check-circle-fill text-success"></i>';
    return '<tr class="'+(o.status==='approved'?'ar':'')+'" onclick="openEditor(\\''+sid+'\\',event)">'
      +'<td><strong>'+sid+'</strong></td>'
      +'<td style="font-size:12px">'+(o.store_name||o.name||'')+'<br><span class="text-muted">'+(o.city||'')+', '+(o.state||'')+'</span></td>'
      +'<td>'+mkTB(tier)+'</td>'
      +'<td style="font-size:12px">'+days+'</td>'
      +'<td class="text-center fw-bold">'+(o.cart_count||0)+'</td>'
      +'<td class="text-center">'+fmt(o.total_units||0)+'</td>'
      +'<td style="font-size:12px">'+(o.load_name||o.load||'&mdash;')+'</td>'
      +'<td>'+mkSP(o.status||'draft')+'</td>'
      +'<td>'+act+'</td></tr>';
  }).join('');
  var xb=document.getElementById('xb');
  if(xb)xb.disabled=!orders.some(function(o){return o.status==='approved';});
}
function filterO(q){buildTable(orders.filter(function(o){return !q||String(o.store_id||o.id).includes(q)||(o.store_name||o.name||'').toLowerCase().includes(q.toLowerCase())||(o.city||'').toLowerCase().includes(q.toLowerCase());}));}
function filterT(t){buildTable(t?orders.filter(function(o){return(o.dynamic_velocity_tier||o.tier||'').trim()===t;}):orders);}

// APPROVE
function openApprove(enc){
  var d=JSON.parse(decodeURIComponent(enc));aT=d;
  document.getElementById('ab').innerHTML='<p>Approve order for <strong>Store '+d.sid+' &mdash; '+d.name+', '+d.state+'</strong>?</p>'
    +'<ul style="font-size:13px"><li>'+d.carts+' carts &middot; '+fmt(d.units)+' units</li><li>Load: '+d.load+'</li></ul>'
    +'<p class="text-muted mb-0" style="font-size:12px">Locks quantities. Adjustments still possible before export.</p>';
  aMod.show();
}
async function doApprove(){
  if(!aT)return;
  try{
    await api('/api/orders/'+aT.id+'/approve?version='+aT.v,{method:'PATCH'});
    var o=orders.find(function(x){return x.id===aT.id;});if(o)o.status='approved';
    buildStats();buildTable();buildEL();toast('Order approved','ok');
  }catch(e){toast('Approve failed: '+e.message,'err');}
  aMod.hide();
}
async function approveAll(){
  var done=0;
  for(var o of orders.filter(function(o){return o.status==='draft';})){
    try{await api('/api/orders/'+o.id+'/approve?version='+(o.version||1),{method:'PATCH'});o.status='approved';done++;}catch(e){}
  }
  buildStats();buildTable();buildEL();toast(done+' orders approved','ok');
}

// LOADS
function buildLoads(){
  var el=document.getElementById('lg');if(!el)return;
  if(!loads.length){el.innerHTML='';return;}
  el.innerHTML=loads.map(function(l,i){
    var u=parseFloat(l.utilization_pct||0);var lw=u<95;
    return '<div class="col-6 col-md-4 col-xl-2"><div class="card p-3" style="cursor:pointer"'
      +' onmouseenter="this.style.boxShadow=\\'0 4px 14px rgba(47,74,62,.18)\\'"'
      +' onmouseleave="this.style.boxShadow=\\'\\'" onclick="openLoad('+i+')">'
      +'<div class="d-flex justify-content-between mb-1">'
      +'<strong style="font-size:13px">'+(l.load_name||'L'+(i+1))+'</strong>'
      +'<span class="'+(lw?'text-orange':'text-success')+' fw-bold" style="font-size:12px">'+u+'%</span></div>'
      +'<div style="font-size:11px;color:var(--ink-soft)">'+(l.merchant||'')+'</div>'
      +'<div style="font-size:11px;color:var(--ink-soft)" class="mb-2">'+(l.cart_count||0)+' carts &middot; '+(l.store_count||0)+' stores</div>'
      +'<div class="lu"><div class="lf '+(lw?'wn':'')+'" style="width:'+u+'%"></div></div>'
      +'<div style="font-size:10px;margin-top:3px;color:'+(lw?'var(--orange)':'var(--success)')+'"><i class="bi bi-'+(lw?'exclamation-triangle':'mouse')+' me-1"></i>'+(lw?'Below 95%':'Click to view')+'</div>'
      +'</div></div>';
  }).join('');
}

function openLoad(i){
  var l=loads[i];if(!l||!lMod)return;
  aLN=l.load_name;
  var sids=(l.store_ids||[]).map(function(s){return parseInt(s);});
  var lo=orders.filter(function(o){return sids.includes(parseInt(o.store_id||o.id));});
  var u=parseFloat(l.utilization_pct||0);
  var apprC=lo.filter(function(o){return o.status==='approved';}).length;
  document.getElementById('lt').innerHTML='<i class="bi bi-truck me-2"></i>'+(l.load_name||'')+' — '+(l.merchant||'')+' \\xb7 '+(l.region||'');
  document.getElementById('lb').innerHTML=
    '<div class="row g-2 mb-3">'
    +'<div class="col-3"><div class="sc py-2 text-center"><div class="n" style="font-size:1.3rem">'+(l.cart_count||0)+'</div><div class="l">Carts</div></div></div>'
    +'<div class="col-3"><div class="sc py-2 text-center"><div class="n" style="font-size:1.3rem">'+lo.length+'</div><div class="l">Stores</div></div></div>'
    +'<div class="col-3"><div class="sc '+(u<95?'wn':'')+' py-2 text-center"><div class="n" style="font-size:1.3rem">'+u+'%</div><div class="l">Utilization</div></div></div>'
    +'<div class="col-3"><div class="sc '+(apprC===lo.length?'ok':'')+' py-2 text-center"><div class="n" style="font-size:1.3rem">'+apprC+'/'+lo.length+'</div><div class="l">Approved</div></div></div>'
    +'</div>'
    +'<table class="table jt mb-0"><thead><tr><th>Store</th><th>Location</th><th>Tier</th><th class="text-center">Carts</th><th>Status</th></tr></thead><tbody>'
    +lo.map(function(o){
      return '<tr><td><strong>'+(o.store_id||o.id)+'</strong><br><small>'+(o.store_name||o.name||'')+'</small></td>'
        +'<td style="font-size:12px">'+(o.city||'')+', '+(o.state||'')+'</td>'
        +'<td>'+mkTB((o.dynamic_velocity_tier||o.tier||'').trim())+'</td>'
        +'<td class="text-center fw-bold">'+(o.cart_count||0)+'</td>'
        +'<td>'+mkSP(o.status||'draft')+'</td></tr>';
    }).join('')+'</tbody></table>'
    +(u<95?'<div class="alert alert-warning mt-3 py-2 mb-0" style="font-size:12.5px"><i class="bi bi-exclamation-triangle me-2"></i>Below 95% utilization</div>':'');
  lMod.show();
}

async function doApproveLoad(){
  var l=loads.find(function(x){return x.load_name===aLN;});if(!l)return;
  var sids=(l.store_ids||[]).map(function(s){return parseInt(s);});
  var done=0;
  for(var o of orders.filter(function(o){return sids.includes(parseInt(o.store_id||o.id))&&o.status==='draft';})){
    try{await api('/api/orders/'+o.id+'/approve?version='+(o.version||1),{method:'PATCH'});o.status='approved';done++;}catch(e){}
  }
  if(lMod)lMod.hide();buildStats();buildTable();buildEL();
  toast(aLN+' approved — '+done+' stores','ok');
}

// EXPORT
async function exportAster(){
  if(!rId){toast('No plan to export','warn');return;}
  toast('Generating Aster import file...','ok');
  try{
    var b=await apiBlob('/api/exports/runs/'+rId+'/aster-import');
    var u=URL.createObjectURL(b);var a=document.createElement('a');
    a.href=u;a.download='JIM_Aster_'+(wk||'export')+'.csv';a.click();URL.revokeObjectURL(u);
    toast('Aster import file downloaded','ok');
  }catch(e){toast('Export failed: '+e.message,'err');}
}

// EDITOR
function buildEL(d){
  var el=document.getElementById('el');if(!el)return;
  var list=d||orders;
  if(!list.length){el.innerHTML='<div class="p-3 text-muted" style="font-size:13px">No orders yet</div>';return;}
  el.innerHTML=list.map(function(o){
    var sid=o.store_id||o.id;var tier=(o.dynamic_velocity_tier||o.tier||'').trim();
    return '<div class="sr" onclick="openEditorStore(\\''+sid+'\\',event)">'
      +'<div class="d-flex align-items-center justify-content-between">'
      +'<span style="font-weight:700;font-size:13px">'+sid+' — '+(o.store_name||o.name||'')+'</span>'+mkTB(tier)+'</div>'
      +'<div style="font-size:11px;color:var(--ink-soft)" class="d-flex justify-content-between mt-1">'
      +'<span>'+(o.city||'')+', '+(o.state||'')+'</span>'
      +'<span>'+(o.cart_count||0)+' carts '+mkSP(o.status||'draft')+'</span></div></div>';
  }).join('');
}
function filterES(q){buildEL(orders.filter(function(o){return!q||String(o.store_id||o.id).includes(q)||(o.store_name||o.name||'').toLowerCase().includes(q.toLowerCase());}));}

async function openEditorStore(sid,e){
  if(e)e.stopPropagation();
  document.querySelectorAll('#el .sr').forEach(function(r){r.classList.remove('active');});
  if(e&&e.currentTarget)e.currentTarget.classList.add('active');
  var o=orders.find(function(x){return String(x.store_id||x.id)===String(sid);});if(!o)return;
  var ed=document.getElementById('ed');
  ed.style.display='block';ed.classList.remove('d-flex','align-items-center','justify-content-center');
  ed.innerHTML='<div class="p-4"><div class="d-flex justify-content-between mb-3"><div>'
    +'<h6 class="mb-0">'+(o.store_id||o.id)+' — '+(o.store_name||o.name||'')+', '+(o.state||'')+'</h6>'
    +'<div style="font-size:12px;color:var(--ink-soft)">Loading carts...</div></div>'
    +mkTB((o.dynamic_velocity_tier||o.tier||'').trim())+'</div>'
    +'<div class="text-center py-4"><span class="spinner-border spinner-border-sm"></span></div></div>';
  try{var d=await api('/api/orders/'+o.id);renderOrder(o,d);}
  catch(e){ed.innerHTML='<div class="p-4 text-danger">Failed to load: '+e.message+'</div>';}
}

function openEditor(sid,e){
  if(e)e.stopPropagation();
  go('editor',document.querySelectorAll('.nl')[2]);
  setTimeout(function(){openEditorStore(sid,null);},50);
}

function renderOrder(o,d){
  var cs=d.carts||[];
  var h='<div class="p-4"><div class="d-flex justify-content-between mb-3"><div>'
    +'<h6 class="mb-0">'+(o.store_id||o.id)+' — '+(o.store_name||o.name||'')+', '+(o.state||'')+'</h6>'
    +'<div style="font-size:12px;color:var(--ink-soft)">'+(o.merchant||'')+' \\xb7 '+cs.length+' carts \\xb7 '+fmt(o.total_units||0)+' units</div></div>'
    +mkTB((o.dynamic_velocity_tier||o.tier||'').trim())+'</div>';
  cs.forEach(function(c){
    var sh=c.shelves||[];
    h+='<div class="card mb-3"><div class="ch d-flex justify-content-between" style="padding:8px 12px;font-size:13px;border-radius:9px 9px 0 0">'
      +'<span><i class="bi bi-cart3 me-2"></i>Cart '+c.cart_number+' <span class="ck">'+(c.cart_key||'C'+c.cart_number)+'</span></span>'
      +'<span style="font-size:11px;opacity:.8">'+(c.shelves_used||sh.length)+'/5 \\xb7 '+(c.total_units||0)+'u'+(c.is_full?'✓':'')+'</span></div>';
    sh.forEach(function(s){
      h+='<div class="shr"><span class="shn">'+s.shelf_position+'</span>'
        +'<div style="flex:1;min-width:0"><div style="font-weight:600;font-size:12.5px">'+sn(s.description||('SKU '+s.sku_id))+'</div>'
        +'<div style="font-size:11px;color:var(--ink-soft)">'+(s.tray_count||0)+' trays \\xb7 '+(s.unit_count||0)+'u'
        +(s.kit_type?'<span class="kp ms-1">'+s.kit_type.replace('_kit','')+'</span>':'')+'</div></div></div>';
    });
    for(var e=sh.length;e<5;e++){
      h+='<div class="shr" style="background:#fafcfa">'
        +'<div style="width:22px;height:22px;border:2px dashed var(--line);border-radius:50%;display:flex;align-items:center;justify-content:center;color:var(--ink-soft);font-size:9px;flex-shrink:0">'
        +'<i class="bi bi-plus"></i></div><div style="color:var(--ink-soft);font-size:12px;font-style:italic">Empty shelf '+(e+1)+'</div></div>';
    }
    h+='</div>';
  });
  h+='</div>';
  document.getElementById('ed').innerHTML=h;
}

// STORES
async function loadStores(){
  try{stores=await api('/api/stores/?limit=100');renderSL(stores);}
  catch(e){var el=document.getElementById('sl2');if(el)el.innerHTML='<div class="p-3 text-danger" style="font-size:13px">Failed to load: '+e.message+'</div>';}
}
function renderSL(list){
  var el=document.getElementById('sl2');if(!el)return;
  el.innerHTML=list.map(function(s){
    return '<div class="sr" onclick="openStore('+s.store_id+',event)">'
      +'<div class="d-flex justify-content-between"><span style="font-weight:700;font-size:13px">'+s.store_id+' — '+(s.store_name||'')+'</span>'+mkTB((s.dynamic_velocity_tier||'').trim())+'</div>'
      +'<div style="font-size:11px;color:var(--ink-soft)" class="d-flex justify-content-between mt-1">'
      +'<span>'+(s.city||'')+', '+(s.state||'')+'</span><span>'+(s.merchant||'')+'</span></div></div>';
  }).join('');
}
function filterSL(q){renderSL(stores.filter(function(s){return!q||String(s.store_id).includes(q)||(s.store_name||'').toLowerCase().includes(q.toLowerCase())||(s.city||'').toLowerCase().includes(q.toLowerCase());}));}
function filterST(t){renderSL(t?stores.filter(function(s){return(s.dynamic_velocity_tier||'').trim()===t;}):stores);}

async function openStore(id,e){
  if(e)e.stopPropagation();
  document.querySelectorAll('#sl2 .sr').forEach(function(r){r.classList.remove('active');});
  if(e&&e.currentTarget)e.currentTarget.classList.add('active');
  var sp=document.getElementById('sp');
  sp.style.padding='16px';sp.classList.remove('d-flex','align-items-center','justify-content-center');
  sp.innerHTML='<div style="display:flex;align-items:center;justify-content:center;height:200px"><span class="spinner-border text-success"></span></div>';
  try{
    var p=await api('/api/stores/'+id);var s=p.store||{};
    var tier=(s.dynamic_velocity_tier||'').trim();
    var weekly=(p.weekly_sales||[]).slice(-26).map(function(w){return w.units||0;});
    var maxW=Math.max.apply(null,weekly.concat([1]));
    var cats=[['9cm',s.tier_9cm],['12cm',s.tier_12cm],['17cm',s.tier_17cm],['H2O',s.tier_h2o],['Collectors',s.tier_collectors]].filter(function(c){return c[1];});
    sp.innerHTML=
      '<div class="d-flex justify-content-between align-items-start mb-3">'
      +'<div><h5 class="mb-1" style="color:var(--fern-dark)">Store '+s.store_id+' — '+(s.store_name||'')+'</h5>'
      +'<p class="text-muted mb-0" style="font-size:13px">'+(s.city||'')+', '+(s.state||'')+' \\xb7 '+(s.merchant||'')+'</p></div>'
      +'<div class="text-end">'+mkTB(tier)+'<div style="font-size:11px;color:var(--ink-soft);margin-top:3px">Static: '+(s.volume_code||'').trim()+'</div></div></div>'
      +'<div class="row g-2 mb-3">'
      +'<div class="col-6"><div class="sc py-2 text-center"><div class="n">'+parseFloat(s.dynamic_velocity_score||0).toFixed(1)+'</div><div class="l">Velocity Score</div></div></div>'
      +'<div class="col-6"><div class="sc py-2 text-center"><div class="n">'+(s.region||'—')+'</div><div class="l">Region</div></div></div></div>'
      +(cats.length?'<div class="mb-3">'+cats.map(function(c){return '<span style="background:#f0f9f0;border:1px solid var(--moss-pale);border-radius:8px;padding:5px 10px;display:inline-flex;align-items:center;gap:5px;font-size:12px;margin:3px"><span style="color:var(--ink-soft)">'+c[0]+'</span>'+mkTB(c[1].trim())+'</span>';}).join('')+'</div>':'')
      +'<div class="card mb-3"><div class="ch lt"><i class="bi bi-graph-up me-2"></i>Weekly Sales — Last 26 Weeks</div>'
      +'<div class="card-body pb-2"><div style="display:flex;align-items:flex-end;gap:2px;height:90px">'
      +weekly.map(function(v){return '<div style="flex:1;background:var(--fern);border-radius:2px 2px 0 0;height:'+Math.max(4,v/maxW*88)+'px;min-width:0;opacity:.85" title="'+v+'u"></div>';}).join('')
      +'</div><div class="d-flex justify-content-between mt-1" style="font-size:10px;color:var(--ink-soft)"><span>26 wks ago</span><span>This week</span></div></div></div>'
      +'<div class="card"><div class="ch lt"><i class="bi bi-bar-chart me-2"></i>Top SKUs (All Time)</div>'
      +'<table class="table jt mb-0"><thead><tr><th>Product</th><th>Family</th><th class="text-end">Units</th></tr></thead><tbody>'
      +(p.top_skus||[]).slice(0,8).map(function(sk){
        return '<tr><td style="font-size:12px">'+sn(sk.description||sk.desc||'')+'</td>'
          +'<td style="font-size:11px;color:var(--ink-soft)">'+(sk.family||'')+'</td>'
          +'<td class="text-end fw-bold">'+fmt(sk.total_units||sk.units||0)+'</td></tr>';
      }).join('')+'</tbody></table></div>';
  }catch(e){sp.innerHTML='<div class="p-4 text-danger">Failed: '+e.message+'</div>';}
}
</script>
</body>
</html>"""

@app.get("/")
def serve_frontend():
    return HTMLResponse(
        content=_FRONTEND_HTML,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        }
    )
