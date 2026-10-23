"use strict";
/* ------------------------------------------------------------------ */
/* State                                                               */
/* ------------------------------------------------------------------ */
var logLines       = [];
var LOG_MAX        = 400;
var activeChannels = [];
var sseSource      = null;
var currentPanel   = "status";
var configLoaded   = {};
var captureRunning = false;
var lastEndpointsKey = null;
var lastChannelsKey  = null;

/* ------------------------------------------------------------------ */
/* Startup                                                             */
/* ------------------------------------------------------------------ */
window.addEventListener("DOMContentLoaded", function () {
  refreshStatus();
  setInterval(refreshStatus, 3000);
  startSSE();
  document.addEventListener("click", function (e) {
    if (!e.target.closest(".transfer-wrap"))
      document.querySelectorAll(".transfer-menu.open").forEach(function (m) { m.classList.remove("open"); });
  });
});

/* ------------------------------------------------------------------ */
/* Panel switching                                                     */
/* ------------------------------------------------------------------ */
function switchPanel(name, btn) {
  document.querySelectorAll(".nav-tab").forEach(function (b) { b.classList.remove("active"); });
  document.querySelectorAll(".panel").forEach(function (p) { p.classList.remove("active"); });
  btn.classList.add("active");
  document.getElementById("panel-" + name).classList.add("active");
  currentPanel = name;
  if (name === "config") {
    var pane = document.querySelector("#panel-config .tab-pane.active");
    if (pane) { var f = pane.id.replace("ctab-", ""); if (!configLoaded[f]) loadConfig(f); }
  }
  if (name === "recordings") loadRecordings();
  if (name === "capture") { loadCaptures(); refreshCaptureStatus(); }
}

/* ------------------------------------------------------------------ */
/* Status                                                              */
/* ------------------------------------------------------------------ */
function refreshStatus() {
  fetch("/api/status").then(function (r) { return r.json(); }).then(function (data) {
    document.getElementById("version-badge").textContent = "Asterisk " + data.version;
    var dot = document.getElementById("ami-dot"), label = document.getElementById("ami-label");
    if (data.ami_connected) { dot.className = "dot green"; label.textContent = "AMI connected"; }
    else { dot.className = "dot grey"; label.textContent = "AMI disconnected"; }
    var epKey = JSON.stringify(data.endpoints || []);
    if (epKey !== lastEndpointsKey) { lastEndpointsKey = epKey; renderEndpoints(data.endpoints || []); }
    activeChannels = data.channels || [];
    var chKey = JSON.stringify(activeChannels.map(function (c) {
      return [c.id, c.name, c.state, c.dialplan && c.dialplan.exten];
    }));
    if (chKey !== lastChannelsKey) { lastChannelsKey = chKey; renderChannels(activeChannels); }
  }).catch(function () {
    document.getElementById("ami-dot").className = "dot grey";
    document.getElementById("ami-label").textContent = "WebUI unreachable";
    lastChannelsKey = null;
    renderChannels([]);
  });
}

var EXTENSIONS = [
  {name:"echo",num:"600"},{name:"milliwatt",num:"601"},{name:"tone",num:"602"},
  {name:"dtmf-test",num:"603"},{name:"record",num:"604"},{name:"opus-echo",num:"605"},
  {name:"silence",num:null},{name:"speak",num:null},{name:"playfile",num:"606"},
  {name:"tg-real",num:"700"},{name:"tg-tech",num:"701"},{name:"tg-routine",num:"702"},{name:"tg-routine2",num:"703"},
  {name:"p100-real",num:"704"},{name:"p100-routine",num:"705"}
];

