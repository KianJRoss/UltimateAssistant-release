"use strict";
const $ = id => document.getElementById(id);
const node = (tag, text = "", className = "") => {
  const element = document.createElement(tag);
  element.textContent = text == null ? "" : String(text);
  if (className) element.className = className;
  return element;
};
const pill = (text, good = true, type = false) => node("span", text, `pill ${type ? "type" : good ? "ok" : "bad"}`);
const action = (label, handler, danger = false) => {
  const button = node("button", label, danger ? "danger" : "");
  button.type = "button";
  button.addEventListener("click", handler);
  return button;
};
const cell = (value, className = "") => {
  const td = node("td", "", className);
  td.append(value instanceof Node ? value : document.createTextNode(value == null ? "" : String(value)));
  return td;
};
const render = (id, rows, columns, empty) => {
  const body = $(id); body.replaceChildren();
  if (!rows.length) {
    const tr = node("tr"), td = cell(empty, "mono"); td.colSpan = columns; tr.append(td); body.append(tr); return;
  }
  rows.forEach(row => body.append(row));
};
const tr = (...cells) => { const row = node("tr"); row.append(...cells); return row; };
const request = async (path, options) => { const response = await fetch(path, options); const data = await response.json(); if (!response.ok) throw new Error(data.detail || JSON.stringify(data)); return data; };

const loginAdapters = {};
function loginPanel(cli) {
  const panel = node("div", "", "login-panel"); panel.id = `login-panel-${cli}`;
  const title = node("div", `Login: ${cli}`, "mono"); title.style.marginBottom = "6px";
  const output = node("pre", "(not started)", "mono"); output.id = `login-output-${cli}`;
  const row = node("div", "", "row"), input = node("input"); input.id = `login-code-${cli}`; input.placeholder = "paste code here if prompted";
  row.append(action("Start login", () => startLogin(cli)), action("Poll", () => pollLogin(cli)), action("Cancel", () => cancelLogin(cli), true), input, action("Submit code", () => submitCode(cli)));
  panel.append(title, output, row); return panel;
}

