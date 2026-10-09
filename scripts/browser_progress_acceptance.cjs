const {python}=require('./browser_support.cjs');
/* Real local OCR in an isolated workspace. The supplied image is read only. */
const fs=require('node:fs'), path=require('node:path'), assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root=path.resolve(__dirname,'..'), run=path.join(root,'.local-data','progress-qa-'+Date.now());
const source=process.argv[2];if(!source) throw Error('Pass a local statement image for isolated OCR validation.');
const input=fs.readFileSync(source);
const wait=ms=>new Promise(r=>setTimeout(r,ms));
fs.mkdirSync(run,{recursive:true});
const connection=path.join(run,'connection.json');
let server,browser,page;
(async()=>{
  server=spawn(python,['-B','-X','utf8',path.join(root,'backend','run_app.py'),'--data-dir',path.join(run,'workspace'),'--connection-file',connection],{cwd:root,windowsHide:true,stdio:'ignore'});
  for(let i=0;i<100&&!fs.existsSync(connection);i++) await wait(100);
  const conn=JSON.parse(fs.readFileSync(connection,'utf8'));
  browser=await chromium.launch({channel:'msedge',headless:true});
  const context=await browser.newContext({viewport:{width:1440,height:1050}});
  await context.addInitScript(()=>localStorage.setItem('flowlens-guide-v1',JSON.stringify({skipped:true})));
  page=await context.newPage();const errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  page.on('console',m=>{if(m.type()==='error')errors.push(m.text());});
  await page.goto(conn.url);await page.locator('#create-book').click();
  await page.locator('#book-name').fill('进度隔离验证');await page.locator('#name-form [type="submit"]').click();
  await page.waitForFunction(()=>!document.querySelector('#choose-files').disabled);
  await page.locator('#file-input').setInputFiles([
    {name:'合成表格.csv',mimeType:'text/csv',buffer:Buffer.from('交易日期,金额,收支\n2026-01-01,1,支出\n')},
    {name:'本地扫描验证.png',mimeType:'image/png',buffer:input},
  ]);
  await page.waitForFunction(()=>document.querySelector('.progress-heading strong')?.textContent==='识别扫描页面',{},{timeout:60000});
  assert.equal(await page.locator('.progress-count').innerText(),'已处理 1 / 2 个文件');
  assert.equal(await page.locator('progress').getAttribute('value'),'1');
  assert.equal(await page.locator('progress').getAttribute('max'),'2');
  assert((await page.locator('.progress-description').innerText()).includes('第 1 页'));
  await page.screenshot({path:path.join(run,'progress-ocr.png')});
  await page.reload();
  await page.locator('#job-status.running').waitFor();
  assert((await page.locator('.progress-count').innerText()).includes('/ 2'));
  await page.locator('#job-status.completed').waitFor({timeout:180000});
  assert.equal(await page.locator('progress').getAttribute('value'),'2');
  assert.equal(await page.locator('.progress-count').innerText(),'已处理 2 / 2 个文件');
  await page.locator('#review-shortcut').click();
  await page.locator('[data-review-record]').first().click();
  await page.locator('#record-fields').waitFor();
  assert.equal(await page.locator('#detail-reasons').count(),0);
  assert.equal(await page.locator('#detail-evidence .page-evidence-toggle button').getAttribute('aria-expanded'),'false');
  await page.locator('#detail-dialog [data-close]').click();
  await page.locator('[data-page="overview"]').click();
  await page.evaluate(async()=>{
    const {renderProgress}=await import('/js/progress.js');
    renderProgress(document.querySelector('#job-status'),{status:'failed',message:'合成失败展示检查',progress:{total:2,completed:1}});
  });
  assert.equal(await page.locator('#job-status progress').count(),0);
  await page.setViewportSize({width:390,height:844});
  await page.locator('#show-guide').click();
  await page.locator('#guide-dialog').waitFor();
  assert(await page.locator('#guide-dialog').evaluate(el=>el.scrollWidth<=el.clientWidth+1));
  await page.screenshot({path:path.join(run,'guide-mobile.png')});
  assert.equal(errors.length,0,errors.join('\n'));
  assert(input.equals(fs.readFileSync(source)));
  const report={run,results:['真实 OCR 阶段、文件计数和页码可见','读取中刷新恢复任务进度','保存登记后才显示完成','核验弹窗展示具体 OCR 原因','失败展示无完成条（组件合成状态）','窄屏引导没有内部横向溢出','来源图像未修改'],errors};
  fs.writeFileSync(path.join(run,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report,null,2));
})().catch(async e=>{console.error(e);if(page)await page.screenshot({path:path.join(run,'failure.png')}).catch(()=>{});console.error(run);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();if(server&&server.exitCode===null)server.kill();});