function renderEndpoints(endpoints) {
  var ul = document.getElementById("endpoint-list");
  if (!endpoints.length) { ul.innerHTML = '<li><span class="text-dim">No endpoints</span></li>'; return; }
  ul.innerHTML = endpoints.map(function (ep) {
    var state = (ep.state || "unknown").toLowerCase();
    var dot = state === "online" ? "dot green" : state === "offline" ? "dot grey" : "dot yellow";
    var contacts = ep.channel_ids ? ep.channel_ids.length : 0;
    var detail = contacts ? " (" + contacts + " ch)" : "";
    return '<li><span class="' + dot + '"></span>' +
      '<span class="endpoint-name">' + esc(ep.resource || ep.id || "?") + '</span>' +
      '<span class="reg-state">' + esc(ep.state || "?") + esc(detail) + '</span></li>';
  }).join("");
}

function renderChannels(channels) {
  var ul = document.getElementById("channel-list");
  if (!channels.length) {
    ul.innerHTML = '<li><span class="text-dim">No active channels</span></li>';
    updateChannelSelectors([]);
    return;
  }
  ul.innerHTML = channels.map(function (ch) {
    var chId = ch.id || ch.name || "?", chName = ch.name || chId;
    var m = chName.match(/^(?:PJSIP|SIP)\/([^-]+)-/i);
    var ep = m ? m[1] : chName;
    var exten = (ch.dialplan && ch.dialplan.exten) ? ch.dialplan.exten : "";
    var state = ch.state || "";
    var label = ep + (exten ? " → " + exten : "") + (state ? " [" + state + "]" : "");
    var menu = EXTENSIONS.map(function (ex) {
      var l = ex.name + (ex.num ? " (" + ex.num + ")" : "");
      return '<button onclick="doTransfer(' + jsStr(chId) + ',' + jsStr(ex.name) + ',this)">' + esc(l) + '</button>';
    }).join("");
    return '<li><span class="ch-id" title="' + esc(chId) + '">' + esc(label) + '</span>' +
      '<span class="ch-actions">' +
        '<button class="btn-danger" onclick="hangup(' + jsStr(chId) + ')">Hangup</button>' +
        '<button class="btn-sm" onclick="holdCh(' + jsStr(chId) + ',true,this)">Mute</button>' +
        '<button class="btn-sm" onclick="holdCh(' + jsStr(chId) + ',false,this)">Unmute</button>' +
        '<button class="btn-rec" onclick="startRecord(' + jsStr(chId) + ',this)">&#9210; Rec</button>' +
        '<button class="btn-sm" onclick="stopRecord(' + jsStr(chId) + ',this)">&#9209;</button>' +
        '<span class="transfer-wrap">' +
          '<button class="btn-sm" onclick="toggleTransferMenu(this)">Transfer &#9660;</button>' +
          '<div class="transfer-menu">' + menu + '</div>' +
        '</span>' +
      '</span></li>';
  }).join("");
  updateChannelSelectors(channels);
}

function updateChannelSelectors(channels) {
  ["dtmf-channel", "play-channel"].forEach(function (id) {
    var sel = document.getElementById(id);
    if (!sel) return;
    var prev = sel.value;
    sel.innerHTML = '<option value="">— select channel —</option>';
    channels.forEach(function (ch) {
      var chId = ch.id || ch.name || "?", chName = ch.name || chId;
      var m = chName.match(/^(?:PJSIP|SIP)\/([^-]+)-/i);
      var ep = m ? m[1] : chName;
      var exten = (ch.dialplan && ch.dialplan.exten) ? ch.dialplan.exten : "";
      var opt = document.createElement("option");
      opt.value = chId;
      opt.textContent = ep + (exten ? " → " + exten : "");
      if (chId === prev) opt.selected = true;
      sel.appendChild(opt);
    });
    if (channels.length === 1 && !prev) sel.value = channels[0].id || channels[0].name || "";
  });
}

