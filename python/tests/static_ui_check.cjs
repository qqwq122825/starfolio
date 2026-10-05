// Source-level render and event-dispatch smoke tests. NOT browser/visual QA.
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert');
const nodes={};
function node(sel){return nodes[sel]??={innerHTML:'',textContent:'',value:'',classList:{toggle(){},add(){},remove(){}},open:false,showModal(){this.open=true},close(){this.open=false}}}
const state={mode:'demo',csrf_token:'test',server_time:'2026-10-05T00:00:00Z',watchlist:[],alerts:[],news:[],runs:[],audit:[],portfolio:[],digest:'示例日报',monitor:{freshness_minutes:20},rule_version:'evidence-v1',paper:{initial_capital:100000,cash:100000,decisions:[],fills:[],exit_plans:[],exit_events:[],exit_disclaimer:'TEST ONLY · 未经收益验证',status:'hold_missing_calendar',disclaimer:'纯模拟'}};
const requests=[],listeners={};let delayPost=null,failPost=false;
const ctx={console,document:{querySelector:node,querySelectorAll:()=>[],addEventListener:(name,handler)=>listeners[name]=handler,hidden:false},window:{addEventListener(){}},location:{hash:''},setInterval(){},setTimeout(){},FormData:function(form){return Object.entries(form.values)},fetch:async(path,opts)=>{if(opts?.method==='POST'){requests.push({path,data:JSON.parse(opts.body)});assert.equal(opts.headers['X-Watch-Token'],'test');if(delayPost)await delayPost;if(failPost)return {ok:false,json:async()=>({error:'测试拒绝：证据缺失'})}}return {ok:true,json:async()=>state}},Blob,URL};
vm.createContext(ctx);vm.runInContext(fs.readFileSync(require('node:path').join(__dirname,'../static/app.js'),'utf8'),ctx);
function form(id,values,kind){const button={disabled:false};return {id,values,dataset:kind?{kind}:{},querySelector:()=>button,button}}
async function submit(target){await listeners.submit({target,preventDefault(){}})}
function assertRoutes(paths){assert.deepEqual(requests.map(r=>r.path),paths);requests.length=0}
setImmediate(async()=>{try{
 // Empty pages and offline/live fallback.
 const paper=state.paper;
 for(const mode of ['demo','imported','live']){
  state.mode=mode;state.paper=mode==='live'?null:paper;
  for(const page of ['overview','watchlist','news','portfolio','alerts','settings']){
   vm.runInContext(`go('${page}')`,ctx);assert(nodes['#content'].innerHTML.length>100)
  }
 }
 state.mode='imported';state.paper=paper;
 // Populated records, including adversarial user-entered text.
 state.watchlist=[{symbol:'600000',name:'<script>alert(1)</script>',snapshot:null}];
 vm.runInContext("go('watchlist')",ctx);assert(!nodes['#content'].innerHTML.includes('<script>'));assert(nodes['#content'].innerHTML.includes('&lt;script&gt;'));
 state.news=[{symbol:'600000',stage:'order',verification:'reviewed',title:'测试公告 <script>x</script>',evidence_excerpt:'原文测试',expectation_status:'priced_in',expectation_evidence:'事前基准 <img src=x onerror=x>',price_reaction:'down',price_reaction_evidence:'观察区间测试',research_assessment:{action:'HOLD',status:'insufficient_evidence',reason:'预期仍需核验'}}];
 vm.runInContext("go('news')",ctx);let html=nodes['#content'].innerHTML;
 assert(html.includes('HOLD'));assert(html.includes('可能已计价（人工判断）'));assert(html.includes('观察窗口下跌'));assert(html.includes('&lt;img'));assert(!html.includes('<img'));assert(!html.includes('<script>'));
 const plan={plan_id:'plan <script>x</script>',symbol:'600000',source:'user',status:'active',reason:'测试理由',reference_price:10,quantity:200,high_water_mark:12.5,remaining_quantity:100,last_observed_at:'2026-10-05T00:00:00Z',stages:[{gain_pct:0.1,quantity:100}],trailing:{activation_gain_pct:0.15,distance_pct:0.05},completed_stages:[0],trailing_active:true,pending_decision_id:'pending-1',block_reason:'T+1 <script>x</script>'};
 paper.exit_plans=[plan];paper.exit_events=[{plan_id:plan.plan_id,event:'triggered',at:'2026-10-05T00:00:00Z',details:{reason:'<script>event</script>'}}];
 vm.runInContext("go('portfolio')",ctx);html=nodes['#content'].innerHTML;
 for(const expected of ['TEST ONLY','高水位','12.5','剩余 100 / 200','T+1 &lt;script&gt;','pending-1','退出计划审计','未经收益验证'])assert(html.includes(expected),expected);
 assert(!html.includes('<script>'));
 // Both analyst axes default to unknown in the form, with separate text fields.
 vm.runInContext('newsForm()',ctx);html=nodes['#modal-content'].innerHTML;
 for(const field of ['stage','evidence_excerpt','expectation_status','expectation_evidence','price_reaction','price_reaction_evidence'])assert(html.includes(`name="${field}"`));
 assert(html.includes('价格上涨升级事实'));assert(html.includes('value="unknown"'));
 vm.runInContext("importForm('paper-exit')",ctx);html=nodes['#modal-content'].innerHTML;
 assert(html.includes('test_only'));assert(html.includes('reference_price'));assert(html.includes('0.1 表示 10%'));
 vm.runInContext(`exitCancelForm(${JSON.stringify(plan.plan_id)})`,ctx);assert(nodes['#modal-content'].innerHTML.includes('&lt;script&gt;'));assert(!nodes['#modal-content'].innerHTML.includes('<script>'));
 assert.throws(()=>vm.runInContext("exitCancelForm('no-longer-active')",ctx),/活动计划已变更/);
 // Regression: old dispatch parsed every non-import form as paper JSON and ignored paper imports.
 await submit(form('watch-form',{symbol:'600000',name:'测试'}));assertRoutes(['/api/watchlist']);
 await submit(form('news-form',{symbol:'600000',title:'测试'}));assertRoutes(['/api/news','/api/mode']);
 await submit(form('import-form',{content:'symbol,name,shares,cost\n600000,测试,100,10'},'portfolio'));assertRoutes(['/api/import/portfolio']);
 await submit(form('import-form',{content:'[]'},'market'));assertRoutes(['/api/import/market','/api/mode']);
 await submit(form('import-form',{content:'{"decision_id":"test"}'},'paper'));assertRoutes(['/api/paper/decision']);
 await submit(form('import-form',{content:'{"plan_id":"test","test_only":true}'},'paper-exit'));assert.equal(requests[0].data.test_only,true);assertRoutes(['/api/paper/exit-plan']);
 await submit(form('exit-cancel-form',{plan_id:'test',mode:'imported',reason:'测试'}));assertRoutes(['/api/paper/exit-cancel']);
 // Malformed input stays in the modal and is never transmitted.
 node('#modal').open=true;const malformed=form('import-form',{content:'{'},'paper-exit');await submit(malformed);assertRoutes([]);assert.equal(node('#modal').open,true);assert.equal(malformed.button.disabled,false);
 // Failed requests allow retry; errors do not pretend that saving succeeded.
 failPost=true;const rejected=form('import-form',{content:'{}'},'paper-exit');await submit(rejected);assertRoutes(['/api/paper/exit-plan']);assert.equal(node('#modal').open,true);assert.equal(rejected.button.disabled,false);assert(node('#toast').textContent.includes('证据缺失'));failPost=false;
 // Repeated submit while pending emits only one mutation.
 let release;delayPost=new Promise(resolve=>release=resolve);const repeated=form('import-form',{content:'{}'},'paper-exit');const first=submit(repeated),second=submit(repeated);await second;assert.equal(requests.length,1);release();await first;delayPost=null;assertRoutes(['/api/paper/exit-plan']);assert.equal(repeated.button.disabled,false);
 vm.runInContext('closeModal()',ctx);assert.equal(node('#modal').open,false);
 state.mode='live';state.paper=null;vm.runInContext("go('portfolio')",ctx);assert(!nodes['#content'].innerHTML.includes('id="paper-exit-import"'));assert.throws(()=>vm.runInContext("importForm('paper-exit')",ctx),/实时模式未接入/);
 console.log('Static UI: 18 empty mode/page renders, populated evidence/exit records, escaping, seven form routes, malformed/error/repeated-submit and close flows PASS. Browser/visual QA NOT RUN.');
}catch(error){console.error(error);process.exitCode=1}});
