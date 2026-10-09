/* Read-only user operations against a private isolated baseline; local projection writes allowed. */
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');const {spawn}=require('node:child_process');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');const root=path.resolve(__dirname,'..'),run=path.resolve(process.argv[2]);
assert(run.startsWith(path.join(root,'.local-data','analysis-baseline-')));const connFile=path.join(run,'analysis-ui-connection.json');
const expected=JSON.parse(fs.readFileSync(path.join(run,'analysis-validation.json'),'utf8'));
let server,browser,page;const wait=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{
 if(fs.existsSync(connFile))fs.unlinkSync(connFile);
 server=spawn(path.join(root,'runtime/python.exe'),['-B','-X','utf8',path.join(root,'backend/run_app.py'),'--data-dir',path.join(run,'workspace'),'--no-ocr','--connection-file',connFile],{cwd:root,windowsHide:true,stdio:'ignore'});
 for(let i=0;i<100&&!fs.existsSync(connFile);i++)await wait(100);const conn=JSON.parse(fs.readFileSync(connFile,'utf8'));
 browser=await chromium.launch({channel:'msedge',headless:true});page=await browser.newPage({viewport:{width:1440,height:1050}});page.setDefaultTimeout(60000);
 await page.addInitScript(()=>localStorage.setItem('flowlens-guide-v1',JSON.stringify({skipped:true})));
 const errors=[];page.on('pageerror',e=>errors.push(e.message));await page.goto(conn.url);await page.locator('#kpi-total').filter({hasText:String(expected.reading_summary.total)}).waitFor();await page.locator('[data-page="analysis"]').click();
 await page.locator('#analysis-counts').filter({hasText:`${expected.transaction_count} 笔流水`}).waitFor();await page.locator('#analysis-rows tr').first().waitFor();
 if(expected.reading_summary.pending)assert((await page.locator('#analysis-basis').innerText()).includes('仍有读取缺口'));
 await page.screenshot({path:path.join(run,'private-analysis.png')});
 const merged=page.locator('#analysis-rows tr').filter({hasText:'2 条来源'}).first();await merged.locator('button').click();await page.locator('#flow-fields tr').first().waitFor();
 assert.equal(await page.locator('[data-flow-source]').count(),2);assert((await page.locator('#flow-fields').innerText()).includes('商品／说明'));
 await page.screenshot({path:path.join(run,'private-merged-fields.png')});await page.locator('#flow-detail [data-close]').click();
 await page.locator('#analysis-next').click();await page.locator('#analysis-page').filter({hasText:'第 2 /'}).waitFor();
 await page.locator('.analysis-ai summary').click();await page.locator('#analysis-ai-preview').click();await page.locator('#flow-privacy').waitFor();assert((await page.locator('#flow-privacy-count').innerText()).includes('DeepSeek V4.1 Flash'));
 await page.locator('#flow-privacy [data-close]').click();assert.deepEqual(errors,[]);
 const report={records:expected.source_count,transactions:expected.transaction_count,partial_disclosed:true,merged_fields_visible:true,pagination:true,privacy_preview:true,external_ai_calls:0,user_decisions:0,errors};fs.writeFileSync(path.join(run,'analysis-ui-report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));
})().catch(async e=>{console.error(e);if(page)await page.screenshot({path:path.join(run,'private-analysis-failure.png')}).catch(()=>{});process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();if(server&&server.exitCode===null)server.kill();});