function hangup(id) {
  fetch("/api/hangup", {method:"POST", headers:{"Content-Type":"application/json"},
    body:JSON.stringify({channel:id})}).then(function(){refreshStatus();}).catch(function(){refreshStatus();});
}
function holdCh(id, hold, btn) {
  var orig = btn.textContent; btn.textContent = "…"; btn.disabled = true;
  fetch("/api/hold", {method:"POST", headers:{"Content-Type":"application/json"},
    body:JSON.stringify({channel:id, hold:hold})})
    .then(function(r){return r.json();})
    .then(function(d){btn.textContent=d.ok?(hold?"Muted ✓":"Live ✓"):"Err";btn.disabled=false;
      setTimeout(function(){btn.textContent=orig;},2000);})
    .catch(function(){btn.textContent=orig;btn.disabled=false;});
}
function toggleTransferMenu(btn) {
  var m = btn.nextElementSibling, w = m.classList.contains("open");
  document.querySelectorAll(".transfer-menu.open").forEach(function(x){x.classList.remove("open");});
  if (!w) m.classList.add("open");
}
function doTransfer(chId, ext, btn) {
  var m = btn.closest(".transfer-menu"); if (m) m.classList.remove("open");
  fetch("/api/transfer",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({channel:chId,extension:ext})})
    .then(function(r){return r.json();}).then(function(d){if(!d.ok)alert("Transfer failed: "+(d.error||"?"));
      else refreshStatus();}).catch(function(e){alert("Transfer error: "+e);});
}
function startRecord(chId, btn) {
  var orig = btn.textContent; btn.textContent = "…"; btn.disabled = true;
  fetch("/api/record",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({channel:chId})})
    .then(function(r){return r.json();}).then(function(d){btn.disabled=false;
      if(d.ok){btn.innerHTML="&#9210; REC";btn.style.color="var(--err)";btn.title="Recording to "+(d.file||"?");}
      else{btn.textContent="Err";setTimeout(function(){btn.textContent=orig;btn.style.color="";},3000);}})
    .catch(function(){btn.textContent=orig;btn.disabled=false;});
}
function stopRecord(chId, btn) {
  var orig = btn.textContent; btn.textContent = "…"; btn.disabled = true;
  fetch("/api/stoprecord",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({channel:chId})})
    .then(function(r){return r.json();}).then(function(d){btn.disabled=false;
      btn.textContent=d.ok?"\u23F9 Done":"Err";setTimeout(function(){btn.textContent=orig;},3000);
      if(d.ok){var li=btn.closest("li"),rec=li?li.querySelector(".btn-rec"):null;
        if(rec){rec.style.color="";rec.title="Start MixMonitor recording";}}})
    .catch(function(){btn.textContent=orig;btn.disabled=false;});
}

/* ------------------------------------------------------------------ */
/* Calls — ring device                                                 */
/* ------------------------------------------------------------------ */
function ringDevice() {
  var acct = document.getElementById("ring-account").value;
  var ext  = document.getElementById("ring-extension").value;
  var res  = document.getElementById("ring-result");
  res.textContent = "Ringing…"; res.className = "result-msg";
  fetch("/api/ring",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({account:acct,extension:ext})})
    .then(function(r){return r.json();}).then(function(d){
      if(d.ok){res.textContent="Ringing PJSIP/"+acct+" → "+ext;res.className="result-msg ok";}
      else{res.textContent="Error: "+(d.error||"?");res.className="result-msg err";}})
    .catch(function(e){res.textContent="Failed: "+e;res.className="result-msg err";});
}

/* ------------------------------------------------------------------ */
/* Calls — DTMF                                                        */
/* ------------------------------------------------------------------ */
function sendDtmf() {
  var digits = document.getElementById("dtmf-digits").value.trim();
  var res    = document.getElementById("dtmf-result");
  var sel    = document.getElementById("dtmf-channel");
  var chId   = sel ? sel.value : "";

  if (!digits) { res.textContent = "Enter digits."; res.className = "result-msg err"; return; }
  if (!chId) {
    if (!activeChannels.length) { res.textContent = "No active channel."; res.className = "result-msg err"; return; }
    chId = activeChannels[0].id || activeChannels[0].name;
  }
  res.textContent = "Sending…"; res.className = "result-msg";
  fetch("/api/dtmf",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({channel:chId, digits:digits})})
    .then(function(r){return r.json();}).then(function(d){
      if(d.ok){res.textContent="Sent ["+d.digits+"]";res.className="result-msg ok";}
      else{res.textContent="Error: "+(d.error||"?");res.className="result-msg err";}})
    .catch(function(e){res.textContent="Failed: "+e;res.className="result-msg err";});
}

