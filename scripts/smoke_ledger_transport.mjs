import assert from 'node:assert/strict';
import { browser } from './ledger_browser.mjs';

{
  const b=browser();b.edit('patient_name','수정');
  assert.equal(b.checkpoints().length,1,'each edit still checkpoints before its DB save');
  b.advance(999);assert.equal(b.commands().length,0,'editing waits the full 1 second');
  b.advance(1);assert.equal(b.commands().length,1,'editing saves after 1 second');
  assert.equal(b.commands()[0].value.raw.patient_name,'수정');
}
{
  const b=browser();b.edit('patient_name','계속 입력');b.advance(300);
  b.field('카드').focus();b.advance(0);
  assert.equal(b.commands().length,0,'moving within a row must not force an incomplete save');
  b.edit('카드','1000');b.advance(999);assert.equal(b.commands().length,0);
  b.advance(1);assert.equal(b.commands()[0].value.raw.payments.카드,'1000');
}
{
  const b=browser();const input=b.edit('patient_name','Enter 저장');
  b.event(input,'keydown',{key:'Enter',isComposing:false});b.advance(0);
  assert.equal(b.commands().length,1,'Enter still flushes immediately');
}
{
  const b=browser();b.edit('patient_name','다음 행');b.field('chart_no','b').focus();b.advance(0);
  assert.equal(b.commands().length,1,'leaving a row still flushes immediately');
}
{
  const b=browser();const input=b.edit('patient_name','원장 벗어남');b.event(input,'blur',{relatedTarget:null});b.advance(0);
  assert.equal(b.commands().length,1,'leaving the editor still flushes immediately');
}
{
  const b=browser();b.edit('patient_name','주기 조회');b.advance(800);b.render();
  b.advance(199);assert.equal(b.commands().length,0);
  b.advance(1);assert.equal(b.commands().length,1,'server polling must not postpone the pending save');
}
{
  const b=browser();const input=b.field('patient_name');input.focus();b.event(input,'compositionstart');
  b.edit('patient_name','한');b.event(input,'keydown',{key:'Enter',isComposing:true});b.advance(1000);
  assert.equal(b.commands().length,0,'IME text is never submitted mid-composition');
  input.value='한글';b.event(input,'compositionend');b.advance(999);assert.equal(b.commands().length,0);
  b.advance(1);assert.equal(b.commands()[0].value.raw.patient_name,'한글');
}
{
  const b=browser();b.edit('patient_name','첫 저장');b.advance(1000);
  const first=b.commands()[0].value;
  b.edit('patient_name','응답 전 다음 입력');b.advance(1000);
  b.render({ack:{id:'a',request_id:first.request_id,status:'saved',receipt_revision:2,draft_revision:0}});
  b.advance(1000);
  assert.equal(b.commands().length,2,'a fired timer must be cleared so an ACK can queue remaining edits');
  assert.equal(b.commands()[1].value.raw.patient_name,'응답 전 다음 입력');
  assert.equal(b.commands()[1].value.receipt_revision,2);
}
console.log('Ledger editor transport: debounce, row focus, Enter, polling, IME and ACK timing passed');
