import {request, filePayload} from './api.js';
import {renderProgress} from './progress.js';
import {createGuide} from './guide.js';

const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = (value, currency = '') => value === null || value === undefined ? '待确定' : `${Number(value).toLocaleString('zh-CN', {minimumFractionDigits:2, maximumFractionDigits:8})} ${currency}`;
const direction = {in:'收入方向',out:'支出方向',none:'不计收支'};
const status = {posted:'已成功 / 入账',pending:'进行中',void:'关闭 / 失败',unknown:'未知'};
let state, overview, activePage = 'overview', page = 1, rows = [], editing, pageTask, nameMode = 'create';
let busy = false, timer, toastTimer, objectUrls = [], recordsRequest = 0;
let reviewSession = null, recordSaving = false, detailGeneration = 0, evidenceGeneration = 0;
let deleteContext = null;
const context = () => ({book_id:state.book_id, catalog_revision:state.catalog_revision});
const batchContext = () => ({...context(), batch_id:state.batch_id, workspace_id:state.ledger?.workspace_id});
const currentBook = () => state?.books.find(b => b.id === state.book_id);

function toast(message) {
  clearTimeout(toastTimer); $('#toast').textContent = message; $('#toast').hidden = false;
  toastTimer = setTimeout(() => $('#toast').hidden = true, 7000);
}
function notice(message) { $('#notice').textContent = message; $('#notice').hidden = !message; }
function handle(fn) { return async event => { try { await fn(event); } catch (error) { toast(error.message); if (!state) notice(error.message); } }; }
function setBusy(value) {
  busy = value;
  $('#choose-files').disabled = value || !state?.book_id;
  $('#create-book').disabled = value;
  $('#rename-book').disabled = value || !state?.book_id;
  $('#delete-book').disabled = value || !state?.book_id;
  $('#recycle-books').disabled = value;
  $('#book-select').disabled = value;
  $('#review-batch').disabled = value;
  $('#add-record').disabled = value || !state?.ledger;
  $('#export').disabled = value || !currentBook()?.batch_count;
}
function readState(next) {
  state = next;
  const book = currentBook();
  $('#book-select').innerHTML = (state.books.filter(b => !b.deleted).map(b => `<option value="${escape(b.id)}">${escape(b.name)}</option>`).join('') || '<option value="">尚无账本</option>');
  if(!state.book_id && state.books.some(b=>!b.deleted)) $('#book-select').insertAdjacentHTML('afterbegin','<option value="" disabled>请选择账本</option>');
  $('#book-select').value = state.book_id || '';
  $('#recycle-books').textContent = `回收站${state.books.some(b=>b.deleted)?'（'+state.books.filter(b=>b.deleted).length+'）':''}`;
  $('#book-caption').textContent = book ? `${book.name} · ${book.batch_count} 个导入批次 · 自动保存于本机` : '从自己的多平台账单开始';
  $('#import-hint').textContent = book ? `将作为新批次追加到「${book.name}」，原记录和修订保留。` : '先新建一个账本，再选择文件。';
  $('#nav-pending').textContent = (book?.summary.pending || 0) + (book?.summary.page_pending || 0);
  $('#review-batch').innerHTML = book?.batches.map(b => `<option value="${escape(b.id)}">${escape(b.name)}${b.error ? ' · 无法读取' : ` · ${b.summary.pending} 条 / ${b.summary.page_pending} 页待核验`}</option>`).join('') || '<option value="">暂无批次</option>';
  $('#review-batch').value = state.batch_id || '';
  const job = state.job;
  $('#job-status').hidden = job.status === 'idle' || Boolean(job.book_id && job.book_id !== state.book_id);
  renderProgress($('#job-status'), job);
  setBusy(job.status === 'running');
  clearTimeout(timer);
  if (job.status === 'running') timer = setTimeout(pollProgress, 1500);
}
async function pollProgress() {
  try {await refresh();}
  catch(error) {notice('暂时无法更新进度，正在重试；也可点击刷新。'+error.message);timer=setTimeout(pollProgress,3000);}
}
async function refresh() {
  readState(await request('/api/state'));
  overview = await request('/api/overview', {query: context()});
  renderOverview(); renderReview();
  if (activePage === 'records') await renderRecords();
  if (!busy) guide();
}
function renderOverview() {
  const book = currentBook(), summary = book?.summary;
  const coverage = overview.range ? `${overview.range.start} 至 ${overview.range.end} · ${overview.batch_count} 个批次` : '尚无可用日期范围';
  $('#coverage').textContent = coverage;
  for (const [id, key] of [['total','total'],['eligible','eligible'],['pending','pending'],['pages','page_pending']]) $('#kpi-'+id).textContent = summary ? summary[key] : '—';
  const warnings = [...state.warnings];
  if (state.open_error) warnings.push(state.open_error);
  if (book?.unreadable_batches) warnings.push(`${book.unreadable_batches} 个批次无法读取，记录数未知。下方汇总仅覆盖可读部分。`);
  if (overview.status === 'partial') warnings.push(`当前为部分可用数据：${summary.pending} 条记录、${summary.page_pending} 页待核验，${summary.blocking_issues} 项读取问题。可先查看已读取部分。`);
  notice(warnings.join('\n'));
  $('#review-shortcut').disabled = busy || !book?.batch_count;
  const sources = overview.sources;
  $('#sources').innerHTML = sources.length ? `<div class="table-wrap"><table><thead><tr><th>来源 / 币种</th><th>可用记录</th><th>支出方向金额</th><th>收入方向金额</th><th>不计收支金额</th><th>交易状态分布</th></tr></thead><tbody>${sources.map(s => `<tr><td>${escape(s.source)}<small>${escape(s.currency)}</small></td><td>${s.count}</td><td>${escape(money(s.out,s.currency))}</td><td>${escape(money(s.in,s.currency))}</td><td>${escape(money(s.none,s.currency))}</td><td>${Object.entries(s.statuses).map(([key,n])=>`${escape(status[key] || key)} ${n} 条`).join('<br>')}</td></tr>`).join('')}</tbody></table></div>` : '<div class="empty-state">暂无可用记录。导入后会在这里按来源和币种展示；需要核验的内容会单独提示。</div>';
  $('#currency-note').hidden = !overview.currency_default_count;
  $('#currency-note').textContent = `${overview.currency_default_count || 0} 条原记录使用了默认人民币提示；原字段保留，可在明细中核对与纠正。已识别外币分别汇总。`;
  $('#batches').innerHTML = book?.batches.length ? book.batches.map(b => `<div class="batch-row"><div><strong>${escape(b.name)}</strong><p>${escape((b.filenames || []).join('、'))}</p><p>${b.error ? escape(b.error) : `${b.summary.total} 条记录 · ${b.summary.eligible} 条可用 · ${b.summary.pending} 条 / ${b.summary.page_pending} 页待核验 · ${b.summary.blocking_issues} 项读取问题`}</p></div><button class="button small" data-batch="${escape(b.id)}">查看本批</button></div>`).join('') : '<div class="empty-state">还没有导入批次。每次追加都会独立保存。</div>';
  const previousSource = $('#source-filter').value;
  $('#source-filter').innerHTML = '<option value="">所有来源</option>' + [...new Set(sources.map(s=>s.source))].map(s=>`<option value="${escape(s)}">${escape(s)}</option>`).join('');
  $('#source-filter').value = previousSource;
}
async function showPage(name) {
  activePage = name;
  $$('.workspace').forEach(el => el.hidden = el.id !== 'page-'+name);
  $$('[data-page]').forEach(el => {el.classList.toggle('active',el.dataset.page === name);el.setAttribute('aria-current',el.dataset.page === name ? 'page' : 'false');});
  $('#page-title').textContent = {overview:'账单导入概览',records:'整本来源明细',review:'读取核验'}[name];
  if (name === 'records') await renderRecords();
  if (name === 'review') renderReview();
}
function reviewLabel(row) { return row.review_status === 'excluded' ? '已排除' : row.page_review_pending ? '整页待核验' : row.review_status === 'confirmed' ? '人工已核验' : row.review_status === 'pending' ? '需要核验' : '读取可用'; }
async function renderRecords() {
  const requestId=++recordsRequest;
  const filters = Object.fromEntries(new FormData($('#filters')));
  $('#records-body').innerHTML='<tr><td colspan="7" class="empty-state">正在读取整本明细…</td></tr>';
  const data = await request('/api/records', {query:{...context(),...filters,page,page_size:50}});
  if(requestId!==recordsRequest) return;
  rows = data.records;
  $('#records-body').innerHTML = rows.map((r,i)=>`<tr><td>${escape(r.transaction_at || r.booking_at || '日期待定')}<small>${escape(r.source)}</small></td><td>${escape(r.counterparty || '—')}<small>${escape(r.description || '—')}</small></td><td>${escape(money(r.amount,r.currency))}<small>${escape(direction[r.direction] || '方向待定')}</small></td><td>${escape(status[r.transaction_status] || '未知')}</td><td><span class="status-pill ${r.eligible_for_processing ? '' : 'warn'}">${reviewLabel(r)}</span></td><td>${escape(r.batch_name)}<small>${escape(r.filename)}</small></td><td><button class="button small" data-record="${i}">查看 / 核验</button></td></tr>`).join('') || '<tr><td colspan="7" class="empty-state">没有符合条件的记录。</td></tr>';
  $('#records-count').textContent = `共 ${data.total} 条 · 第 ${page} / ${Math.max(1,Math.ceil(data.total/50))} 页${data.unreadable_batches.length ? ' · 部分批次无法读取' : ''}`;
  $('#previous').disabled = page <= 1; $('#next').disabled = page*50 >= data.total;
}
function renderReview() {
  const ledger = state.ledger;
  $('#history').disabled = !ledger || busy;
  if (!ledger) { $('#review-content').innerHTML = `<div class="empty-state">${escape(state.open_error || '先导入一份账单，再查看需要核验的内容。')}</div>`;return; }
  const pending = ledger.records.filter(r=>r.review_status === 'pending');
  const tasks = ledger.tasks.filter(t=>t.scope === 'page' && t.status === 'pending');
  const issues = ledger.blockers.filter(i=>!pending.some(r=>r.record_id===i.record_id) && !tasks.some(t=>t.file_id===i.file_id && t.page===i.page));
  const receipts = `<details><summary>本批 ${ledger.files.length} 个文件的读取结果</summary>${ledger.files.map(f=>`<p>${escape(f.filename)} · ${escape({completed:'读取完成',partial:'部分读取，需要核验',failed:'读取失败',duplicate_file_skipped:'内容与本批其他文件相同，已跳过'}[f.status] || f.status)}</p>`).join('')}</details>`;
  $('#review-content').innerHTML = issues.map(i=>`<div class="issue"><strong>文件读取问题</strong><p>${escape(i.message)}</p><small>${escape(['HEADER_UNRECOGNIZED','UNMAPPED_TEXT','MAPPING_INVALID'].includes(i.code) ? '未识别模板需提供映射或新增适配，不能靠补录少量记录消除整个文件的失败。' : '请对照来源处理相关记录或页面；完整导出前需解决读取缺口。')}</small></div>`).join('') +
    tasks.map(t=>`<div class="review-row"><div><strong>第 ${escape(t.page)} 页：请核对是否漏行</strong><p>${escape(ledger.files.find(f=>f.file_id===t.file_id)?.filename || '')}</p><p>确认本页记录后，再对照原页填写交易总笔数。</p></div><button class="button small" data-page-task="${escape(t.review_id)}">查看原页 / 核对笔数</button></div>`).join('') +
    pending.map(r=>`<div class="review-row"><div><strong>${escape(r.counterparty || r.description || r.filename)}</strong><p>${escape(r.transaction_at || r.booking_at || '日期待定')} · ${escape(money(r.amount,r.currency))} · ${escape(direction[r.direction] || '方向待定')}</p><p>${escape(ledger.issues.filter(i=>i.record_id===r.record_id && i.severity==='error').map(i=>i.message).join('；') || '请对照原始字段或截图核验。')}</p></div><button class="button small" data-review-record="${escape(r.record_id)}">对照并核验</button></div>`).join('') +
    (!pending.length && !tasks.length && !issues.length ? '<div class="empty-state">本批次没有待处理的读取问题。仍可在来源明细查看和纠正；这不表示已完成交易去重。</div>' : '') + receipts;
}
async function openBatch(id) {
  if (id !== state.batch_id) readState(await request('/api/books',{body:{...context(),action:'open_batch',batch_id:id}}));
  renderReview();
}
function resetEvidence() {evidenceGeneration++;objectUrls.forEach(url=>URL.revokeObjectURL(url));objectUrls=[];}
async function renderEvidence(container, tasks, ctx) {
  const generation=evidenceGeneration;
  container.replaceChildren();
  const rowPaths = [...new Set(tasks.map(t=>t.evidence?.row_image).filter(Boolean))];
  const pagePaths = [...new Set(tasks.map(t=>t.evidence?.page_image).filter(Boolean))].filter(path=>!rowPaths.includes(path));
  async function load(card,path,isPage) {
    card.textContent='正在加载截图…';
    try {
      const blob = await request('/api/evidence',{query:{...ctx,path},blob:true});
      if(generation!==evidenceGeneration) return;
      const url = URL.createObjectURL(blob);objectUrls.push(url);
      const img=document.createElement('img');img.src=url;img.alt=isPage?'原始页面':'原始行截图';
      const link=document.createElement('a');link.href=url;link.target='_blank';link.rel='noopener';link.textContent='打开原尺寸截图';
      card.replaceChildren(link,img);
    } catch(error) {if(generation===evidenceGeneration) card.textContent=error.message;}
  }
  // Full pages are optional evidence, loaded only when explicitly expanded.
  for (const path of pagePaths) {
    const section=document.createElement('div');section.className='page-evidence-toggle';
    const button=document.createElement('button');button.type='button';button.className='button small';button.textContent='打开整页截图';button.setAttribute('aria-expanded','false');
    const card=document.createElement('div');card.className='evidence-card';card.hidden=true;card.id='evidence-'+crypto.randomUUID();button.setAttribute('aria-controls',card.id);
    let loaded=false;
    button.addEventListener('click',async()=>{
      card.hidden=!card.hidden;button.setAttribute('aria-expanded',String(!card.hidden));button.textContent=card.hidden?'打开整页截图':'收起整页截图';
      if(!card.hidden&&!loaded){loaded=true;await load(card,path,true);}
    });
    section.append(button,card);container.append(section);
  }
  for (const path of rowPaths) {
    const card=document.createElement('div');card.className='evidence-card row-evidence';container.append(card);await load(card,path,false);
  }
}
const fields = [['transaction_at','交易日期 / 时间'],['booking_at','记账日期 / 时间'],['amount','金额（非负）'],['direction','收付方向'],['currency','币种代码'],['balance','余额（可留空）'],['counterparty','交易对方'],['description','摘要'],['account','来源账户'],['source_record_id','来源流水号'],['transaction_status','交易状态']];
function renderFields(row, attention={}) {
  $('#record-fields').innerHTML=fields.map(([key,label])=>{
    const reasons=attention.fields?.[key] || [];
    const hintId='attention-'+key;
    const attrs=reasons.length?` aria-describedby="${hintId}"`:'';
    const content=key==='direction' ? `<select name="${key}"${attrs}><option value="">请选择</option>${Object.entries(direction).map(([v,t])=>`<option value="${v}" ${row[key]===v?'selected':''}>${t}</option>`).join('')}</select>` : key==='transaction_status' ? `<select name="${key}"${attrs}>${Object.entries(status).map(([v,t])=>`<option value="${v}" ${row[key]===v?'selected':''}>${t}</option>`).join('')}</select>` : `<input name="${key}" value="${escape(row[key])}" maxlength="2000"${attrs} ${['amount','currency'].includes(key)?'required':''}>`;
    const caption=key==='balance' && row.balance_reliable===false?'余额（仅供参考，可留空）':label;
    return `<label${reasons.length?' class="field-attention"':''}><span>${reasons.length?`<mark>${caption}</mark>`:caption}</span>${content}${reasons.length?`<small id="${hintId}" class="field-attention-reason">待核对 · ${escape(reasons.join('；'))}</small>`:''}</label>`;
  }).join('');
}
async function openRecord(row, continuing=false) {
  const generation=++detailGeneration;
  await openBatch(row.batch_id || state.batch_id);
  const ctx=batchContext();
  const detail=await request('/api/detail',{query:{...ctx,record_id:row.record_id}});
  if(generation!==detailGeneration) return;
  if(!continuing) {
    const pending=state.ledger.records.filter(r=>r.review_status==='pending').map(r=>r.record_id);
    const index=pending.indexOf(row.record_id);
    reviewSession=index<0?null:{ids:[...pending.slice(index),...pending.slice(0,index)],total:pending.length};
    $('#auto-next').checked=true;
  }
  editing={ctx,revision:detail.revision,record:detail.record,action:'confirm'};
  $('#manual-source').hidden=true;$('#exclude-record').hidden=false;$('#detail-error').hidden=true;
  $('#detail-title').textContent='查看来源与核验';updateSequence();
  $('#detail-location').textContent=`${currentBook().name} · ${detail.record.filename} · ${detail.record.page ? '第 '+detail.record.page+' 页' : detail.record.sheet || ''} ${detail.record.row ? '第 '+detail.record.row+' 行' : ''}`;
  const originalValues=detail.raw?.values || [];
  const originalHeaders=detail.original?.raw_headers || [];
  const attention=detail.attention || {};
  const rawAttention=new Map((attention.raw_columns || []).map(item=>[item.index,item]));
  const readable=value=>value && typeof value==='object' ? value.value ?? JSON.stringify(value) : value ?? '（空）';
  const reasons=state.ledger.tasks.filter(t=>t.record_id===row.record_id && t.status!=='resolved_by_policy').flatMap(t=>t.reason_messages || []);
  const notes=state.ledger.issues.filter(i=>i.record_id===row.record_id || i.file_id===detail.record.file_id && !i.record_id);
  $('#detail-raw').innerHTML=(originalValues.length ? `<table><thead><tr><th>原账单字段</th><th>原始内容</th></tr></thead><tbody>${originalValues.map((v,i)=>{
    const concern=rawAttention.get(i), label=escape(originalHeaders[i] || `第 ${i+1} 列`), value=escape(readable(v));
    return concern?`<tr class="raw-attention" data-raw-index="${i}"><td><mark>${label}</mark><small>待核对</small></td><td><mark>${value}</mark><small class="raw-attention-reason">${escape(concern.reasons.join('；'))}</small></td></tr>`:`<tr><td>${label}</td><td>${value}</td></tr>`;
  }).join('')}</tbody></table>` : '<p>此条为手工补录，来源和依据保存在修改历史中。</p>');
  if(detail.record.balance_reliable===false) $('#detail-raw').insertAdjacentHTML('beforeend','<p>余额识别存在不确定性，保留供参考，不影响收支记录使用，也不作为后续金额校验的可靠依据。</p>');
  if(detail.record.policy_adjustment) $('#detail-raw').insertAdjacentHTML('beforeend','<p>当前读取结果已按新版规则重新评估；原始底稿与人工修改均保留。</p>');
  const readableReason = r => r.replaceAll('amount+direction','金额和收付方向').replaceAll('transaction_at','交易日期').replaceAll('balance','余额').replace('关键字段识别分数低于 0.98','关键字段识别分数未通过门槛（缺失或低于 0.98）');
  $('#reading-notes').hidden=!notes.length&&!reasons.length;
  $('#reading-notes').open=false;
  $('#reading-notes-content').innerHTML=`<ul>${[...new Set([...notes.map(i=>i.message),...reasons.map(readableReason)])].map(r=>`<li>${escape(r)}</li>`).join('')}</ul>`;
  renderFields(detail.record,attention);
  $('#attention-legend').hidden=!Object.keys(attention.fields || {}).length;$('#record-note').value='';$('#record-note').required=false;$('#record-note-label').textContent='核验依据 / 修改原因（选填）';$('#raw-details').open=true;
  resetEvidence();if(!$('#detail-dialog').open) $('#detail-dialog').showModal();
  $('#detail-dialog').scrollTop=0;
  await renderEvidence($('#detail-evidence'),[{evidence:detail.record.ocr_evidence},...state.ledger.tasks.filter(t=>t.record_id===row.record_id || t.scope==='page' && t.file_id===detail.record.file_id && t.page===detail.record.page)],ctx);
}
function openManual() {
  reviewSession=null;$('#review-sequence').hidden=true;$('#attention-legend').hidden=true;
  editing={ctx:batchContext(),revision:state.ledger.revision,action:'add'};
  $('#reading-notes').hidden=true;
  $('#manual-source').hidden=false;$('#exclude-record').hidden=true;$('#detail-error').hidden=true;
  $('#detail-title').textContent='补录来源中遗漏的记录';$('#save-record').textContent='保存补录';
  $('#detail-location').textContent='请选择原文件与页码，保留补录依据；不会自动解除文件或页面异常。';
  $('#manual-file').innerHTML=state.ledger.files.filter(f=>f.file_id && f.status!=='duplicate_file_skipped').map(f=>`<option value="${escape(f.file_id)}">${escape(f.filename)}</option>`).join('');
  $('#manual-page').value='';$('#record-note').value='';$('#detail-raw').textContent='手工补录尚无原始行，来源文件和依据将随修订保存。';$('#raw-details').open=false;$('#detail-evidence').replaceChildren();
  $('#record-note').required=true;$('#record-note-label').textContent='补录依据（必填）';
  renderFields({});$('#detail-dialog').showModal();
}
async function saveRecord(exclude=false) {
  if(recordSaving) return;
  const note=$('#record-note').value.trim();
  if (!note && (exclude || editing.action==='add')) {$('#detail-error').textContent='补录或排除记录时，请说明来源或原因。普通核验确认无需填写。';$('#detail-error').hidden=false;$('#record-note').focus();return;}
  const changes={};for(const [key] of fields) changes[key]=$(`#record-fields [name="${key}"]`).value.trim() || null;
  const body={...editing.ctx,expected_revision:editing.revision,request_id:crypto.randomUUID(),note,action:exclude?'exclude':editing.action};
  if(editing.record) body.record_id=editing.record.record_id;
  if(!exclude) body.fields=changes;
  if(editing.action==='add') {body.file_id=$('#manual-file').value;body.page=$('#manual-page').value?Number($('#manual-page').value):null;}
  const buttons=$$('#record-form button');buttons.forEach(b=>b.disabled=true);
  recordSaving=true;
  const session=reviewSession, generation=detailGeneration;
  let saved=false;
  try {
    await request('/api/action',{body});saved=true;await refresh();
    const nextId=session?.ids.find(id=>id!==body.record_id && state.ledger.records.some(r=>r.record_id===id && r.review_status==='pending'));
    const next=nextId && state.ledger.records.find(r=>r.record_id===nextId);
    if(next && session===reviewSession && generation===detailGeneration && $('#detail-dialog').open && $('#auto-next').checked) {
      toast('本条已保存，继续核验下一条。');await openRecord(next,true);
    } else {
      if(generation===detailGeneration) $('#detail-dialog').close();
      toast(session && !next?'本批待核验记录已处理完；页面笔数或文件问题仍请单独查看。':exclude?'已排除，可在修改历史中撤销。':'核验已保存，来源汇总已更新。');
    }
  }
  catch(error) {
    if(saved) {if(generation===detailGeneration) $('#detail-dialog').close();toast('本条已保存，但后续页面加载未完成。请刷新继续。'+error.message);}
    else {$('#detail-error').textContent=error.message;$('#detail-error').hidden=false;}
  }
  finally {recordSaving=false;buttons.forEach(b=>b.disabled=false);}
}
function updateSequence() {
  $('#review-sequence').hidden=!reviewSession;
  const remaining=reviewSession?reviewSession.ids.filter(id=>state.ledger.records.some(r=>r.record_id===id && r.review_status==='pending')).length:0;
  $('#sequence-position').textContent=reviewSession?`本批剩余 ${remaining} 条 · 可随时关闭退出`:'';
  $('#save-record').textContent=reviewSession && remaining>1 && $('#auto-next').checked?'确认并下一条':'确认并保存';
}
$('#auto-next').addEventListener('change',updateSequence);
$('#detail-dialog').addEventListener('close',()=>{reviewSession=null;detailGeneration++;resetEvidence();});
async function openPageTask(id) {
  const task=state.ledger.tasks.find(t=>t.review_id===id);
  pageTask={task,ctx:batchContext(),revision:state.ledger.revision};
  $('#page-reason').textContent=`${state.ledger.files.find(f=>f.file_id===task.file_id)?.filename || ''} · 第 ${task.page} 页。请对照原页，确认当前记录是否完整。`;
  $('#observed-count').value='';$('#page-note').value='';$('#page-error').hidden=true;
  resetEvidence();$('#page-dialog').showModal();await renderEvidence($('#page-evidence'),[task],pageTask.ctx);
}
function openHistory() {
  const names={...Object.fromEntries(fields),human_confirmed:'人工确认',record_exists:'记录存在',excluded:'排除状态',page_confirmation:'页面确认'};
  const value=v=>v===null || v===undefined?'未设置':typeof v==='boolean'?(v?'是':'否'):typeof v==='object'?`已核对 ${v.count} 笔`:String(v);
  $('#history-content').innerHTML=state.ledger.history.map(h=>`<div class="history-event"><strong>修订 ${h.revision} · ${escape({confirm:'确认 / 修正',exclude:'排除',add:'补录',undo:'撤销',resolve_page:'页核验'}[h.action] || h.action)}</strong><p>${escape(h.note?.trim() || '未填写备注')}</p><small>${escape(h.created_at)}</small><ul>${h.changes.map(c=>`<li>${escape(names[c.field] || c.field)}：${escape(value(c.before))} → ${escape(value(c.after))}</li>`).join('')}</ul></div>`).join('') || '<div class="empty-state">本批次尚无人工修改。</div>';
  $('#undo').disabled=!state.ledger.undo_revision;$('#history-dialog').showModal();
}
async function importFiles(files) {
  if(busy) return;
  if(!state?.book_id) {toast('请先新建或打开账本。');return;}
  if(!files.length) return;
  if(files.length>20 || files.reduce((sum,f)=>sum+f.size,0)>50*1024*1024) throw new Error('每批最多 20 个文件，合计不超过 50 MiB。');
  const ctx=context();setBusy(true);$('#job-status').hidden=false;
  renderProgress($('#job-status'),{status:'running',started_at:Date.now()/1000,progress:{stage:'uploading'},message:'正在准备所选文件，请稍候。'});
  try {
    const payload=await Promise.all(files.map(filePayload));
    await request('/api/import',{body:{...ctx,files:payload,batch_name:$('#batch-name').value.trim(),allow_duplicate_files:$('#allow-duplicate').checked}});
    $('#file-input').value='';$('#batch-name').value='';$('#allow-duplicate').checked=false;
    await refresh();
  } catch(error) {await refresh();throw error;}
}
async function download(partial) {
  const blob=await request('/api/export',{query:{...context(),allow_partial:partial},blob:true});
  const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`FlowLens-${partial?'部分':'完整'}有效流水.json`;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),10000);$('#export-dialog').close();toast('已生成下载，请在浏览器下载记录中查看。');
}

