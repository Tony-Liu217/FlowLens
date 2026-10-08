export function createGuide({getStage, onAction}) {
  const dialog = document.querySelector('#guide-dialog');
  const key = 'flowlens-guide-v1';
  let saved = {};
  try {saved = JSON.parse(localStorage.getItem(key)) || {};} catch {}
  const seen = new Set(Array.isArray(saved.seen) ? saved.seen : []);
  let skipped = saved.skipped === true, current;
  const steps = {
    create: ['01', '先给账单一个家', '点击左侧「＋ 新建」，为这段时间的账单创建一个账本。不同平台的文件可以放进同一个账本。', '新建账本'],
    upload: ['02', '把分散的账单放进来', '在导入区域选择或拖入文件。读取会在本机进行，你可以看到当前文件和处理进度。以后也能继续追加。', '前往导入区域'],
    review: ['03', '结果已就绪，再核对少量疑点', '已读取的部分可以先查看。点击「待核验记录」卡片上的入口，或左侧「读取核验」，对照原图确认疑点；记录准确时，无需填写理由。', '去读取核验'],
    overview: ['03', '随时回到读取核验', '新建账本在左侧，导入区域可以追加文件。待核验卡片和左侧「读取核验」都能打开核验页面；修改历史里可以撤销操作。', '知道了'],
  };
  function save() {try {localStorage.setItem(key,JSON.stringify({skipped,seen:[...seen]}));} catch {}}
  function skip() {skipped=true;save();dialog.close();}
  dialog.querySelector('#guide-skip').addEventListener('click',skip);
  dialog.addEventListener('cancel',event=>{event.preventDefault();skip();});
  dialog.querySelector('#guide-next').addEventListener('click',async()=>{
    seen.add(current);save();dialog.close();await onAction(current);
  });
  return function show(force=false) {
    if (document.querySelector('dialog[open]')) return;
    const stage = getStage() || (force ? 'overview' : null);
    if (!stage || !force && (skipped || seen.has(stage))) return;
    if(force) {skipped=false;save();}
    current = stage;
    const [number,title,description,action] = steps[stage];
    dialog.querySelector('#guide-step').textContent = `上手指引 · ${number} / 03`;
    dialog.querySelector('#guide-title').textContent = title;
    dialog.querySelector('#guide-description').textContent = description;
    dialog.querySelector('#guide-next').textContent = action;
    dialog.showModal();
  };
}
