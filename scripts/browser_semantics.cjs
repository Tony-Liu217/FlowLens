/* Synthetic bank contexts only; isolated workspace and browser profile. */
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {python}=require('./browser_support.cjs');
const root=path.resolve(__dirname,'..'),run=path.join(root,'.local-data','semantic-browser-'+Date.now());
fs.mkdirSync(run,{recursive:true});
const connection=path.join(run,'connection.json');
let server,browser;
(async()=>{
  server=spawn(python,['-B','-X','utf8',path.join(root,'backend/run_app.py'),'--data-dir',path.join(run,'workspace'),'--no-ocr','--connection-file',connection],{cwd:root,windowsHide:true,stdio:'ignore'});
  for(let i=0;i<100&&!fs.existsSync(connection);i++)await new Promise(r=>setTimeout(r,100));
  browser=await chromium.launch({channel:'msedge',headless:true});
  const context=await browser.newContext({viewport:{width:1440,height:1000},acceptDownloads:true});
  await context.addInitScript(()=>localStorage.setItem('flowlens-guide-v1',JSON.stringify({skipped:true})));
  const page=await context.newPage(),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(JSON.parse(fs.readFileSync(connection,'utf8')).url);
  await page.locator('#create-book').click();
  await page.getByLabel('账本名称',{exact:true}).fill('来源信息合成验收');
  await page.locator('#name-form').getByRole('button',{name:'保存',exact:true}).click();
  await page.waitForFunction(()=>!document.querySelector('#choose-files').disabled);
  await page.locator('#file-input').setInputFiles([
    {name:'合成建行.csv',mimeType:'text/csv',buffer:Buffer.from('序号,摘要,币别,钞汇,交易日期,交易金额,账户余额,交易地点/附言,对方账号与户名,新增业务字段\n1,消费,人民币,钞,20260801,-12.30,100,财付通-微信支付-合成商户,Z***0010/**商户,独有检索线索\n')},
    {name:'合成中行.csv',mimeType:'text/csv',buffer:Buffer.from('记账日期,记账时间,币别,金额,余额,交易名称,渠道,网点名称,附言,对方账户名,对方卡号/账号,对方开户行\n2026-08-02,10:20:30,人民币,-20,80,网上快捷支付,银企对接,某网点,合成群收款,支付机构,123***,某行\n')}
  ]);
  await page.locator('#kpi-total').filter({hasText:'2'}).waitFor();
  await page.locator('[data-page="records"]').click();
  await page.getByText('附言中的对方 / 渠道线索',{exact:true}).waitFor();
  assert((await page.locator('#records-body').innerText()).includes('合成群收款'));
  await page.locator('#filters [name="q"]').fill('独有检索线索');
  await page.locator('#filters').getByRole('button',{name:'筛选'}).click();
  await page.waitForFunction(()=>document.querySelector('#records-count').textContent.includes('共 1 条'));
  assert((await page.locator('#records-body').innerText()).includes('有未归类信息：新增业务字段'));
  await page.locator('[data-record]').click();
  await page.locator('#detail-dialog').waitFor();
  assert.equal(await page.locator('#record-fields [name="counterparty"]').inputValue(),'财付通-微信支付-合成商户');
  assert((await page.locator('#detail-raw').innerText()).includes('尚未归类，原文已保留并可搜索'));
  assert((await page.locator('#detail-raw').innerText()).includes('保留来源信息，不推断身份'));
  await page.screenshot({path:path.join(run,'semantic-review.png')});
  await page.keyboard.press('Escape');
  await page.locator('[data-page="overview"]').click();
  await page.getByRole('button',{name:'导出有效流水',exact:true}).click();
  const downloading=page.waitForEvent('download');
  await page.locator('#download-full').click();
  await (await downloading).saveAs(path.join(run,'effective.json'));
  const exported=JSON.parse(fs.readFileSync(path.join(run,'effective.json'),'utf8'));
  assert(exported.records.some(r=>r.source_fields.some(f=>f.value==='独有检索线索')));
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({run,passed:['建行附言线索可见','中行摘要可见','未归类字段搜索及提示','遮蔽字段保留而不推断','完整导出保留所有源字段'],errors},null,2));
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();if(server&&server.exitCode===null)server.kill();});