/* ------------------------------------------------------------------ */
/* Calls — play WAV to channel                                         */
/* ------------------------------------------------------------------ */
function refreshPlayFiles() {
  fetch("/api/recordings").then(function(r){return r.json();}).then(function(data){
    var sel = document.getElementById("play-file");
    var recs = data.recordings || [];
    if (!recs.length) { sel.innerHTML = '<option value="">— no WAV files —</option>'; return; }
    sel.innerHTML = recs.map(function(r){
      return '<option value="'+esc(r.name)+'">'+esc(r.name)+'</option>';}).join("");
  }).catch(function(){});
}

function playWavToChannel() {
  var ch   = document.getElementById("play-channel").value;
  var file = document.getElementById("play-file").value;
  var res  = document.getElementById("play-result");
  if (!ch) { res.textContent = "Select a channel."; res.className = "result-msg err"; return; }
  if (!file) { res.textContent = "Select a file."; res.className = "result-msg err"; return; }
  res.textContent = "Playing…"; res.className = "result-msg";
  fetch("/api/playback",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({channel:ch, filename:file})})
    .then(function(r){return r.json();}).then(function(d){
      if(d.ok){res.textContent="Playing "+file+" → channel";res.className="result-msg ok";}
      else{res.textContent="Error: "+(d.error||"?");res.className="result-msg err";}})
    .catch(function(e){res.textContent="Failed: "+e;res.className="result-msg err";});
}

/* ------------------------------------------------------------------ */
/* Recordings                                                          */
/* ------------------------------------------------------------------ */
function loadRecordings() {
  fetch("/api/recordings").then(function(r){return r.json();}).then(function(data){
    var recs = data.recordings || [];
    document.getElementById("rec-status").textContent = recs.length + " file" + (recs.length!==1?"s":"");
    renderRecordings(recs);
  }).catch(function(){});
}

function renderRecordings(recs) {
  var ul = document.getElementById("rec-list");
  if (!recs.length) { ul.innerHTML = '<li class="text-dim">No .wav files yet</li>'; return; }
  ul.innerHTML = recs.map(function(r){
    var n = esc(r.name), nj = jsStr(r.name), ne = encodeURIComponent(r.name);
    return '<li><div class="rec-card" id="rec-'+n+'">' +
      '<div class="rec-header">' +
        '<span class="rec-name">'+n+'</span>' +
        '<span class="rec-meta">'+esc(formatSize(r.size))+' &bull; '+esc(relTime(r.mtime))+'</span>' +
        '<span class="rec-header-actions">' +
          '<a href="/api/recordings/'+ne+'/download" class="btn-sm btn-dl">Download</a>' +
          '<button class="btn-danger" onclick="deleteRec('+nj+')">Delete</button>' +
        '</span></div>' +
      '<div class="rec-body">' +
        '<audio controls preload="none" src="/api/recordings/'+ne+'/wav"></audio>' +
        '<button class="analysis-toggle" onclick="toggleAnalysis('+nj+',this)">' +
          '<span class="toggle-arrow">&#9658;</span> Analysis</button>' +
        '<div class="analysis-section" id="analysis-'+n+'"></div>' +
      '</div></div></li>';
  }).join("");
}