async function refresh() {
  try {
    const paths = ["/backends","/usage/summary","/logs/recent?limit=40","/auth/status","/auth/capabilities","/accounts","/agent-sessions?limit=50","/capture/kapture","/integrations/discover","/capture/g4f/accounts","/capture/g4f/sessions","/flow-runs","/events?limit=30","/hooks","/devices/pending","/devices"];
    const [backends,usage,logs,auth,authCapabilities,accounts,agentSessions,kapture,integrations,captureAccounts,captureSessions,flowRuns,events,hooks,pendingDevices,trustedDevices] = await Promise.all(paths.map(path => fetch(path).then(response => response.ok ? response.json() : {})));
    render("accounts-body", (accounts.accounts || []).map(a => tr(cell(a.name),cell(a.provider),cell(a.auth_kind),cell(a.priority),cell(pill(a.enabled ? "enabled" : "disabled", a.enabled)),cell(a.secret_ref || "profile/session","mono"),cell(a.auth_kind === "api_key" ? action("Activate", () => activateAccount(a.name)) : ""))), 7, "no accounts");
    const test = $("test-account"), selectedTest = test.value; test.replaceChildren();
    (accounts.accounts || []).filter(a => a.enabled).forEach(a => { const option = node("option", `${a.name} (${a.provider})`); option.value = a.name; test.append(option); }); if (selectedTest) test.value = selectedTest;
    render("agent-sessions-body", (agentSessions.sessions || []).map(s => { const controls=node("span"); controls.append(action("Reset",()=>resetAgentMemory(s.id))," ",action("Delete",()=>deleteAgentMemory(s.id),true)); return tr(cell(s.name),cell(s.memory),cell(s.model),cell(`${s.project || "global"}/${s.part || "-"}`),cell(s.turn_count),cell((s.updated_at || "").slice(0,19),"mono"),cell(controls)); }), 7, "no named agent memories");
    $("kapture-status").textContent = `Kapture ${kapture.version || ""}: ${kapture.registered ? "registered" : "not registered"}; bridge ${kapture.bridge_reachable ? "reachable" : "offline"} — ${kapture.note || ""}`;
    render("integrations-body", (integrations.integrations || []).map(i => tr(cell(i.owner),cell(i.name),cell(i.kind),cell(i.transport),cell(i.source,"wrap mono"),cell(i.imported ? pill("yes") : action("Import",()=>importIntegration(i.owner,i.name,i.source))))), 6, "no external CLI connections discovered");
    const capture=$("capture-account"), selectedCapture=capture.value; capture.replaceChildren(); (captureAccounts.accounts || []).forEach(a=>{const option=node("option",`${a.name} — ${a.email_hint || ""}`);option.value=a.name;capture.append(option);}); if(selectedCapture)capture.value=selectedCapture;
    render("capture-sessions-body",(captureSessions.sessions||[]).map(s=>tr(cell((s.id||"").slice(0,12),"mono"),cell(s.account),cell(s.status),cell(s.identity_verified?pill("yes"):"no"),cell((s.started_at||"").slice(0,19),"mono"),cell(s.error||(s.status==="materialized"?"restart G4F stack to load":""),"wrap mono"))),6,"no capture sessions");
    render("flow-runs-body",(flowRuns.runs||[]).map(r=>tr(cell((r.id||"").slice(0,12),"mono"),cell(r.name),cell(r.mode),cell(r.status),cell(`${r.current_stage}/${r.total_stages}`),cell((r.updated_at||"").slice(0,19),"mono"))),6,"no persistent runs");
    render("events-body",(events.events||[]).map(e=>tr(cell((e.created_at||"").slice(0,19),"mono"),cell(e.topic),cell(JSON.stringify(e.payload),"wrap mono"))),3,"no events");
    render("hooks-body",(hooks.hooks||[]).map(h=>tr(cell(h.name),cell(h.pattern,"mono"),cell(h.transport),cell(h.config.url||(h.config.command||[]).join(" "),"wrap mono"),cell(action("Remove",()=>removeHook(h.name))))),5,"no hooks");
    render("pending-devices-body",(pendingDevices.devices||[]).map(d=>tr(cell(d.short_code,"mono"),cell(d.display_name),cell((d.last_seen||"").slice(0,19),"mono"),cell(action("Approve",()=>approveDevice(d.short_code))))),4,"no new devices discovered on the network");
    render("trusted-devices-body",(trustedDevices.devices||[]).map(d=>tr(cell(d.short_code,"mono"),cell(d.display_name),cell((d.approved_at||"").slice(0,19),"mono"),cell(action("Revoke",()=>revokeDevice(d.node_id),true)))),4,"no trusted devices yet");
    const loginClis=(auth.clis||[]).filter(c=>authCapabilities.clis?.[c.cli]?.login);
    loginClis.forEach(c=>{loginAdapters[c.cli]=authCapabilities.clis[c.cli].adapter||c.cli;});
    render("auth-body",(auth.clis||[]).map(c=>tr(cell(c.cli),cell(pill(c.status==="logged_in"?"logged in":c.status,c.status==="logged_in")),cell(c.detail||"","wrap mono"),cell(authCapabilities.clis?.[c.cli]?.login?action("Login…",()=>$("login-panel-"+c.cli).classList.toggle("open")):"not supported","mono"))),4,"no CLIs checked");
    const panels=$("login-panels"),panelKey=loginClis.map(c=>c.cli).sort().join("|"); if(panels.dataset.built!==panelKey){panels.replaceChildren(...loginClis.map(c=>loginPanel(c.cli)));panels.dataset.built=panelKey;}
    render("backends-body",(backends.backends||[]).map(b=>tr(cell(b.name),cell(pill(b.backend_type,true,true)),cell(b.pool_name||"—","mono"),cell(b.priority),cell(pill(b.circuit_open?"open":"closed",!b.circuit_open)),cell(JSON.stringify(b.config),"wrap mono"),cell(action("Delete",()=>deleteBackend(b.name),true)))),7,"no backends registered");
    render("sessions-body",(usage.browser_sessions||[]).map(s=>tr(cell(s.backend_name),cell(pill(s.consecutive_failures?"failing":"healthy",!s.consecutive_failures)),cell(s.last_success_at||"—","mono"),cell(s.last_failure_at||"—","mono"),cell(s.last_error||"","wrap mono"))),5,"no browser-session backends registered");
    render("usage-body",(usage.by_backend||[]).map(u=>tr(cell(u.backend_name),cell(pill(u.backend_type,true,true)),cell(u.total_calls),cell(`${u.successful_calls}/${u.total_calls}`),cell(u.total_input_tokens??"—"),cell(u.total_output_tokens??"—"),cell(u.total_cost_usd==null?"—":`$${Number(u.total_cost_usd).toFixed(4)}`),cell(u.avg_duration_ms==null?"—":Math.round(u.avg_duration_ms)))),8,"no calls logged yet");
    render("logs-body",(logs.calls||[]).map(c=>tr(cell((c.timestamp||"").replace("T"," ").slice(0,19),"mono"),cell(c.backend_name),cell(pill(c.success?"ok":"fail",c.success)),cell(c.duration_ms),cell(c.prompt_preview,"wrap mono"),cell(c.response_preview||c.error,"wrap mono"))),6,"no calls logged yet");
    $("refresh-indicator").textContent="updated "+new Date().toLocaleTimeString();
  } catch(error) { $("refresh-indicator").textContent="refresh failed: "+error.message; }
}