async function goToReview() {
  if(busy) return;
  const batches = currentBook()?.batches || [];
  const target = batches.find(b => b.summary && (b.summary.pending || b.summary.page_pending || b.summary.blocking_issues)) || batches[0];
  if(target) await openBatch(target.id);
  await showPage('review');
  $('#page-title').scrollIntoView({block:'start'});
}
const guide = createGuide({
  getStage: () => !state || busy || activePage !== 'overview' ? null : !state.book_id ? 'create' : !currentBook()?.batch_count ? 'upload' : ((currentBook()?.summary.pending || currentBook()?.summary.page_pending || currentBook()?.summary.blocking_issues) ? 'review' : null),
  onAction: async stage => {try {
    if(stage==='create') $('#create-book').click();
    if(stage==='upload') {$('#choose-files').scrollIntoView({block:'center'});$('#choose-files').focus();}
    if(stage==='review') await goToReview();
  } catch(error) {toast(error.message);}},
});
$('#show-guide').addEventListener('click',()=>guide(true));
$('#review-shortcut').addEventListener('click',handle(goToReview));
$$('[data-page]').forEach(button=>button.addEventListener('click',handle(()=>showPage(button.dataset.page))));
$$('[data-close]').forEach(button=>button.addEventListener('click',()=>button.closest('dialog').close()));
$('#refresh').addEventListener('click',handle(refresh));
$('#create-book').addEventListener('click',()=>{nameMode='create';$('#name-title').textContent='新建账本';$('#book-name').value='';$('#name-dialog').showModal();});
$('#rename-book').addEventListener('click',()=>{nameMode='rename';$('#name-title').textContent='重命名账本';$('#book-name').value=currentBook().name;$('#name-dialog').showModal();});
$('#delete-book').addEventListener('click',()=>{
  const book=currentBook();if(busy||!book)return;
  deleteContext={...context(),target_id:book.id};
  $('#delete-book-description').textContent=`将「${book.name}」及其 ${book.batch_count} 个导入批次移入回收站。账单与核验记录会保留，可随时恢复。`;
  $('#delete-book-error').hidden=true;$('#delete-book-dialog').showModal();
});
$('#delete-book-form').addEventListener('submit',async event=>{
  event.preventDefault();const button=$('#confirm-delete-book');if(button.disabled)return;button.disabled=true;
  try{
    readState(await request('/api/books',{body:{...deleteContext,action:'trash'}}));
    $('#delete-book-dialog').close();page=1;await refresh();toast('账本已移入回收站，可从侧栏恢复。');
  }catch(error){$('#delete-book-error').textContent=error.message;$('#delete-book-error').hidden=false;}
  finally{button.disabled=false;}
});
function renderRecycleBooks(){
  const books=state.books.filter(b=>b.deleted);
  $('#recycle-content').innerHTML=books.length?books.map(b=>`<div class="batch-row"><div><strong>${escape(b.name)}</strong><p>${b.batch_count} 个导入批次 · 账单与核验记录保留</p></div><button class="button small" data-restore-book="${escape(b.id)}">恢复账本</button></div>`).join(''):'<p class="empty-state">回收站为空。</p>';
}
$('#recycle-books').addEventListener('click',()=>{renderRecycleBooks();$('#recycle-error').hidden=true;$('#recycle-dialog').showModal();});
$('#recycle-content').addEventListener('click',async event=>{
  const button=event.target.closest('[data-restore-book]');if(!button||button.disabled)return;
  $$('#recycle-content button').forEach(b=>b.disabled=true);
  try{
    readState(await request('/api/books',{body:{...context(),action:'restore',target_id:button.dataset.restoreBook}}));
    renderRecycleBooks();await refresh();toast('账本已恢复，可在“我的账本”中打开。');
  }catch(error){$('#recycle-error').textContent=error.message;$('#recycle-error').hidden=false;}
  finally{$$('#recycle-content button').forEach(b=>b.disabled=false);}
});
$('#name-form').addEventListener('submit',handle(async event=>{event.preventDefault();readState(await request('/api/books',{body:{...context(),action:nameMode,target_id:state.book_id,name:$('#book-name').value}}));$('#name-dialog').close();page=1;await refresh();}));
$('#book-select').addEventListener('change',handle(async event=>{readState(await request('/api/books',{body:{...context(),action:'open',target_id:event.target.value}}));page=1;await refresh();}));
$('#choose-files').addEventListener('click',()=>$('#file-input').click());
$('#file-input').addEventListener('change',handle(event=>importFiles([...event.target.files])));
$('#drop-zone').addEventListener('dragover',event=>{event.preventDefault();$('#drop-zone').classList.add('dragging');});
$('#drop-zone').addEventListener('dragleave',()=>$('#drop-zone').classList.remove('dragging'));
$('#drop-zone').addEventListener('drop',handle(event=>{event.preventDefault();$('#drop-zone').classList.remove('dragging');return importFiles([...event.dataTransfer.files]);}));
$('#batches').addEventListener('click',handle(async event=>{const button=event.target.closest('[data-batch]');if(button){await openBatch(button.dataset.batch);await showPage('review');}}));
$('#filters').addEventListener('submit',handle(async event=>{event.preventDefault();page=1;await renderRecords();}));
$('#previous').addEventListener('click',handle(async()=>{page--;await renderRecords();}));
$('#next').addEventListener('click',handle(async()=>{page++;await renderRecords();}));
$('#records-body').addEventListener('click',handle(event=>{const button=event.target.closest('[data-record]');if(button)return openRecord(rows[Number(button.dataset.record)]);}));
$('#review-batch').addEventListener('change',handle(event=>openBatch(event.target.value)));
$('#review-content').addEventListener('click',handle(event=>{const record=event.target.closest('[data-review-record]'),task=event.target.closest('[data-page-task]');if(record)return openRecord(state.ledger.records.find(r=>r.record_id===record.dataset.reviewRecord));if(task)return openPageTask(task.dataset.pageTask);}));
$('#add-record').addEventListener('click',openManual);
$('#record-form').addEventListener('submit',event=>{event.preventDefault();saveRecord();});
$('#exclude-record').addEventListener('click',()=>saveRecord(true));
$('#page-form').addEventListener('submit',handle(async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;try{await request('/api/action',{body:{...pageTask.ctx,expected_revision:pageTask.revision,request_id:crypto.randomUUID(),action:'resolve_page',review_id:pageTask.task.review_id,observed_count:Number($('#observed-count').value),note:$('#page-note').value}});await refresh();$('#page-dialog').close();toast('整页核验已保存。');}catch(error){$('#page-error').textContent=error.message;$('#page-error').hidden=false;}finally{button.disabled=false;}}));
$('#history').addEventListener('click',openHistory);
$('#undo').addEventListener('click',handle(async()=>{await request('/api/action',{body:{...batchContext(),expected_revision:state.ledger.revision,request_id:crypto.randomUUID(),action:'undo',target_revision:state.ledger.undo_revision,note:'用户撤销最近一次核验修改'}});await refresh();$('#history-dialog').close();toast('已撤销，原始数据与完整修改历史保留。');}));
$('#export').addEventListener('click',()=>{const partial=overview.status==='partial';$('#export-description').textContent=partial?'账本仍有读取缺口，完整导出暂不可用。你可以明确选择仅导出可用部分，文件会保留缺口说明。':'所有批次的读取核验门槛已通过，可以导出完整有效流水。';$('#download-full').disabled=partial;$('#download-partial').hidden=!partial;$('#export-dialog').showModal();});
$('#download-full').addEventListener('click',handle(()=>download(false)));
$('#download-partial').addEventListener('click',handle(()=>download(true)));
refresh().catch(error=>{notice(error.message);setBusy(false);});