function deleteRec(name) {
  if (!confirm("Delete "+name+"?")) return;
  fetch("/api/recordings/"+encodeURIComponent(name),{method:"DELETE"})
    .then(function(r){return r.json();}).then(function(d){if(d.ok||d.ok===undefined)loadRecordings();
      else alert("Delete failed: "+(d.error||"?"));}).catch(function(e){alert("Error: "+e);});
}

function uploadWav(input) {
  if (!input.files || !input.files[0]) return;
  var fd = new FormData(); fd.append("file", input.files[0]);
  var res = document.getElementById("upload-result");
  res.textContent = "Uploading…"; res.className = "result-msg";
  fetch("/api/upload",{method:"POST",body:fd})
    .then(function(r){return r.json();}).then(function(d){
      if(d.ok){res.textContent="Uploaded "+d.filename;res.className="result-msg ok";
        loadRecordings();refreshPlayFiles();}
      else{res.textContent="Error: "+(d.error||"?");res.className="result-msg err";}})
    .catch(function(e){res.textContent="Failed: "+e;res.className="result-msg err";});
  input.value = "";
}

function toggleAnalysis(name, btn) {
  var sec = document.getElementById("analysis-" + name);
  var arrow = btn.querySelector(".toggle-arrow");
  if (!sec) return;
  if (sec.classList.contains("open")) { sec.classList.remove("open"); arrow.innerHTML="&#9658;"; return; }
  sec.classList.add("open"); arrow.innerHTML="&#9660;";
  if (sec.dataset.loaded) return;
  sec.dataset.loaded = "1";
  sec.innerHTML = '<div class="loading-msg">Loading analysis&hellip;</div>';
  var soxDone=false, specDone=false, soxData=null;
  function build() {
    if (!soxDone) return;
    var m = parseSoxMetrics(soxData ? soxData.stat : "");
    var mh = '<div class="metrics-row">' +
      '<div class="metric-card"><div class="metric-label">Max Amp</div><div class="metric-value">'+esc(m.maxAmp)+'</div></div>' +
      '<div class="metric-card"><div class="metric-label">RMS Amp</div><div class="metric-value">'+esc(m.rmsAmp)+'</div></div>' +
      '<div class="metric-card"><div class="metric-label">Frequency</div><div class="metric-value">'+esc(m.roughFreq)+'</div></div></div>';
    var sh = "", ssh = "";
    if (soxData && !soxData.error) {
      sh = '<div class="sox-label">sox stat</div><pre class="sox-output">'+esc(soxData.stat||"(none)")+'</pre>';
      ssh = '<div class="sox-label">sox stats</div><pre class="sox-output">'+esc(soxData.stats||"(none)")+'</pre>';
    } else { sh = '<div class="sox-output text-err">'+esc((soxData&&soxData.error)||"Error")+'</div>'; }
    var si = "spec-img-"+name, sp = "spec-ph-"+name;
    sec.innerHTML = mh + '<div class="analysis-cols"><div>'+sh+'</div><div>'+ssh+'</div></div>' +
      '<div class="spectrogram-wrap">'+(specDone?'':'<div class="spectrogram-placeholder" id="'+esc(sp)+'">Generating&hellip;</div>')+
      '<img class="spectrogram-img" id="'+esc(si)+'"'+(specDone?'':' class="hidden"')+' alt="spectrogram"></div>';
  }
  fetch("/api/sox/"+encodeURIComponent(name)).then(function(r){return r.json();})
    .then(function(d){soxData=d;soxDone=true;build();})
    .catch(function(e){soxData={error:String(e)};soxDone=true;build();});
  var img = new Image();
  img.onload = function(){specDone=true;var p=document.getElementById("spec-ph-"+name),
    i=document.getElementById("spec-img-"+name);if(p)p.style.display="none";
    if(i){i.src=img.src;i.style.display="block";}};
  img.onerror = function(){specDone=true;var p=document.getElementById("spec-ph-"+name);
    if(p)p.textContent="Spectrogram unavailable";};
  img.src = "/api/recordings/"+encodeURIComponent(name)+"/spectrogram";
}

