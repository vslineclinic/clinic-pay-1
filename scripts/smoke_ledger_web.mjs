import assert from 'node:assert/strict';
import { browser } from './ledger_browser.mjs';

const channels=['카드','현금','이체','여신티켓','강남언니','나만의닥터','제로페이','기타지역화폐','알리페이','위챗페이','카카오페이','간편결제','바비톡','닥터나우'];
const row=(id,name=`합성 ${id}`)=>({id,receipt_revision:1,draft_revision:0,raw:{chart_no:id,patient_name:name,kind:'receipt',refund_of:null,note:'',payments:Object.fromEntries(channels.map((channel,i)=>[channel,String(i+1)])),yeoshin_used:false,gangnamunni_used:false}});
const initial={initial_layout:'web',channels,rows:[row('a'),row('b')],totals:{gross:'210',refund:'0',net:'210',receipt_count:2,refund_count:0}};
const byId=(b,id)=>b.document.getElementById(id);
const detail=b=>byId(b,'web-detail');
const field=(b,name)=>detail(b).querySelectorAll('input,select').find(input=>input.dataset.field===name);
const listRow=(b,id)=>byId(b,'web-view').querySelectorAll('.web-receipt').find(element=>element.dataset.id===id);
const select=(b,id)=>listRow(b,id).querySelector('.web-select').click();
const edit=(b,name,value)=>{const input=field(b,name);assert.ok(input,`${name} input exists`);input.focus();input.value=value;b.event(input,input.tag==='select'?'change':'input');return input;};
const button=(b,label)=>byId(b,'web-view').querySelectorAll('button').find(element=>element.textContent===label);
const values=b=>b.messages.filter(message=>message.type==='streamlit:setComponentValue');
const payments=command=>JSON.parse(JSON.stringify(command.raw.payments));

