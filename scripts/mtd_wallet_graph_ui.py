"""Dependency-free saved Wallet Graph document. No store or provider imports."""
from html import escape


def page(csrf: str) -> str:
    """Return a full document; the portal supplies its existing inline CSP."""
    return _PAGE.replace('__CSRF__', escape(str(csrf), quote=True))


_PAGE = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MTD Wallet Graph</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;padding:24px;width:100%;font:15px system-ui;background:#0b1220;color:#e7eef8}h1{margin:0 0 8px}a{color:#9dc5ff}nav{display:flex;gap:18px;flex-wrap:wrap}.notice,.panel{background:#111e32;border:1px solid #334b6b;border-radius:9px;padding:16px;margin:16px 0}.badges{display:flex;gap:10px;flex-wrap:wrap}.badges span{padding:5px 10px;border:1px solid #466283;border-radius:20px}form{display:flex;gap:16px;align-items:end;flex-wrap:wrap}label{display:grid;gap:7px}form label{flex:1;min-width:240px}textarea{width:100%;min-height:110px;resize:vertical}textarea,button{font:inherit;color:inherit;background:#14253e;border:1px solid #5d7da6;border-radius:6px;padding:10px}button{cursor:pointer}button.primary{background:#2164ce;font-weight:750}button:disabled{opacity:.55;cursor:wait}button:focus-visible,a:focus-visible,svg [tabindex]:focus-visible{outline:3px solid #ffe082;outline-offset:3px}.layout{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(310px,1fr);gap:18px}.graph-panel{min-width:0}#graph{display:block;width:100%;height:66vh;min-height:560px;background:#0b172a;touch-action:none;border:1px solid #334b6b;border-radius:8px}.tools{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:12px}.tools label{display:flex;gap:5px;align-items:center}.mint{color:#b4a1ff}.transfer{color:#53c9ec}.sale{color:#f6ba60}svg text{fill:#e7eef8;font-size:14px;pointer-events:none}svg .node,svg .edge{cursor:pointer}svg .edge:focus{stroke:#fff}svg .selected{stroke:#fff;stroke-width:5}.scroll{overflow:auto}table{width:100%;border-collapse:collapse}th,td{text-align:left;vertical-align:top;padding:10px;border-bottom:1px solid #31415f}td:first-child{white-space:nowrap}td.nft{min-width:220px}small,.muted{color:#b1c1d6}code{overflow-wrap:anywhere;font-size:12px}li{margin-bottom:10px}#connections button,#patterns button{width:100%;text-align:left}#status{white-space:pre-wrap}#warnings{color:#ffdc9a}#selection{white-space:pre-wrap}#details p{overflow-wrap:anywhere}.error{color:#ffb4b4}@media(max-width:1000px){.layout{grid-template-columns:1fr}body{padding:14px}#graph{height:65vh}}
/* Saved Contents only: bounded, searchable, exact evidence stays text. */
.layout>aside{min-width:0}.holdings-search{margin:10px 0;font-size:13px}.holdings-search input{min-width:0;width:100%;font:inherit;color:inherit;background:#14253e;border:1px solid #5d7da6;border-radius:6px;padding:6px}.holdings-search input:focus-visible{outline:3px solid #ffe082;outline-offset:2px}.holdings-scroll{max-height:380px;overflow:auto;border:1px solid #334b6b;border-radius:6px;isolation:isolate}.holdings-table{font-size:12px;table-layout:fixed;min-width:300px}.holdings-table th,.holdings-table td{padding:5px 7px;overflow-wrap:anywhere;white-space:normal}.holdings-table th{position:sticky;top:0;z-index:1;background:#14253e}.holdings-table th:nth-child(1){width:43%}.holdings-table th:nth-child(2){width:11%}.holdings-table th:nth-child(3){width:46%}.holdings-table td:nth-child(3){font-family:ui-monospace,monospace}.holdings-table td:first-child{white-space:normal}.holdings-count{font-size:12px;margin:6px 0}
</style></head><body><nav><a href="http://127.0.0.1:8765/" target="_top">← MTD Control Panel</a> · <a href="/wallet-transfers">Wallet / Transfers</a><a href="/nft-history">NFT History</a></nav><h1>Wallet Graph</h1><nav><a href="/wallet-transfers">Wallet / Transfers</a><a href="/nft-history">NFT History</a></nav>
<p class="notice">Explore only the saved Polygon SQL evidence for the wallets you select. No live refresh, recursive crawling, queue changes, or notifications. Missing evidence does not establish that an event never happened.</p>
<div class="badges"><span>Saved SQL only</span><span>No marketplace calls</span><span>No notifications</span><span>No model tokens</span></div>
<section class="panel"><form id="graph-form" autocomplete="off"><input id="csrf" type="hidden" value="__CSRF__"><label for="wallets">2–50 wallets / unique saved Name Tags<textarea id="wallets" name="wallets" placeholder="One full wallet or exact unique alias per line" required></textarea><small>One per line; commas also accepted. Spaces inside Name Tags are preserved. Aliases are resolved afresh on each click.</small></label><button class="primary" id="generate" type="submit">Generate Saved Graph</button></form></section>
<p id="status" role="status" aria-live="polite">Ready. Choose wallets, then generate explicitly.</p><p id="coverage"></p><ul id="warnings"></ul>
<section class="panel" id="review-leads"><h2>Review leads</h2><p>Review leads only: common ownership and XP not proven. No XP score is calculated. Saved history can be incomplete; quantities do not identify distinct ERC-1155 copies.</p><ul id="patterns"></ul></section>
<div class="layout"><section class="panel graph-panel"><h2>Directed connections</h2><div class="tools"><label class="mint"><input type="checkbox" data-kind="mint" checked>Mint</label><label class="transfer"><input type="checkbox" data-kind="transfer" checked>Transfer</label><label class="sale"><input type="checkbox" data-kind="sale" checked>Sale</label><button id="fit" type="button">Fit</button><button id="all" type="button">Show all events</button></div><p class="muted">Drag nodes or empty space; scroll to zoom. Arrows follow the saved sender → recipient. Counts are events, not distinct copies. Select a connection or wallet to inspect exact evidence.</p><p id="wallet-legend"><span><span aria-hidden="true" style="background:#163f6c;border:1px solid #b6cee9">●</span> Wallet</span> · <span><span aria-hidden="true" style="background:#574585">◆</span> Mint source</span> · <span style="color:#ffe066">● Isolated wallet — no saved link to other selected wallets</span>. Saved links only; incomplete history does not prove no relationship.</p><svg id="graph" viewBox="0 0 1000 700" role="group" aria-label="Saved wallet graph; keyboard-accessible nodes and connections"></svg><h3>Accessible connection list</h3><ul id="connections"></ul></section>
<aside class="panel"><h2>Wallet summary</h2><div id="wallet-summary" aria-live="polite">Select a wallet to inspect saved activity and holdings.</div><h2>Selection</h2><p id="selection">No graph loaded.</p><div id="details"></div></aside></div>
<section class="panel"><h2>Selected saved NFT events</h2><p id="event-count">No evidence loaded.</p><div class="scroll"><table><thead><tr><th>Date (recorded timezone)</th><th>Type / execution</th><th>From</th><th>To</th><th>NFT / exact identity</th><th>Qty</th><th>Price</th></tr></thead><tbody id="events"></tbody></table></div></section>
<script>
'use strict';
const $=id=>document.getElementById(id),NS='http://www.w3.org/2000/svg',colors={mint:'#b4a1ff',transfer:'#53c9ec',sale:'#f6ba60'};
let graph=null,positions=new Map(),selected=null,view=[0,0,1000,700],drag=null,walletView=null;
function element(tag,text,parent){const e=document.createElement(tag);if(text!==undefined)e.textContent=String(text??'');if(parent)parent.append(e);return e;}
function svg(tag,attrs,parent){const e=document.createElementNS(NS,tag);for(const [k,v] of Object.entries(attrs))e.setAttribute(k,String(v));if(parent)parent.append(e);return e;}
function label(id){const n=graph?.nodes.find(n=>n.id===id);if(id==='__mint__')return 'Mint';const value=n?.label||id||'Unknown';return /^0x[0-9a-f]{40}$/i.test(value)?'0x...'+value.slice(-4):value;}
function enabled(kind){return !!document.querySelector('[data-kind="'+kind+'"]')?.checked;}
function allowedEvent(e){return enabled(e.kind)&&(!selected||selected.ids.has(e.id));}
function setView(){ $('graph').setAttribute('viewBox',view.join(' ')); }
function fit(){const points=[...positions.values()];if(!points.length){view=[0,0,1000,700];}else{const minX=Math.min(...points.map(p=>p.x))-120,minY=Math.min(...points.map(p=>p.y))-140,maxX=Math.max(...points.map(p=>p.x))+120,maxY=Math.max(...points.map(p=>p.y))+90;const w=Math.max(500,maxX-minX,(maxY-minY)/.7),h=w*.7;view=[(minX+maxX-w)/2,(minY+maxY-h)/2,w,h];}setView();}
function activate(e,fn){e.setAttribute('tabindex','0');e.setAttribute('role','button');e.addEventListener('click',fn);e.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();fn();}});}
function choose(ids,text,detail){clearWalletSummary();selected={ids:new Set(ids)};$('selection').textContent=text;$('details').replaceChildren();if(detail){element('p',detail.explanation||'', $('details'));for(const c of detail.caveats||[])element('p',c,$('details'));element('p','Exact asset: '+detail.asset,$('details'));}render();}
function clearWalletSummary(text='Select a wallet to inspect saved activity and holdings.'){walletView=null;$('wallet-summary').replaceChildren();element('p',text,$('wallet-summary'));}
function island(node){return node.kind!=='mint'&&!graph.edges.some(e=>['transfer','sale'].includes(e.kind)&&e.source!==e.target&&[e.source,e.target].includes(node.id)&&graph.nodes.some(n=>n.kind==='wallet'&&n.id===(e.source===node.id?e.target:e.source)));}
function selectWallet(id){const node=graph?.nodes.find(n=>n.id===id);if(!node)return;clearWalletSummary();selected={ids:new Set(graph.events.filter(e=>e.from===id||e.to===id).map(e=>e.id))};walletView={id,mode:'summary'};$('selection').textContent='Saved wallet: '+label(id);$('details').replaceChildren();walletSummary(node);render();}
function walletSummary(node){
 const box=$('wallet-summary');box.replaceChildren();
 if(node.kind==='mint'){element('p','Mint is a synthetic source, not a wallet or beneficial owner. No wallet activity or holdings summary applies.',box);return;}
 element('h3',label(node.id),box);
 if(island(node))element('p','Isolated wallet — no saved link to other selected wallets. Incomplete saved history does not establish no relationship.',box);
 const summary=graph.wallet_summaries?.[node.id],activity=summary?.activity,holdings=summary?.holdings;
 if(!summary||summary.status==='no_saved_wallet_data')element('p','No saved wallet data available in this response. Run Wallet / Transfers and ownership discovery separately; no collection starts here.',box);
 element('h3','All saved wallet activity',box);
 element('p','NFT quantity totals, not event counts. Includes saved counterparties outside this graph; checkbox and review-lead filters do not change these totals. Bounded saved history, not lifetime completeness.',box);
 if(!activity||activity.status==='unavailable')element('p','No saved movement data available for this wallet. Unavailable is not zero.',box);
 const table=element('table',undefined,box),head=element('tr',undefined,element('thead',undefined,table));element('th','Activity',head);element('th','NFT quantity',head);
 const body=element('tbody',undefined,table);
 for(const [key,text] of Object.entries({minted:'Minted',transferred_in:'Transferred in',transferred_out:'Transferred out',bought:'Bought',sold:'Sold'})){
  const row=element('tr',undefined,body);element('th',text,row);const value=element('td',activity?.quantities?.[key]??'Unavailable',row);value.setAttribute('data-metric',key);
 }
 if(activity?.history_truncated)element('p','History read truncated by the saved row/byte budget; totals are scanned subsets only.',box);
 if(activity?.ambiguous_representations)element('p','Unresolved sale/transfer representations exist. Affected transfer totals are unavailable rather than double-counted; exact matched representations contribute only Bought/Sold. Review saved evidence separately.',box);
 if(activity?.excluded_evidence_count)element('p','Excluded archive evidence: '+activity.excluded_evidence_count+'. These archive-wide exclusions can leave gaps.',box);
 if(activity)element('p',activity.instruction,box);
 const controls=element('div',undefined,box);
 for(const [mode,title] of [['contents','Contents'],['transactions','Transactions']]){const button=element('button',title,controls);button.type='button';button.onclick=()=>{walletView={id:node.id,mode};walletSummary(node);render();};}
 if(walletView?.mode!=='contents')return;
 element('h3','Saved holdings',box);
 if(!holdings||holdings.status==='unavailable'){
  element('p',(holdings?.job_id!==null&&holdings?.job_id!==undefined?'Saved ownership list unavailable. ':'No saved ownership scan available for an owned NFT list. ')+(holdings?.reason||'Saved ownership data is missing.'),box);
 }else{
  element('p',holdings.status==='complete_saved_scan'?'Complete saved ownership scan — not a live current balance.':'Partial saved scan — not a complete owned list.',box);
  element('p','As of saved scan: '+(holdings.as_of||'timestamp unavailable')+' · Scan '+holdings.job_id,box);
  if(holdings.older_saved_scan)element('p','Showing an older completed saved scan; a newer scan exists. New scan progress: '+holdings.latest_scan?.state+' (scan '+holdings.latest_scan?.job_id+').',box);
  else if(holdings.latest_scan)element('p','Saved scan state: '+holdings.latest_scan.state,box);
  if(holdings.quantity_total==='0'&&holdings.status==='complete_saved_scan')element('p','Known zero holdings in this completed saved scan and its allowlist.',box);
  else element('p',holdings.quantity_total!==null?'Saved owned quantity: '+holdings.quantity_total:'Observed quantity only: '+(holdings.observed_quantity_total??'unavailable')+'; complete total unavailable.',box);
  const searchLabel=element('label','Search saved items by name, Token ID, or contract',box);searchLabel.className='holdings-search';
  const search=element('input',undefined,searchLabel);search.id='holdings-search';search.type='search';search.autocomplete='off';search.setAttribute('aria-controls','holdings-table');
  const count=element('p',undefined,box);count.id='holdings-count';count.className='holdings-count';count.setAttribute('role','status');
  const scroll=element('div',undefined,box);scroll.className='holdings-scroll';scroll.tabIndex=0;scroll.setAttribute('role','region');scroll.setAttribute('aria-label','Saved holdings; scroll for all available items');
  const inventory=element('table',undefined,scroll);inventory.id='holdings-table';inventory.className='holdings-table';inventory.setAttribute('aria-label','Saved holdings');
  const header=element('tr',undefined,element('thead',undefined,inventory));for(const title of ['NFT / name','Token ID','Contract']){const th=element('th',title,header);th.scope='col';}
  const rows=element('tbody',undefined,inventory),available=holdings.items||[],indexed=[];
  for(const item of available){
   const row=element('tr',undefined,rows),name=element('td',undefined,row);element('span',item.title||'Title unavailable',name);
   element('td',item.token_id,row);element('td',item.contract,row);
   indexed.push({row,text:[item.title,item.token_id,item.contract].map(value=>String(value??'')).join('\n').toLowerCase()});
  }
  const filter=()=>{const query=search.value.trim().toLowerCase();let matched=0;for(const entry of indexed){entry.row.hidden=!entry.text.includes(query);if(!entry.row.hidden)matched++;}count.textContent=matched+' / '+available.length+' saved items';scroll.scrollTop=0;};
  search.addEventListener('input',filter);filter();

  if(holdings.excluded_rows)element('p','Excluded saved ownership rows: '+holdings.excluded_rows+'. The list is incomplete.',box);
 }
 if(holdings?.truncated||holdings?.jobs_truncated)element('p','Saved ownership read truncated by a row, job, or byte budget. The list is incomplete.',box);
 if(holdings?.oversized_job_metadata)element('p','Oversized saved scan metadata excluded: '+holdings.oversized_job_metadata+'. Saved ownership data may be unavailable.',box);
 element('p','Ownership is based only on saved ownership discoveries, never receipts or net flows. '+(holdings?.instruction||'Run wallet current-holdings discovery separately to save missing ownership data. This graph starts no collection.'),box);
}