async function refreshCliUsage(){try{const data=await request("/usage/cli?refresh=true");render("cli-usage-body",(data.clis||[]).map(c=>{const u=c.usage||{};return tr(cell(c.cli),cell(c.status==="ok"?pill(c.plan_type||"ok"):c.status),cell(u.input_tokens??"-"),cell(u.output_tokens??"-"),cell(u.cache_creation_input_tokens??"-"),cell(u.cache_read_input_tokens??"-"));}),6,"no CLI usage data");}catch(error){render("cli-usage-body",[],6,"refresh failed: "+error.message);}}
const CLI_LOGIN_WARNINGS={codex:"Starting this immediately invalidates the existing Codex login. Continue?",claude:"This starts a new Claude login. Continue when ready to finish it.",antigravity:"This starts a new Antigravity login using the configured example account. Continue?"};
function renderLoginResult(cli,data){const out=$("login-output-"+cli);if(out)out.textContent=data.error?`Error: ${data.error}\n\n${data.output||""}`:`${data.output||"(no output yet)"}\n\n[${data.state||"invalid"}]`;}
async function loginAction(cli,operation){try{const data=await operation();renderLoginResult(cli,data);return data;}catch(error){const data={state:"invalid",error:error.message};renderLoginResult(cli,data);return data;}}
async function startLogin(cli){const adapter=loginAdapters[cli]||cli;if(CLI_LOGIN_WARNINGS[adapter]&&!confirm(CLI_LOGIN_WARNINGS[adapter]))return;$("login-panel-"+cli).classList.add("open");const data=await loginAction(cli,()=>request("/auth/login/start",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({cli})}));if(data.running)setTimeout(()=>pollLogin(cli),3000);}
async function submitCode(cli){const input=$("login-code-"+cli),code=input.value.trim();if(!code)return;input.value="";await loginAction(cli,()=>request("/auth/login/code",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({cli,code})}));}
async function pollLogin(cli){const data=await loginAction(cli,()=>request(`/auth/login/${encodeURIComponent(cli)}`));if(data.running)setTimeout(()=>pollLogin(cli),3000);}
async function cancelLogin(cli){await loginAction(cli,()=>request(`/auth/login/${encodeURIComponent(cli)}/cancel`,{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"}));}
async function testAccount(){const account=$("test-account").value;if(!account)return;$("test-output").textContent="testing this account directly...";try{const data=await request(`/accounts/${encodeURIComponent(account)}/test`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({prompt:$("test-prompt").value})});$("test-output").textContent=`${data.account} / ${data.lane} -> ${data.backend}\n\n${data.content}`;}catch(error){$("test-output").textContent=error.message;}}
async function resetAgentMemory(id){await fetch(`/agent-sessions/${encodeURIComponent(id)}/reset`,{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});refresh();}
async function deleteAgentMemory(id){if(confirm("Permanently delete this encrypted agent memory?")){await fetch(`/agent-sessions/${encodeURIComponent(id)}`,{method:"DELETE"});refresh();}}
async function flowRequest(path){const data=await request(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({spec:$("flow-yaml").value,input:$("flow-input").value})});$("flow-output").textContent=data.result?.content||data.content||JSON.stringify(data,null,2);refresh();}
async function registerKapture(){await fetch("/capture/kapture/register",{method:"POST"});refresh();}
async function startG4FCapture(){const account=$("capture-account").value;if(!account)return;const data=await request("/capture/g4f/sessions",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({account,timeout:Number($("capture-timeout").value)||120})});$("capture-instruction").textContent=data.instruction;refresh();}
async function importIntegration(owner,name,source){await request("/integrations/import",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({owner,name,source,scope:"global"})});refresh();}
async function addWebhook(){await request("/hooks",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:$("hook-name").value.trim(),pattern:$("hook-pattern").value.trim(),transport:"http",config:{url:$("hook-url").value.trim()}})});refresh();}
async function removeHook(name){await request(`/hooks/${encodeURIComponent(name)}`,{method:"DELETE"});refresh();}
async function activateAccount(name){await request(`/accounts/${encodeURIComponent(name)}/activate`,{method:"POST"});refresh();}
async function deleteBackend(name){if(confirm(`Delete backend '${name}'?`)){await fetch(`/backends/${encodeURIComponent(name)}`,{method:"DELETE"});refresh();}}
async function approveDevice(code){await request("/devices/approve",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({code})});refresh();}
async function revokeDevice(nodeId){if(confirm("Revoke this device's access?")){await request(`/devices/${encodeURIComponent(nodeId)}/revoke`,{method:"POST"});refresh();}}

