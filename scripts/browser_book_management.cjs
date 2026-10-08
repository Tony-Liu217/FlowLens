const {python}=require('./browser_support.cjs');
/* Only synthetic books in a new isolated data directory. */
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..'),run=path.join(root,'.local-data','book-management-qa-'+Date.now());
fs.mkdirSync(run,{recursive:true});const connection=path.join(run,'connection.json');
let server,browser,page;const wait=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{
 server=spawn(python,['-B','-X','utf8',path.join(root,'backend/run_app.py'),'--data-dir',path.join(run,'workspace'),'--no-ocr','--connection-file',connection],{cwd:root,windowsHide:true,stdio:'ignore'});
 for(let i=0;i<100&&!fs.existsSync(connection);i++)await wait(100);
 const conn=JSON.parse(fs.readFileSync(connection,'utf8'));
 browser=await chromium.launch({channel:'msedge',headless:true});const context=await browser.newContext({viewport:{width:1360,height:980}});
 await context.addInitScript(()=>localStorage.setItem('flowlens-guide-v1',JSON.stringify({skipped:true})));
 page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));await page.goto(conn.url);
 async function create(name){await page.locator('#create-book').click();await page.locator('#book-name').fill(name);await page.locator('#name-form [type="submit"]').click();await page.locator('#name-dialog').waitFor({state:'hidden'});await page.waitForFunction(()=>!document.querySelector('#choose-files').disabled);return page.locator('#book-select').inputValue();}
 const first=await create('保留账本');const target=await create('待删除 <账本>');
 await page.locator('#file-input').setInputFiles({name:'合成.csv',mimeType:'text/csv',buffer:Buffer.from('交易日期,金额,收支,币种\n2026-01-01,12,支出,CNY\n')});
 await page.locator('#kpi-total').filter({hasText:'1'}).waitFor();
 await page.locator('#delete-book').click();assert((await page.locator('#delete-book-description').innerText()).includes('待删除 <账本>'));
 await page.locator('#delete-book-dialog [data-close]').click();assert.equal(await page.locator('#book-select').inputValue(),target);
 // A second page changes the catalog after the confirmation was opened.
 await page.locator('#delete-book').click();const second=await context.newPage();await second.goto(conn.url);
 await second.locator('#book-select').selectOption(first);await second.locator('#kpi-total').filter({hasText:'0'}).waitFor();
 await page.locator('#confirm-delete-book').click();await page.locator('#delete-book-error').filter({hasText:'版本已变化'}).waitFor();
 await second.close();await page.locator('#delete-book-dialog [data-close]').click();await page.locator('#refresh').click();
 await page.waitForFunction(id=>document.querySelector('#book-select').value===id,first);
 await page.locator('#book-select').selectOption(target);await page.locator('#kpi-total').filter({hasText:'1'}).waitFor();
 await page.locator('#delete-book').click();await page.locator('#confirm-delete-book').click();await page.locator('#delete-book-dialog').waitFor({state:'hidden'});
 await page.locator('#kpi-total').filter({hasText:'—'}).waitFor();assert.equal(await page.locator(`#book-select option[value="${target}"]`).count(),0);
 assert.equal(await page.locator(`#book-select option[value="${first}"]`).count(),1);
 await page.reload();await page.locator('#recycle-books').filter({hasText:'1'}).waitFor();await page.locator('#recycle-books').click();
 await page.screenshot({path:path.join(run,'recycle.png')});await page.locator('[data-restore-book]').click();await page.locator('#recycle-content').filter({hasText:'回收站为空'}).waitFor();
 await page.locator('#recycle-dialog [data-close]').click();await page.locator('#book-select').selectOption(target);await page.locator('#kpi-total').filter({hasText:'1'}).waitFor();
 assert.equal(await page.locator('#kpi-eligible').innerText(),'1');
 await page.setViewportSize({width:390,height:844});await page.screenshot({path:path.join(run,'mobile-books.png')});
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
 assert.equal(errors.length,0);
 const report={cancel_preserves_book:true,stale_confirmation_rejected:true,other_book_preserved:true,recycle_survives_reload:true,restore_preserves_records:true,mobile_no_overflow:true,errors};
 fs.writeFileSync(path.join(run,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify({run,...report}));
})().catch(async e=>{console.error(e);if(page)await page.screenshot({path:path.join(run,'failure.png')}).catch(()=>{});process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();if(server&&server.exitCode===null)server.kill();});
