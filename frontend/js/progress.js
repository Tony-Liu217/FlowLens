// Shared task UI: counts are completed work units, never an estimated time percentage.
const stages = {preparing:'准备读取',reading:'读取文件',standardizing:'整理账单字段',ocr:'识别扫描页面',file_done:'文件处理结束',saving:'保存账本与读取结果',uploading:'准备并传送文件'};
export function renderProgress(container, task) {
  container.replaceChildren();
  container.className = `job-status ${['running','completed','failed','interrupted'].includes(task.status) ? task.status : ''}`;
  container.setAttribute('aria-busy', String(task.status === 'running'));
  const detail = task.progress || {};
  const head = document.createElement('div'); head.className = 'progress-heading';
  const title = document.createElement('strong');
  title.textContent = task.status === 'running' ? stages[detail.stage] || '正在处理' : task.status === 'completed' ? '读取完成，已保存到账本' : '本次处理未完成';
  const count = document.createElement('span'); count.className = 'progress-count';
  const hasCount = Number.isInteger(detail.total) && detail.total > 0 && Number.isInteger(detail.completed);
  count.textContent = hasCount ? `已处理 ${detail.completed} / ${detail.total} 个文件` : task.status === 'completed' ? '已保存' : '正在进行';
  head.append(title, count);
  const track = document.createElement('div');track.className = 'progress-track';
  const bar = document.createElement('progress');bar.max = hasCount ? detail.total : 1;
  bar.setAttribute('aria-label','文件处理进度');
  if (task.status === 'completed') bar.value = bar.max;
  else if (hasCount && !['preparing','saving'].includes(detail.stage)) bar.value = detail.completed;
  track.append(bar);
  const description = document.createElement('p'); description.className = 'progress-description';
  description.textContent = task.status === 'running' ? [detail.filename, detail.page ? `第 ${detail.page} 页` : detail.segment ? `第 ${detail.segment} / ${detail.segments} 个片段` : '', detail.stage === 'ocr' ? '正在本机识别并比对多个图像版本，请稍候。' : task.message].filter(Boolean).join(' · ') : task.message;
  const elapsed = document.createElement('span');elapsed.className='progress-elapsed';
  if(task.status === 'running' && task.started_at) elapsed.dataset.progressStart = task.started_at;
  // A failed or interrupted task must not appear as a completed progress bar.
  if (task.status !== 'running' && task.status !== 'completed') {count.textContent='需要处理';container.append(head,description);return;}
  container.append(head,track,description,elapsed);tick();
}
function tick() {
  document.querySelectorAll('[data-progress-start]').forEach(el=>{
    const seconds = Math.max(0, Math.floor(Date.now()/1000 - Number(el.dataset.progressStart)));
    el.textContent = `已等待 ${seconds < 60 ? seconds+' 秒' : Math.floor(seconds/60)+' 分 '+seconds%60+' 秒'} · 文件进度不代表剩余时间，请保持服务开启`;
  });
}
setInterval(tick,1000);
