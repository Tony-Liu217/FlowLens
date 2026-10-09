import {request} from './api.js';
const $=s=>document.querySelector(s);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const amount=(v,c='')=>`${Number(v||0).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:8})} ${c}`;
const names={transaction_at:'交易时间',booking_at:'银行记账时间',counterparty:'交易对象',description:'商品／说明',memo:'附言',payment_method:'支付方式',account:'资金账户',counterparty_account:'对方账户',counterparty_combined:'对方账户与名称',counterparty_bank:'对方银行',source_record_id:'来源流水号',merchant_order_no:'商户订单号',trade_type:'交易类型',original_category:'来源分类',transaction_status:'交易状态',raw_status:'来源状态',balance:'账户余额',balance_reliable:'余额可靠性'};
const kind={duplicate:'重复导出',payment:'同笔收付／合付',refund:'退款关联'};
const directionLabel=d=>({in:'流入（收入）',out:'流出（支出）',none:'不计收支'}[d]||'方向待核验');
const pairTitle=p=>p.kind==='payment'?(p.records.every(r=>r.refund)?'同笔退款入账':p.records.every(r=>(r.effective_direction||r.direction)==='in')?'同笔收入':'同笔支出／合付'):kind[p.kind];
const verdict={same:'AI 倾向同笔，仍需核对依据',different:'AI 倾向不是同笔',uncertain:'AI 尚无法确定'};

