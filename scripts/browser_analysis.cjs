/* Stage-two user task on a fresh synthetic book; no provider calls. */
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {spawn,spawnSync}=require('node:child_process');const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..'),run=path.join(root,'.local-data','analysis-browser-'+Date.now());fs.mkdirSync(run,{recursive:true});
const connection=path.join(run,'connection.json'),python=path.join(root,'runtime/python.exe'),data=path.join(run,'workspace');
const seeded=spawnSync(python,['-B','-X','utf8',path.join(root,'scripts/seed_analysis_fixture.py'),data],{cwd:root,encoding:'utf8',windowsHide:true});assert.equal(seeded.status,0,seeded.stderr);
let server,browser,page;const wait=ms=>new Promise(r=>setTimeout(r,ms));
async function start(){if(fs.existsSync(connection))fs.unlinkSync(connection);server=spawn(python,['-B','-X','utf8',path.join(root,'backend/run_app.py'),'--data-dir',data,'--no-ocr','--connection-file',connection],{cwd:root,windowsHide:true,stdio:'ignore'});for(let i=0;i<100&&!fs.existsSync(connection);i++)await wait(100);return JSON.parse(fs.readFileSync(connection,'utf8'));}
async function stop(){if(server&&server.exitCode===null){const ended=new Promise(r=>server.once('exit',r));server.kill();await ended;}}
(async()=>{
 let conn=await start();browser=await chromium.launch({channel:'msedge',headless:true});const context=await browser.newContext({viewport:{width:1440,height:1050},acceptDownloads:true});await context.addInitScript(()=>localStorage.setItem('flowlens-guide-v1',JSON.stringify({skipped:true})));
 page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));await page.goto(conn.url);await page.locator('#kpi-total').filter({hasText:'5'}).waitFor();
 await page.locator('[data-page="analysis"]').click();await page.locator('#analysis-counts').filter({hasText:'4 笔流水'}).waitFor();
 await page.screenshot({path:path.join(run,'analysis.png')});
 const merged=page.locator('#analysis-rows tr').filter({hasText:'苹果与牛奶'});await merged.locator('button').click();await page.locator('#flow-detail').waitFor();
 assert((await page.locator('#flow-fields').innerText()).includes('建设银行储蓄卡'));
 assert((await page.locator('#flow-fields').innerText()).includes('财付通'));
 await page.screenshot({path:path.join(run,'merged-fields.png')});
 await page.locator('#flow-display-description').fill('已核对的苹果与牛奶');await page.locator('#flow-display-form button').click();await page.locator('#flow-detail').waitFor({state:'hidden'});
 await page.locator('#analysis-rows tr').filter({hasText:'已核对的苹果与牛奶'}).locator('button').click();
 await page.locator('[data-flow-split]').click();await page.locator('#flow-detail').waitFor({state:'hidden'});await page.locator('#analysis-counts').filter({hasText:'5 笔流水'}).waitFor();
 await page.locator('#analysis-history').click();await page.locator('[data-flow-undo]').first().click();await page.locator('#flow-history').waitFor({state:'hidden'});await page.locator('#analysis-counts').filter({hasText:'4 笔流水'}).waitFor();
 await page.locator('.analysis-ai summary').click();await page.locator('#analysis-ai-preview').click();await page.locator('#flow-privacy').waitFor();
 const payload=await page.locator('#flow-privacy-content').innerText();assert(!payload.includes('order10002'));assert(!payload.includes('(1234)'));await page.locator('#flow-privacy [data-close]').click();
 // Inspect the trial request without making an external call or changing source data.
 let trial;
 await page.route('**/api/analysis/ai',async route=>{trial=route.request().postDataJSON();await route.fulfill({status:200,contentType:'application/json',body:'{}'});});
 await page.locator('#analysis-api-key').fill('synthetic-key-for-ui');await page.locator('#analysis-ai-sample').click();
 for(let i=0;i<50&&!trial;i++)await wait(20);
 assert.equal(trial.sample,true);assert.equal(trial.action,'start');
 await page.waitForFunction(()=>document.querySelector('#analysis-api-key').value==='');
 await page.unroute('**/api/analysis/ai');
 await page.locator('#analysis-pairs summary').click();await page.locator('[data-decision="accept"]').first().waitFor();
 assert((await page.locator('.pair-direction').first().innerText()).includes('流出（支出）'));
 assert((await page.locator('#analysis-pair-list').innerText()).includes('存在同额竞争记录'));
 assert((await page.locator('#analysis-pair-list').innerText()).includes('候选方案'));
 assert((await page.locator('#analysis-pair-list').innerText()).includes('原账单序号'));
 await page.screenshot({path:path.join(run,'pair-directions.png')});
 await page.locator('[data-decision="accept"]').first().click();await page.locator('#analysis-counts').filter({hasText:'3 笔流水'}).waitFor();
 const downloadPromise=page.waitForEvent('download');await page.locator('#analysis-export').click();const download=await downloadPromise;const file=path.join(run,'analysis-export.json');await download.saveAs(file);const exported=JSON.parse(fs.readFileSync(file,'utf8'));
 assert.equal(exported.transaction_count,3);assert.equal(exported.totals.CNY.out,'98.00');assert.equal(exported.source_count,5);
 const raw=await page.evaluate(async()=>{const {request}=await import('/js/api.js');const s=await request('/api/state');return request('/api/export',{query:{book_id:s.book_id,catalog_revision:s.catalog_revision}});});assert.equal(raw.records.length,5);assert(raw.records.some(r=>r.description==='苹果与牛奶'));
 await stop();conn=await start();await page.goto(conn.url);await page.locator('#kpi-total').filter({hasText:'5'}).waitFor();await page.locator('[data-page="analysis"]').click();await page.locator('#analysis-counts').filter({hasText:'3 笔流水'}).waitFor();
 await page.setViewportSize({width:390,height:844});await page.screenshot({path:path.join(run,'analysis-mobile.png')});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
 assert.deepEqual(errors,[]);console.log(JSON.stringify({run,checks:['自动去重结果','商户商品与银行字段保留','显示修正不改底稿','拆开与撤销','外发预览脱敏','竞争配对人工确认','导出金额一致','来源5条不丢失','重启保留','窄屏无溢出'],errors}));
})().catch(async e=>{console.error(e);if(page)await page.screenshot({path:path.join(run,'failure.png')}).catch(()=>{});console.error(run);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();await stop();});