$("add-backend-form").addEventListener("submit",async event=>{event.preventDefault();const form=new FormData(event.target),status=$("add-backend-status");try{const config=form.get("config").trim()?JSON.parse(form.get("config")):{};await request("/backends",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:form.get("name"),backend_type:form.get("backend_type"),config,pool_name:form.get("pool_name")||null,priority:Number(form.get("priority"))||100,enabled:form.get("enabled")==="on"})});status.textContent="Backend saved.";event.target.reset();refresh();}catch(error){status.textContent="Error: "+error.message;}});
$("add-account-form").addEventListener("submit",async event=>{event.preventDefault();const form=new FormData(event.target),status=$("add-account-status");try{const config=form.get("config").trim()?JSON.parse(form.get("config")):{};await request("/accounts",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:form.get("name"),provider:form.get("provider"),auth_kind:form.get("auth_kind"),secret_ref:form.get("secret_ref")||null,priority:Number(form.get("priority"))||100,config})});status.textContent="Account saved.";event.target.reset();refresh();}catch(error){status.textContent="Error: "+error.message;}});
[["test-account-button",testAccount],["capture-start-button",startG4FCapture],["flow-validate-button",()=>flowRequest("/flows/validate")],["flow-run-button",()=>flowRequest($("flow-persist").checked?"/flow-runs":"/flows/run")],["hook-add-button",addWebhook],["kapture-register-button",registerKapture],["auth-refresh-button",async()=>{await fetch("/auth/refresh",{method:"POST"});refresh();}]].forEach(([id,handler])=>$(id).addEventListener("click",handler));
refresh(); refreshCliUsage(); setInterval(refresh,5000); setInterval(refreshCliUsage,60000);