function parseSoxMetrics(t) {
  var d = {maxAmp:"—",rmsAmp:"—",roughFreq:"—"};
  if (!t) return d;
  var a=t.match(/Maximum amplitude:\s*([\d.eE+-]+)/i),b=t.match(/RMS amplitude:\s*([\d.eE+-]+)/i),
    c=t.match(/Rough\s+frequency:\s*([\d.eE+-]+)/i);
  return {maxAmp:a?a[1]:d.maxAmp,rmsAmp:b?b[1]:d.rmsAmp,roughFreq:c?c[1]+" Hz":d.roughFreq};
}

/* ------------------------------------------------------------------ */
/* Capture                                                             */
/* ------------------------------------------------------------------ */
function refreshCaptureStatus() {
  fetch("/api/capture/status").then(function(r){return r.json();}).then(function(d){
    captureRunning = d.running;
    document.getElementById("capture-start-btn").disabled = d.running;
    document.getElementById("capture-stop-btn").disabled = !d.running;
    var st = document.getElementById("capture-status");
    st.textContent = d.running ? "Capturing → " + (d.filename || "?") : "";
    st.className = d.running ? "result-msg ok" : "result-msg";
  }).catch(function(){});
}

function captureStart() {
  var filter = document.getElementById("capture-filter").value.trim();
  var res = document.getElementById("capture-status");
  res.textContent = "Starting…"; res.className = "result-msg";
  fetch("/api/capture/start",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({filter:filter})})
    .then(function(r){return r.json();}).then(function(d){
      if(d.ok){res.textContent="Capturing → "+d.filename;res.className="result-msg ok";
        document.getElementById("capture-start-btn").disabled=true;
        document.getElementById("capture-stop-btn").disabled=false;captureRunning=true;}
      else{res.textContent="Error: "+(d.error||"?");res.className="result-msg err";}})
    .catch(function(e){res.textContent="Failed: "+e;res.className="result-msg err";});
}

function captureStop() {
  var res = document.getElementById("capture-status");
  res.textContent = "Stopping…"; res.className = "result-msg";
  fetch("/api/capture/stop",{method:"POST"}).then(function(r){return r.json();}).then(function(d){
    res.textContent = d.ok ? "Stopped ("+( d.filename||"")+")" : "Error";
    res.className = d.ok ? "result-msg ok" : "result-msg err";
    document.getElementById("capture-start-btn").disabled=false;
    document.getElementById("capture-stop-btn").disabled=true;captureRunning=false;
    loadCaptures();}).catch(function(e){res.textContent="Failed: "+e;res.className="result-msg err";});
}

function loadCaptures() {
  fetch("/api/captures").then(function(r){return r.json();}).then(function(data){
    var caps = data.captures || [];
    var ul = document.getElementById("capture-list");
    if (!caps.length) { ul.innerHTML = '<li class="text-dim">No captures yet</li>'; return; }
    ul.innerHTML = caps.map(function(c){
      var ne = encodeURIComponent(c.name), nj = jsStr(c.name);
      return '<li><div class="rec-card"><div class="rec-header">' +
        '<span class="rec-name">'+esc(c.name)+'</span>' +
        '<span class="rec-meta">'+esc(formatSize(c.size))+' &bull; '+esc(relTime(c.mtime))+'</span>' +
        '<span class="rec-header-actions">' +
          '<a href="/api/captures/'+ne+'/download" class="btn-sm btn-dl">Download</a>' +
          '<button class="btn-danger" onclick="deleteCapture('+nj+')">Delete</button>' +
        '</span></div></div></li>';
    }).join("");
  }).catch(function(){});
}

function deleteCapture(name) {
  if (!confirm("Delete "+name+"?")) return;
  fetch("/api/captures/"+encodeURIComponent(name),{method:"DELETE"})
    .then(function(){loadCaptures();}).catch(function(e){alert("Error: "+e);});
}