{
  const b=browser();
  assert.equal(byId(b,'legacy-view').hidden,false,'A remains the default');
  assert.equal(byId(b,'web-view').hidden,true);
  assert.ok(byId(b,'layout-web'),'B layout button must exist');
  byId(b,'layout-web').click();
  assert.equal(byId(b,'web-view').hidden,false);
  b.render();
  assert.equal(byId(b,'web-view').hidden,false,'server refresh does not undo a local layout choice');
}
{
  const b=browser(initial),before=values(b).length;
  const search=byId(b,'web-search');search.value='합성 b';b.event(search,'input');
  assert.equal(listRow(b,'a').hidden,true,'search hides unmatched receipts locally');
  assert.equal(listRow(b,'b').hidden,false);
  search.value='';b.event(search,'input');select(b,'b');
  assert.equal(detail(b).dataset.id,'b');
  byId(b,'web-filter').value='refund';b.event(byId(b,'web-filter'),'change');
  assert.equal(listRow(b,'a').hidden,true);
  byId(b,'layout-table').click();byId(b,'layout-web').click();
  assert.equal(values(b).length,before,'search, selection, filters and layout changes make no server request');
}
{
  const rows=Array.from({length:100},(_,i)=>row(String(i)));
  const b=browser({...initial,rows});
  assert.equal(byId(b,'web-view').querySelectorAll('.web-receipt').length,100);
  assert.ok(b.document.querySelectorAll('input,select').length<=230,'B mounts one detail form, not 100 full editable rows');
  for(const channel of channels)assert.ok(field(b,channel),`${channel} remains editable`);
  for(const name of ['chart_no','patient_name','note','kind','refund_of'])assert.ok(field(b,name));
  const usage=listRow(b,'0').querySelectorAll('input').find(input=>input.dataset.field==='yeoshin_used');
  assert.equal(usage.type,'checkbox');usage.checked=true;b.event(usage,'change');b.advance(1000);
  assert.equal(b.commands().at(-1).value.raw.yeoshin_used,true);
  assert.deepEqual(payments(b.commands().at(-1).value),rows[0].raw.payments,'usage checkbox changes no payment amount');
}
{
  const b=browser(initial);edit(b,'patient_name','B에서 입력');b.advance(1000);
  const first=b.commands()[0].value;
  byId(b,'layout-table').click();
  assert.equal(b.field('patient_name').value,'B에서 입력');
  b.edit('patient_name','A에서 이어 입력');byId(b,'layout-web').click();
  assert.equal(field(b,'patient_name').value,'A에서 이어 입력');
  assert.equal(b.commands().length,1,'layout switch preserves the active request without submitting another');
  b.render({ack:{id:'a',request_id:first.request_id,status:'saved',receipt_revision:2,draft_revision:0}});
  b.advance(1000);
  assert.equal(b.commands().length,2);
  assert.equal(b.commands()[1].value.raw.patient_name,'A에서 이어 입력');
  assert.equal(b.commands()[1].value.receipt_revision,2,'queued input follows the acknowledged revision');
  assert.equal(first.raw.patient_name,'B에서 입력','inflight payload remains immutable');
}
{
  const b=browser(initial),input=field(b,'patient_name');input.focus();input.setSelectionRange(1,2);
  b.event(input,'compositionstart');edit(b,'patient_name','한');
  select(b,'b');assert.equal(detail(b).dataset.id,'a','row selection must not detach an active IME composition');
  byId(b,'layout-table').click();assert.equal(byId(b,'web-view').hidden,false,'layout changes wait until composition ends');
  b.render({rows:[row('a','다른 작업자'),row('b')]});
  assert.equal(field(b,'patient_name'),input,'poll keeps the live input DOM node');
  assert.equal(b.document.activeElement,input);
  assert.equal(input.value,'한');assert.equal(input.selectionStart,1);assert.equal(input.selectionEnd,2);
  b.advance(1000);assert.equal(b.commands().length,0,'polling must not release an active Korean IME composition');
  input.value='한글';b.event(input,'compositionend');b.advance(1000);
  assert.equal(b.commands().at(-1).value.raw.patient_name,'한글');
}
{
  const b=browser({...initial,totals:{gross:'9223372036854775807',refund:'1',net:'9223372036854775806',receipt_count:2,refund_count:1}});
  assert.match(byId(b,'web-gross').textContent,/9,223,372,036,854,775,807/,'confirmed BIGINT totals are not rounded through Number');
  assert.match(byId(b,'web-net').textContent,/9,223,372,036,854,775,806/);
  const confirmed=byId(b,'web-gross').textContent;
  edit(b,'카드','999999');assert.equal(byId(b,'web-gross').textContent,confirmed,'pending input never changes confirmed summary');
  assert.match(byId(b,'summary').textContent,/저장 대기|입력 중/);
}
{
  const b=browser(initial);edit(b,'patient_name','첫 날짜 미저장');
  b.render({scope:'other-day',rows:[row('a','다른 날짜')]});
  assert.equal(field(b,'patient_name').value,'다른 날짜','same receipt ID in another scope cannot expose the prior input');
  b.render();assert.equal(field(b,'patient_name').value,'첫 날짜 미저장');
  b.advance(1000);assert.equal(b.commands().at(-1).value.scope,'day');
  assert.equal(b.commands().at(-1).value.raw.patient_name,'첫 날짜 미저장');
}
{
  const b=browser(initial);edit(b,'patient_name','충돌 입력');b.advance(1000);const sent=b.commands()[0].value;
  b.render({ack:{id:'a',request_id:sent.request_id,status:'conflict',current:{...row('a').raw,revision:3,patient_name:'서버 입력'},message:'동시 수정'}});
  assert.equal(field(b,'patient_name').value,'충돌 입력');
  assert.ok(button(b,'서버 값 사용'));const mine=button(b,'내 값으로 다시 저장');assert.ok(mine);mine.click();b.advance(0);
  const resolved=b.commands().at(-1).value;assert.equal(resolved.receipt_revision,3);assert.equal(resolved.raw.patient_name,'충돌 입력');
  b.render({ack:{id:'a',request_id:resolved.request_id,status:'error',message:'통신 실패'}});
  assert.match(byId(b,'summary').textContent,/검토 필요/,'current-scope failures must not be labeled as another date');
  const retry=button(b,'다시 시도');assert.ok(retry);retry.click();b.advance(0);
  const retried=b.commands().at(-1).value;assert.equal(retried.request_id,resolved.request_id);assert.equal(retried.attempt,1);
}
{
  const b=browser(initial);edit(b,'kind','refund');edit(b,'refund_of','b');b.advance(1000);
  const command=b.commands().at(-1).value;
  assert.equal(command.raw.kind,'refund');assert.equal(command.raw.refund_of,'b');
  assert.equal(command.raw.chart_no,'b');assert.equal(command.raw.patient_name,'합성 b');
  assert.equal(field(b,'chart_no').disabled,true);assert.equal(field(b,'patient_name').disabled,true);
  b.render({closed:true});
  assert.ok(byId(b,'web-view').querySelectorAll('input,select').filter(input=>input.dataset.field).every(input=>input.disabled),'closed day disables all receipt controls in B');
}
{
  const b=browser(initial);
  byId(b,'web-search').value='없는 성명';b.event(byId(b,'web-search'),'input');
  byId(b,'web-filter').value='refund';b.event(byId(b,'web-filter'),'change');
  byId(b,'add').click();
  const added=listRow(b,detail(b).dataset.id);
  assert.ok(added);assert.equal(added.hidden,false,'adding a receipt reveals it even under an incompatible filter');
  assert.equal(byId(b,'web-search').value,'');assert.equal(byId(b,'web-filter').value,'all');
  assert.equal(b.document.activeElement,field(b,'chart_no'));
}
console.log('Ledger web layout: local controls, bounded DOM, shared A/B state, IME, exact totals, scopes, conflicts, retry and refund passed');