export function createAnalysis({context,hasData,isActive,toast,openSource}){
 let summary,detail,txPage=1,pairPage=1,generation=0,poll,loading=false;
 const query=(section,extra={})=>({...context(),section,...extra});
 async function render(){
  const version=++generation;clearTimeout(poll);
  if(!hasData()){$('#analysis-body').hidden=true;$('#analysis-empty').hidden=false;return;}
  $('#analysis-empty').hidden=true;$('#analysis-body').hidden=false;
  $('#analysis-loading').hidden=false;$('#analysis-loading').textContent='正在按当前账本整理流水与来源信息…';
  try{
   const ctx=context(),next=await request('/api/analysis',{query:{...ctx}});
   if(version!==generation||JSON.stringify(ctx)!==JSON.stringify(context()))return;
   summary=next;
   $('#analysis-counts').textContent=`${next.source_count} 条可用来源记录 → ${next.transaction_count} 笔流水 · 已采用 ${next.relation_count} 组关系`;
   $('#analysis-basis').textContent=next.basis+(next.partial?' 仍有读取缺口，本页覆盖已可用部分。':'')+(next.truncated?' 配对搜索未完整覆盖，相关自动采用已暂停。':'')+(next.stale_decisions.length?` ${next.stale_decisions.length} 项旧决定因输入变化需重新检查。`:'');
   if(next.restart_required)$('#analysis-basis').textContent+=' 检测到配对规则更新：请关闭启动窗口并重新启动软件，页面刷新不会加载新的后端规则。';
   $('#analysis-totals').innerHTML=Object.entries(next.totals).map(([currency,t])=>`<article class="card analysis-total"><span class="eyebrow">${esc(currency)} · 去重后流水</span><div class="analysis-numbers"><div><small>流出</small><strong>${esc(amount(t.out))}</strong></div><div><small>流入</small><strong>${esc(amount(t.in))}</strong></div><div><small>其中明确退款</small><strong>${esc(amount(t.refund))}</strong></div></div></article>`).join('')||'<p class="muted">当前没有可汇总的有效收付。</p>';
   $('#analysis-pair-count').textContent=`待确认配对方案 ${next.pending_pairs} 个 · 方案数不等于交易笔数`;
   const job=next.jobs[0];$('#analysis-ai-status').textContent=job?`${{running:'分析中',completed:'分析完成',failed:'分析未完成',interrupted:'上次分析中断',cancelled:'已取消',stale:'输入已变化'}[job.status]||job.status} · ${job.completed} / ${job.total} 项 · ${job.message}`:'AI 仅辅助去重配对，由你点击开始；当前没有外发任务。';
   if(job?.status==='failed'&&!job.error)$('#analysis-ai-status').textContent+=' 这是旧版任务，未保存具体错误；重启新版服务后再次分析可记录失败原因。';
   $('#analysis-ai-progress').hidden=job?.status!=='running';if(job){$('#analysis-ai-progress').max=Math.max(job.total,1);$('#analysis-ai-progress').value=job.completed;}
   $('#analysis-ai-cancel').hidden=job?.status!=='running';$('#analysis-ai-start').disabled=job?.status==='running';$('#analysis-ai-sample').disabled=job?.status==='running';
   if(job?.sample)$('#analysis-ai-status').textContent='小批试运行 · '+$('#analysis-ai-status').textContent;
   await rows();if($('#analysis-pairs').open)await pairs();
   if(job?.status==='running')poll=setTimeout(()=>{if(isActive())render().catch(e=>toast(e.message));},2500);
  }finally{if(version===generation)$('#analysis-loading').hidden=true;}
 }
 async function rows(){
  const version=generation,ctx=JSON.stringify(context()),page=txPage;
  const result=await request('/api/analysis',{query:query('transactions',{page:txPage,q:$('#analysis-search').value})});
  if(version!==generation||ctx!==JSON.stringify(context())||page!==txPage)return;
  const last=Math.max(1,Math.ceil(result.total/30));if(txPage>last){txPage=last;return rows();}
  $('#analysis-rows').innerHTML=result.items.map(t=>`<tr><td>${esc(t.transaction_at||t.booking_at||'日期待定')}<small>${t.source_ids.length} 条来源</small></td><td>${esc(t.counterparty||'对象未明确')}<small>${esc(t.description||'')}</small></td><td>${esc(amount(t.amount,t.currency))}<small>${t.direction==='out'?'流出':t.direction==='in'?'流入':'不计收支'}${!t.included?' · 未计入收付汇总':''}${t.refund?` · 退款${t.refund_linked?'已关联':'／原付款未关联'}`:''}</small></td><td><button class="button small" data-flow-detail="${esc(t.id)}">详情与来源</button></td></tr>`).join('');
  $('#analysis-page').textContent=`共 ${result.total} 笔 · 第 ${txPage} / ${Math.max(1,Math.ceil(result.total/30))} 页`;
  $('#analysis-prev').disabled=txPage<=1;$('#analysis-next').disabled=txPage*30>=result.total;
 }
 async function pairs(){
  const version=generation,ctx=JSON.stringify(context()),page=pairPage;
  const result=await request('/api/analysis',{query:query('candidates',{page:pairPage,page_size:10,show_all:$('#analysis-pairs-all').checked})});
  if(version!==generation||ctx!==JSON.stringify(context())||page!==pairPage)return;
  const last=Math.max(1,Math.ceil(result.total/10));if(pairPage>last){pairPage=last;return pairs();}
  $('#analysis-pair-list').innerHTML=result.items.map(p=>`<article class="analysis-pair"><strong>候选方案 · ${esc(pairTitle(p))} · ${esc(amount(p.amount,p.currency))}</strong><p>${esc(p.evidence.join('；'))}</p>${p.review_reason?`<p class="basis">未自动采用：${esc(p.review_reason)}</p>`:''}${p.ai_verdict?`<p class="basis">${esc(verdict[p.ai_verdict])}</p>`:''}<ul>${p.records.map(r=>`<li><strong class="pair-direction">${esc(directionLabel(r.effective_direction||r.direction))}</strong> ${esc(r.source)} · ${esc(r.transaction_at||r.booking_at)} · ${esc(r.counterparty||r.description||'对象未明确')} · ${esc(amount(r.amount,r.currency))}<small class="pair-record-context">${esc(r.source_label||'')}${(r.transaction_at_precision||r.booking_at_precision)==='day'?' · 仅有日期，无具体时间':''} · 类型：${esc(r.trade_type||r.description||'未提供')}${r.refund?' · 退款入账':''}${r.effective_direction&&r.effective_direction!==r.direction?` · 原账单方向：${esc(directionLabel(r.direction))}`:''}${r.raw_status?` · 状态：${esc(r.raw_status)}`:''}</small></li>`).join('')}</ul><div class="analysis-buttons"><button class="button small teal" data-pair="${p.id}" data-decision="accept">确认关联</button><button class="button small" data-pair="${p.id}" data-decision="reject">不是同笔／不关联</button><button class="button small" data-pair="${p.id}" data-decision="defer">暂不处理</button><small>${esc({defer:'已暂不处理',reject:'已拒绝',conflict:'来源已被其他关系占用',stale:'来源已变化，请重新检查'}[p.decision]||'')}</small></div></article>`).join('')||'<p class="empty-state">此范围没有待确认配对。</p>';
  $('#analysis-pair-page').textContent=`${result.total} 项 · 第 ${pairPage} 页`;
  $('#analysis-pair-prev').disabled=pairPage<=1;$('#analysis-pair-next').disabled=pairPage*10>=result.total;
 }
 async function mutate(action,extra={},version=summary){
  if(loading)return;loading=true;
  try{await request('/api/analysis/action',{body:{...context(),fingerprint:version.fingerprint,expected_revision:version.revision,request_id:crypto.randomUUID(),action,...extra}});await render();toast('已保存，去重结果与来源信息已更新。');}
  finally{loading=false;}
 }
 async function showDetail(id){
  const version=generation,ctx=JSON.stringify(context());
  const next=await request('/api/analysis',{query:query('detail',{id})});
  if(version!==generation||ctx!==JSON.stringify(context()))return;
  detail=next;const tx=detail.transaction;
  $('#flow-detail-title').textContent=`${tx.counterparty||'交易详情'} · ${directionLabel(tx.direction)} · ${amount(tx.amount,tx.currency)}`;
  $('#flow-display-counterparty').value=tx.counterparty||'';$('#flow-display-description').value=tx.description||'';
  $('#flow-fields').innerHTML=Object.entries(tx.fields).filter(([,v])=>v.alternatives.length).map(([field,v])=>`<tr><th>${esc(names[field]||field)}</th><td>${v.alternatives.map(a=>`<div>${esc(a.value)}<small>${a.sources.map(id=>esc(tx.sources.find(r=>r.book_record_id===id)?.source||'来源')).join('、')}</small></div>`).join('')}</td></tr>`).join('');
  $('#flow-sources').innerHTML=tx.sources.map((r,i)=>`<button class="button small" data-flow-source="${i}">${esc(r.source)} · 查看原始读取</button>`).join('');
  $('#flow-relations').innerHTML=detail.relations.map(p=>`<div class="analysis-pair"><strong>${esc(kind[p.kind])} · ${p.decision==='automatic'?'系统采用':'人工确认'}</strong><p>${esc(p.evidence.join('；'))}</p><button class="button small" data-flow-split="${p.id}">${p.kind==='refund'?'解除退款关联':'拆开这组关系'}</button></div>`).join('')||'<p class="muted">这笔流水尚未与其他来源合并。</p>';
  $('#flow-detail-error').hidden=true;$('#flow-detail').showModal();
 }
 const on=(selector,event,fn)=>$(selector).addEventListener(event,async e=>{try{await fn(e);}catch(error){toast(error.message);}});
 on('#analysis-refresh','click',()=>render());
 on('#analysis-search-form','submit',e=>{e.preventDefault();txPage=1;return rows();});
 on('#analysis-prev','click',()=>{txPage--;return rows();});on('#analysis-next','click',()=>{txPage++;return rows();});
 on('#analysis-pairs','toggle',()=>$('#analysis-pairs').open&&summary?pairs():null);
 on('#analysis-pairs-all','change',()=>{pairPage=1;return pairs();});
 on('#analysis-pair-prev','click',()=>{pairPage--;return pairs();});on('#analysis-pair-next','click',()=>{pairPage++;return pairs();});
 on('#analysis-rows','click',e=>{const b=e.target.closest('[data-flow-detail]');if(b)return showDetail(b.dataset.flowDetail);});
 on('#analysis-pair-list','click',e=>{const b=e.target.closest('[data-pair]');if(b)return mutate(b.dataset.decision,{relation_id:b.dataset.pair});});
 on('#flow-sources','click',e=>{const b=e.target.closest('[data-flow-source]');if(b){const row=detail.transaction.sources[Number(b.dataset.flowSource)];$('#flow-detail').close();return openSource(row);}});
 on('#flow-relations','click',async e=>{const b=e.target.closest('[data-flow-split]');if(b){await mutate('reject',{relation_id:b.dataset.flowSplit},detail);$('#flow-detail').close();}});
 on('#flow-display-form','submit',async e=>{e.preventDefault();await mutate('display',{relation_id:detail.transaction.id,fields:{counterparty:$('#flow-display-counterparty').value,description:$('#flow-display-description').value}},detail);$('#flow-detail').close();});
 on('#analysis-history','click',async()=>{
  const result=await request('/api/analysis',{query:query('history')});summary={...summary,...result};
  const undone=new Set(result.history.filter(e=>e.action==='undo').map(e=>e.target));
  $('#flow-history-content').innerHTML=[...result.history].reverse().map(e=>`<div class="history-event"><strong>第 ${e.revision} 次 · ${esc({accept:'确认关联',reject:'拒绝／拆开',defer:'暂不处理',display:'修正显示信息',undo:'撤销决定'}[e.action])}</strong><p>${esc(e.created)}</p>${e.action!=='undo'&&!undone.has(e.revision)?`<button class="button small" data-flow-undo="${e.revision}">撤销这次决定</button>`:''}</div>`).join('')||'<p>尚无人工配对决定。</p>';
  $('#flow-history').showModal();
 });
 on('#flow-history-content','click',async e=>{const b=e.target.closest('[data-flow-undo]');if(b){await mutate('undo',{target:Number(b.dataset.flowUndo)});$('#flow-history').close();}});
 on('#analysis-export','click',async()=>{const blob=await request('/api/analysis',{query:query('export'),blob:true});const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='FlowLens-去重流水与依据.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),10000);});
 on('#analysis-ai-preview','click',async()=>{
  const preview=await request('/api/analysis',{query:query('privacy')});$('#flow-privacy-content').textContent=JSON.stringify(preview.candidates,null,2);$('#flow-privacy-count').textContent=`DeepSeek V4.1 Flash · ${preview.count} 个候选 · 无总调用次数上限。以下是实际发送的数据副本；原文件及截图不发送。`;
  $('#flow-privacy').showModal();
 });
 async function startAI(sample=false){
  const key=$('#analysis-api-key').value;
  try{await request('/api/analysis/ai',{body:{...context(),fingerprint:summary.fingerprint,expected_revision:summary.revision,api_key:key,action:'start',sample}});await render();}
  finally{$('#analysis-api-key').value='';}
 }
 on('#analysis-ai-start','click',()=>startAI());
 on('#analysis-ai-sample','click',()=>startAI(true));
 on('#analysis-ai-cancel','click',async()=>{await request('/api/analysis/ai',{body:{...context(),action:'cancel',job_id:summary.jobs[0].id}});await render();});
 return {render,reset(){txPage=1;pairPage=1;generation++;clearTimeout(poll);}};
}
