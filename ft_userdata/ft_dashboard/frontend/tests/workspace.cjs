const {chromium,webkit}=require('playwright');
const fs=require('fs');const luminance=([r,g,b])=>{const [x,y,z]=[r,g,b].map(v=>v/255).map(v=>v<=0.03928?v/12.92:((v+0.055)/1.055)**2.4);return 0.2126*x+0.7152*y+0.0722*z;};const contrast=(a,b)=>{const [x,y]=[luminance(a),luminance(b)].sort((p,q)=>q-p);return (x+0.05)/(y+0.05);};const root=require('path').join(__dirname,'../../');const assert=require('node:assert/strict');
(async()=>{
 const browser=await (process.env.BROWSER_ENGINE==='webkit'?webkit:chromium).launch(process.env.CHROME_CHANNEL&&process.env.BROWSER_ENGINE!=='webkit'?{channel:process.env.CHROME_CHANNEL}:{});const page=await browser.newPage({viewport:{width:1440,height:1000}});await page.addInitScript(()=>Object.defineProperty(navigator,'language',{get:()=> 'en-US@posix'}));const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const fixture=require('./fixture.cjs')(); let closeRequests=0;
 await page.route('**/*',async route=>{const url=new URL(route.request().url());if(url.pathname.includes('alpine'))return route.fulfill({path:require.resolve('alpinejs/dist/cdn.min.js'),contentType:'text/javascript'});if(url.pathname.endsWith('/close')) {closeRequests++;return route.fulfill({json:{state:'accepted'}});}if(url.pathname.startsWith('/api/'))return route.fulfill({json:fixture.response(url.pathname)});if(url.pathname.startsWith('/static/'))return route.fulfill({path:root+url.pathname.slice(1)});if(url.hostname==='dashboard.test')return route.fulfill({body:fs.readFileSync(root+'templates/index.html','utf8').replace('master-trader<span>','master-trader · Preview<span>'),contentType:'text/html'});return route.abort();});
 await page.goto('https://dashboard.test/');await page.waitForTimeout(1800);
 await page.locator('details.workspace-research').evaluate(el=>{el.open=true});await page.waitForTimeout(300);
 const recentLive=page.locator('details.workspace-research table.ledger');
 assert.equal((await recentLive.locator('thead th').nth(1).textContent()).trim(),'strategy','Merged live-bot ledger must name the bot');
 const recentBots=await recentLive.locator('tbody tr').evaluateAll(rows=>rows.map(row=>row.cells[1].textContent.trim()));
 assert.equal(recentBots.length,6);assert.deepEqual([...new Set(recentBots)].sort(),['funding-fade','keltner-bounce','killers-scalp']);
 await page.evaluate(()=>Alpine.$data(document.body).setTab('portfolio'));await page.waitForTimeout(500);
 assert.equal(await page.locator('#chart-portfolio-equity .analytics-plot').count(),1);
 assert.equal(await page.locator('#chart-portfolio-strategy tbody tr').count(),3);
 assert.equal(await page.evaluate(()=>Boolean(window.echarts)),false);
 assert.equal(await page.evaluate(()=>Alpine.$data(document.body).stopTone({})), 'pending');
 const sections=await page.locator('.portfolio-analysis>.grid').evaluateAll(nodes=>nodes.map(el=>({top:el.getBoundingClientRect().top,bottom:el.getBoundingClientRect().bottom})));
 for(let i=1;i<sections.length;i++)assert(sections[i].top-sections[i-1].bottom>=20,'Portfolio sections must have a visible gutter');
 await page.getByRole('button',{name:/^positions/}).first().click();await page.waitForTimeout(1000);
 // Strategy-managed exits: rendered from the bot's reported policy, never as orders.
 const policyCard=page.locator('.trade-card',{hasText:'TRX/USDT'});
 assert.match(await policyCard.locator('.exit-policy-head').textContent(),/not resting exchange orders/);
 assert.deepEqual(await policyCard.locator('.exit-policy dl>div').evaluateAll(rows=>rows.map(row=>[row.querySelector('dt').textContent,row.querySelector('dd').textContent,row.title])),[
  ['ROI','Exits above +2.00% P&L now · final step since 24h',''],['Trailing','Off',''],
  ['Time','Exits after 96h if P&L is negative (in effect now)','Exit reason: v2_failed_reversion'],
  ['Time','Exits after 168h if P&L is below +1.00% (in 1.0d)','Exit reason: v2_expired_episode']]);
 const hlContext=await page.locator('.trade-card',{hasText:'LINK/USDC:USDC'}).locator('.position-context').innerText();
 assert.match(hlContext,/3× leverage · P&L % is on margin/);assert.match(hlContext,/Price \+8\.00% from entry/);
 const policyRows=await page.evaluate(()=>{const d=Alpine.$data(document.body);const now=Date.UTC(2026,9,1);const at=min=>({is_open:true,open_ts:now-min*60000,close_ts:now,open_rate:100,close_rate:101});
  const rows=(trade,policy)=>d.exitPolicyRows({...trade,exit_policy:policy}).map(r=>r.label+': '+r.text);
  return {young:rows(at(100),{roi:[[0,.08],[360,.05]],trailing:{enabled:false},rules:[{kind:'time',after_hours:36,profit_below:null,reason:'time_exit_36h'},{kind:'signal',text:'RSI(14) is below 30',reason:'x'}]}),
   shortOffset:rows({...at(10),is_short:true,leverage:3},{roi:[[0,100]],stoploss:-.06,trailing:{enabled:true,positive:.03,offset:.05,only_offset_reached:true},rules:[],receiver_driven:true}),
   fromEntry:rows({...at(10),leverage:3},{roi:[],stoploss:-.06,trailing:{enabled:true,positive:.03,offset:.05,only_offset_reached:false},rules:[]}),
   unreported:rows(at(10),{roi:null,stoploss:null,trailing:null,rules:[]}),
   closed:rows({...at(10),is_open:false},{roi:[[0,.08]],trailing:{enabled:false},rules:[]})};});
 assert.deepEqual(policyRows,{
  young:['ROI: Exits above +8.00% P&L now · +5.00% from 6h (in 4.3h)','Trailing: Off','Time: Exits after 36h (in 1.4d)','Signal: Exits when RSI(14) is below 30'],
  shortOffset:['ROI: Off · table set to +10000.00%','Trailing: Starts once P&L reaches +5.00%, then trails 1.00% above the lowest price','Receiver: Target exits and stop moves come from the source signal; the strategy adds no time or signal exit'],
  fromEntry:['ROI: No ROI table','Trailing: Trails 2.00% below the highest price; 1.00% once P&L exceeds +5.00%'],
  unreported:['ROI: Not reported by the bot','Trailing: Not reported by the bot'],closed:[]});
 await page.getByRole('button',{name:'Close position…',exact:true}).first().click();
 if(process.env.SCREENSHOTS){fs.mkdirSync(process.env.SCREENSHOTS,{recursive:true});for(const width of [1440,390]){await page.setViewportSize({width,height:1000});await page.screenshot({path:process.env.SCREENSHOTS+'/close-'+width+'.png'});}await page.setViewportSize({width:1440,height:1000});}
 await page.getByLabel('Bot API username').fill('operator');await page.getByLabel('Bot API password').fill('synthetic');
 await page.getByRole('button',{name:'Confirm market close',exact:true}).click();
 await page.getByRole('status').filter({hasText:'Exit request accepted'}).waitFor();
 assert.equal(closeRequests,1);assert.equal(await page.getByRole('button',{name:'Confirm market close',exact:true}).isDisabled(),true);
 assert.equal(await page.getByLabel('Bot API password').inputValue(),'');
 await page.getByRole('button',{name:'Dismiss',exact:true}).click();
 await page.getByRole('button',{name:'All exits',exact:true}).first().click();await page.getByRole('button',{name:'Expand chart',exact:true}).first().click();await page.waitForTimeout(400); assert.equal(await page.locator('.trade-card.expanded').count(),1);const expanded=await page.locator('.trade-card.expanded .trade-chart').boundingBox();assert(expanded.width>1200&&expanded.height>=600);
 await page.keyboard.press('Escape');assert.equal(await page.locator('.trade-card.expanded').count(),0);await page.setViewportSize({width:390,height:844});await page.waitForTimeout(400);
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);await page.setViewportSize({width:1440,height:1000}); await page.evaluate(()=>Alpine.$data(document.body).setTab('bot:killers-ft'));await page.waitForTimeout(500);
 for(const width of [390,768,1440]){await page.setViewportSize({width,height:1000});for(const tab of ['live','portfolio','trades','dryrun','bot:killers-ft']){await page.evaluate(tab=>Alpine.$data(document.body).setTab(tab),tab);await page.waitForTimeout(200);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`${tab} overflow at ${width}`);
 if(tab==='portfolio'){for(let pass=0;pass<3;pass++){await page.evaluate(()=>Alpine.$data(document.body).renderPortfolioCharts());await page.waitForTimeout(100);const geometry=await page.locator('#chart-portfolio-equity [data-series]').evaluate(el=>({width:el.getBBox().width,plot:el.closest('svg').getBoundingClientRect().width}));assert(geometry.width>geometry.plot*.65,`Equity must fill plot after render/resize: ${JSON.stringify(geometry)}`);}}
 assert.equal((await page.locator('main:visible').innerText()).includes('undefined'),false);}}
 await page.emulateMedia({colorScheme:'light'});await page.evaluate(()=>Alpine.$data(document.body).openPositionsTab());await page.waitForTimeout(1000);
 const rgb=hex=>{const h=hex.trim().replace('#','');return [0,2,4].map(i=>parseInt(h.slice(i,i+2),16));};
 const theme=()=>page.evaluate(()=>{const root=document.documentElement,token=name=>getComputedStyle(root).getPropertyValue(name).trim();return {theme:root.dataset.theme,scheme:getComputedStyle(root).colorScheme,bg:getComputedStyle(document.body).backgroundColor,bgToken:token('--bg-0'),chartToken:token('--chart-bg'),gridToken:token('--hairline'),label:document.querySelector('.theme-toggle').getAttribute('aria-label')};});
 const chartPixel=()=>page.locator('.trade-chart').first().evaluate(el=>{const canvas=[...el.querySelectorAll('canvas')].sort((a,b)=>b.width*b.height-a.width*a.height)[0];return Array.from(canvas.getContext('2d').getImageData(2,2,1,1).data.slice(0,3));});
 let themed=await theme();assert.equal(themed.theme,'light');assert.equal(themed.label,'Theme: system. Switch theme');assert.deepEqual(await chartPixel(),rgb(themed.chartToken));
 const toggle=page.locator('.theme-toggle');await toggle.focus();await page.keyboard.press('Enter');await page.waitForTimeout(200);assert.equal((await theme()).theme,'light');assert.equal((await theme()).label,'Theme: light. Switch theme');await page.keyboard.press('Enter');await page.waitForTimeout(400);
 themed=await theme();assert.equal(themed.theme,'dark');assert.equal(themed.scheme,'dark');assert.equal(themed.bg,`rgb(${rgb(themed.bgToken).join(', ')})`);assert.notDeepEqual(rgb(themed.bgToken),[244,246,248]);assert.equal(themed.label,'Theme: dark. Switch theme');
 assert.deepEqual(await chartPixel(),rgb(themed.chartToken),'Position chart background must follow the theme');
 await page.evaluate(()=>Alpine.$data(document.body).setTab('portfolio'));await page.waitForTimeout(500);
 assert.equal(await page.locator('#chart-portfolio-equity svg line').first().getAttribute('stroke'),themed.gridToken);
 await toggle.click();await page.waitForTimeout(200);assert.equal(await page.locator('#chart-portfolio-equity svg line').first().getAttribute('stroke'),(await theme()).gridToken,'Analytics must redraw on theme change');assert.notEqual((await theme()).gridToken,themed.gridToken);
 await toggle.click();await toggle.click();await page.waitForTimeout(200);
 await page.reload();await page.waitForTimeout(1800);themed=await theme();assert.equal(themed.theme,'dark','Theme preference must persist across reload');assert.equal(themed.label,'Theme: dark. Switch theme');
 await page.evaluate(()=>Alpine.$data(document.body).openPositionsTab());await page.waitForTimeout(1000);const logoFill=(await page.locator('.trade-chart #tv-attr-logo path[fill="var(--fill)"]').first().evaluate(el=>getComputedStyle(el).fill)).match(/\d+/g).slice(0,3).map(Number);const logoRatio=contrast(logoFill,rgb(themed.chartToken));assert(logoRatio>=3,`TradingView logo created in dark must stay legible: ${logoRatio}`);console.log('Dark logo contrast at creation',logoRatio);
 await toggle.click();await page.waitForTimeout(200);assert.equal((await theme()).theme,'light');
 await page.emulateMedia({colorScheme:'dark'});await page.waitForTimeout(200);assert.equal((await theme()).theme,'dark','System must follow prefers-color-scheme');
 await page.emulateMedia({colorScheme:'light'});await page.waitForTimeout(200);assert.equal((await theme()).theme,'light');
 await page.reload();await page.waitForTimeout(1800);assert.equal((await theme()).label,'Theme: system. Switch theme');
 assert.deepEqual(errors,[]);console.log('Workspace overview, positions, expansion, Escape, mobile and bot detail, theme passed');await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