function eventEvidence(event,parent){const box=element('details',undefined,parent);element('summary','Saved provenance / execution details',box);element('p','Event ID: '+event.id,box);element('p','Execution: '+(typeof event.execution==='object'?JSON.stringify(event.execution):event.execution||'unknown'),box);for(const e of event.evidence||[])element('p','Provider: '+e.provider+' · Evidence ID: '+e.id+' · Digest: '+e.digest,box);}
function draw(){
 const root=$('graph'),list=$('connections');root.replaceChildren();list.replaceChildren();if(!graph)return;
 const defs=svg('defs',{},root);for(const [kind,color] of Object.entries(colors)){const m=svg('marker',{id:'arrow-'+kind,viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:8,markerHeight:8,orient:'auto-start-reverse',markerUnits:'userSpaceOnUse'},defs);svg('path',{d:'M 0 0 L 10 5 L 0 10 z',fill:color},m);}
 const edges=graph.edges.filter(e=>enabled(e.kind));
 for(const edge of edges){
  const a=positions.get(edge.source),b=positions.get(edge.target);if(!a||!b)continue;
  const siblings=edges.filter(e=>[e.source,e.target].sort().join('|')===[edge.source,edge.target].sort().join('|')).sort((a,b)=>a.id.localeCompare(b.id));
  const slot=siblings.findIndex(e=>e.id===edge.id)-(siblings.length-1)/2;
  const len=Math.hypot(b.x-a.x,b.y-a.y)||1,dx=(b.x-a.x)/len,dy=(b.y-a.y)/len;
  const orientation=edge.source<edge.target?1:-1,offset=slot*54*orientation;
  const cx=(a.x+b.x)/2-dy*offset,cy=(a.y+b.y)/2+dx*offset;
  let d=`M ${a.x+dx*24} ${a.y+dy*24} Q ${cx} ${cy} ${b.x-dx*30} ${b.y-dy*30}`;
  if(edge.source===edge.target)d=`M ${a.x-18} ${a.y-18} C ${a.x-110} ${a.y-130} ${a.x+110} ${a.y-130} ${a.x+18} ${a.y-18}`;
  const active=!selected||edge.event_ids.some(id=>selected.ids.has(id));
  const description=label(edge.source)+' → '+label(edge.target)+' · '+edge.kind+' · '+edge.count+' events · quantity sum '+edge.quantity+' · '+edge.asset_count+' assets';
  const g=svg('g',{},root),p=svg('path',{d,fill:'none',stroke:colors[edge.kind],'stroke-width':active?3:1,opacity:active?1:.23,'marker-end':'url(#arrow-'+edge.kind+')',class:'edge','aria-label':description},g);
  activate(p,()=>choose(edge.event_ids,description));const title=svg('title',{},p);title.textContent=description;
  const t=svg('text',{x:(a.x+b.x)/2-dy*offset/2,y:(a.y+b.y)/2+dx*offset/2-7,'text-anchor':'middle',opacity:active?1:.35},g);t.textContent=edge.kind+' · '+edge.count;
  const li=element('li',undefined,list),button=element('button',description,li);button.type='button';button.onclick=()=>choose(edge.event_ids,description);
 }
 for(const node of graph.nodes){
  const isolated=island(node),description=label(node.id)+(isolated?'; Isolated wallet — no saved link to other selected wallets; incomplete history does not prove no relationship':'')+'; select incident events and saved wallet summary';
  const p=positions.get(node.id),g=svg('g',{transform:`translate(${p.x},${p.y})`,class:'node','aria-label':description},root);
  const tooltip=svg('title',{},g);tooltip.textContent=description;
  svg('circle',{r:22,fill:node.kind==='mint'?'#574585':isolated?'#ffe066':'#163f6c',stroke:'#b6cee9','stroke-width':2},g);
  const t=svg('text',{y:43,'text-anchor':'middle',style:'pointer-events:auto'},g);t.textContent=label(node.id)+(isolated?' · isolated':'');
  activate(g,()=>selectWallet(node.id));
  g.addEventListener('click',event=>{if(event.detail)event.stopImmediatePropagation();},true);
  g.addEventListener('pointerdown',event=>{if(event.button!==0)return;event.stopPropagation();drag={node:node.id,x:event.clientX,y:event.clientY,startX:event.clientX,startY:event.clientY,moved:false,pointer:event.pointerId};$('graph').setPointerCapture(event.pointerId);});
 }
}
function render(){
 draw();const body=$('events');body.replaceChildren();const panel=body.closest('section');panel.hidden=!!walletView&&walletView.mode!=='transactions';
 const tx=walletView?.mode==='transactions'?graph?.wallet_summaries?.[walletView.id]?.transactions:null;
 const events=walletView?(tx?.events||[]):graph?.events.filter(allowedEvent)||[];
 panel.querySelector('h2').textContent=walletView?'Saved wallet transactions: '+label(walletView.id):'Selected saved NFT events';
 if(walletView&&!tx){$('event-count').textContent='Choose Transactions to display saved wallet activity.';return;}
 $('event-count').textContent=walletView?((!tx||tx.status==='unavailable')?'Unavailable: no saved transaction data. Run Wallet / Transfers or NFT History separately; nothing starts here.':tx.returned_count+' returned / '+tx.observed_total+' observed saved events. '+tx.scope+(tx.truncated?' · Truncated details; not complete.':' · Not lifetime completeness.')):events.length+' displayed events. Filters affect this response only.';
 if(walletView&&!events.length)return;
 for(const e of events){const tr=element('tr',undefined,body);element('td',e.date,tr);element('td',e.kind+' / '+(typeof e.execution==='string'?e.execution:e.execution?.classification||'unknown'),tr);element('td',e.from_label||label(e.from),tr);element('td',e.to_label||label(e.to),tr);const td=element('td',undefined,tr);td.className='nft';element('strong',e.title||'Title unavailable',td);element('p','Contract: '+e.contract,td);element('p','Token ID: '+e.token_id,td);eventEvidence(e,td);element('td',e.quantity,tr);element('td',e.price_text||'not recorded',tr);}
 if(!events.length)element('td',graph?.events.length?'No events match this selection / type filter.':'No saved eligible evidence for these wallets. Isolated selected wallets remain visible.',element('tr',undefined,body)).colSpan=7;
}
function accept(data){
 if(!data||!Array.isArray(data.nodes)||!Array.isArray(data.edges)||!Array.isArray(data.events)||!Array.isArray(data.patterns)||!data.coverage||!Array.isArray(data.warnings)||data.network_calls!==0||data.notifications!==0||data.llm_tokens!==0)throw new Error('Invalid saved graph response; reload or inspect the local service.');
 clearWalletSummary();graph=data;selected=null;positions=new Map();const wallets=data.nodes.filter(n=>n.kind!=='mint');wallets.forEach((n,i)=>{const angle=-Math.PI/2+2*Math.PI*i/Math.max(wallets.length,1);positions.set(n.id,{x:520+300*Math.cos(angle),y:350+260*Math.sin(angle)});});data.nodes.filter(n=>n.kind==='mint').forEach(n=>positions.set(n.id,{x:75,y:90}));fit();
 $('status').className='';$('status').textContent='Saved graph ready · '+wallets.length+' wallets · '+data.events.length+' events · network calls 0 · notifications 0 · model tokens 0';
 const c=data.coverage;$('coverage').textContent='Coverage: '+(c.summary||'Saved evidence only; completeness unknown')+' · Snapshot: '+(c.snapshot_at||'unknown')+' · Assets: '+c.asset_count+' · Events: '+c.event_count+' · Selected wallets: '+c.selected_wallet_count;
 $('warnings').replaceChildren();for(const w of data.warnings)element('li',w,$('warnings'));
 $('patterns').replaceChildren();for(const pattern of data.patterns){const li=element('li',undefined,$('patterns'));const b=element('button',pattern.title,li);b.type='button';b.onclick=()=>choose(pattern.event_ids,'Review lead: '+pattern.title,pattern);}
 if(!data.patterns.length)element('li','No review leads found in this saved response. This does not establish absence of a pattern.',$('patterns'));
 $('selection').textContent='All saved events. Select a node, connection, or review lead.';$('details').replaceChildren();render();
}
$('graph-form').addEventListener('submit',async event=>{
 event.preventDefault();const wallets=$('wallets').value,inputs=wallets.split(/[\n,]/).map(s=>s.trim()).filter(Boolean);
 clearWalletSummary('Reading saved evidence…');graph=null;selected=null;render();$('patterns').replaceChildren();$('details').replaceChildren();$('warnings').replaceChildren();$('coverage').textContent='';$('selection').textContent='Reading saved evidence…';
 if(inputs.length<2||inputs.length>50){clearWalletSummary('No graph loaded. Choose 2–50 wallets.');$('selection').textContent='No graph loaded.';$('status').textContent='Choose 2–50 full wallets or unique saved aliases.';return;}
 const csrf=$('csrf').value;$('generate').disabled=true;$('status').className='';$('status').textContent='Reading saved SQL evidence… No marketplace calls.';
 try{const response=await fetch('/wallet-graph-data',{method:'POST',headers:{'Content-Type':'application/json','X-MTD-ANALYZER-CSRF':csrf},body:JSON.stringify({wallets,csrf}),cache:'no-store'});const text=await response.text();if(!response.headers.get('Content-Type')?.includes('application/json'))throw new Error('Saved graph endpoint returned HTTP '+response.status+' with a non-JSON content type. Reload the form.');let data;try{data=JSON.parse(text);}catch(_){throw new Error('Saved graph endpoint returned invalid JSON (HTTP '+response.status+').');}if(!response.ok)throw new Error(data.error||'Saved graph request failed: HTTP '+response.status);accept(data);}catch(error){clearWalletSummary('No graph loaded. Saved wallet summary and holdings cleared. Correct input or retry the saved query.');$('status').className='error';$('status').textContent=error.message;$('selection').textContent='No graph loaded. Correct input or retry the saved query.';}finally{$('generate').disabled=false;}
});
for(const checkbox of document.querySelectorAll('[data-kind]'))checkbox.addEventListener('change',render);
$('fit').onclick=fit;$('all').onclick=()=>{clearWalletSummary();selected=null;$('selection').textContent='All saved events.';$('details').replaceChildren();render();};
$('graph').addEventListener('pointerdown',e=>{if(e.button===0){drag={x:e.clientX,y:e.clientY,startX:e.clientX,startY:e.clientY,moved:false,pointer:e.pointerId};$('graph').setPointerCapture(e.pointerId);}});
$('graph').addEventListener('pointermove',e=>{if(!drag||drag.pointer!==e.pointerId)return;if(Math.hypot(e.clientX-drag.startX,e.clientY-drag.startY)>5)drag.moved=true;if(!drag.moved)return;const r=$('graph').getBoundingClientRect(),scale=Math.max(view[2]/r.width,view[3]/r.height),dx=(e.clientX-drag.x)*scale,dy=(e.clientY-drag.y)*scale;if(drag.node){const p=positions.get(drag.node);p.x+=dx;p.y+=dy;draw();}else{view[0]-=dx;view[1]-=dy;setView();}drag.x=e.clientX;drag.y=e.clientY;});
$('graph').addEventListener('pointerup',e=>{if(!drag||drag.pointer!==e.pointerId)return;const ended=drag;drag=null;if(ended.node&&!ended.moved&&Math.hypot(e.clientX-ended.startX,e.clientY-ended.startY)<=5)selectWallet(ended.node);});
for(const type of ['pointercancel','lostpointercapture'])$('graph').addEventListener(type,()=>{drag=null;});
$('graph').addEventListener('wheel',e=>{e.preventDefault();const factor=e.deltaY>0?1.15:.87,w=Math.max(250,Math.min(4000,view[2]*factor)),h=w*.7;view=[view[0]+(view[2]-w)/2,view[1]+(view[3]-h)/2,w,h];setView();},{passive:false});
</script></body></html>'''