/* ------------------------------------------------------------------ */
/* Config                                                              */
/* ------------------------------------------------------------------ */
function switchConfigTab(file, btn) {
  document.querySelectorAll("#panel-config .tab-btn").forEach(function(b){b.classList.remove("active");});
  document.querySelectorAll("#panel-config .tab-pane").forEach(function(p){p.classList.remove("active");});
  btn.classList.add("active");
  document.getElementById("ctab-"+file).classList.add("active");
  if (!configLoaded[file]) loadConfig(file);
}
function loadConfig(file) {
  fetch("/api/config/"+file).then(function(r){return r.json();}).then(function(d){
    var ta=document.getElementById("cfg-"+file);
    if(ta&&d.content!==undefined){ta.value=d.content;configLoaded[file]=true;}}).catch(function(){});
}
function saveConfig(file) {
  var ta=document.getElementById("cfg-"+file),r=document.getElementById("cfg-result-"+file);
  r.textContent="Saving…";r.className="result-msg";
  fetch("/api/config/"+file,{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({content:ta.value})}).then(function(r){return r.json();}).then(function(d){
      if(d.ok){r.textContent="Saved & reloaded";r.className="result-msg ok";configLoaded[file]=false;loadConfig(file);}
      else{r.textContent="Error: "+(d.error||"?");r.className="result-msg err";}})
    .catch(function(e){r.textContent="Failed: "+e;r.className="result-msg err";});
}

/* ------------------------------------------------------------------ */
/* SSE Logs                                                            */
/* ------------------------------------------------------------------ */
function startSSE() {
  if (sseSource) sseSource.close();
  sseSource = new EventSource("/logs");
  sseSource.onmessage = function(ev) {
    try { handleLogMsg(JSON.parse(ev.data)); } catch(e){}
  };
}
function handleLogMsg(msg) {
  var text="", cls="log-line ";
  if (msg.source==="ami") {
    var d=msg.data||{}, ev=d.Event||"";
    var parts=Object.keys(d).filter(function(k){return k!=="Event";}).map(function(k){return k+"="+d[k];});
    text="[AMI] "+(ev?ev+" | ":"")+parts.join(" | "); cls+="ami";
    if(ev&&/error/i.test(ev))cls+=" err";
  } else { text=msg.data||""; cls+="log"; if(/ERROR|FATAL/i.test(text))cls+=" err";
    else if(/WARN/i.test(text))cls+=" warn"; }
  logLines.push({text:text,classes:cls});
  if(logLines.length>LOG_MAX)logLines.shift();
  renderLog();
}
function renderLog() {
  var f=document.getElementById("log-filter").value.toLowerCase(),
    o=document.getElementById("log-output"),a=document.getElementById("log-autoscroll").checked;
  o.innerHTML=logLines.filter(function(l){return!f||l.text.toLowerCase().indexOf(f)!==-1;})
    .map(function(l){return'<div class="'+l.classes+'">'+esc(l.text)+'</div>';}).join("");
  if(a)o.scrollTop=o.scrollHeight;
}
function clearLog(){logLines=[];document.getElementById("log-output").innerHTML="";}

/* ------------------------------------------------------------------ */
/* Utilities                                                           */
/* ------------------------------------------------------------------ */
function esc(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");}
function jsStr(s){return"'"+String(s).replace(/\\/g,"\\\\").replace(/'/g,"\\'")+"'";}
function formatSize(b){return b>=1048576?(b/1048576).toFixed(1)+" MB":(b/1024).toFixed(1)+" KB";}
function relTime(mt){var s=Math.floor(Date.now()/1000-mt);if(s<5)return"just now";if(s<60)return s+" sec ago";
  var m=Math.floor(s/60);if(m<60)return m+" min ago";var h=Math.floor(m/60);if(h<24)return h+" hr ago";
  var d=Math.floor(h/24);return d+" day"+(d!==1?"s":"")+" ago";}
