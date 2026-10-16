import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';

// Exercise the real event handlers and transport with a deterministic browser clock.
function browser() {
  let now=0, sequence=0;
  const timers=new Map(), messages=[], listeners=new Map();
  const document={activeElement:null,body:{scrollHeight:100}};
  class Element {
    constructor(tag) {this.tag=tag;this.children=[];this.dataset={};this.listeners=new Map();this.className='';this.value='';this.classList={add:value=>{this.className+=` ${value}`;}};}
    append(...children) {for(const child of children){child.parent=this;this.children.push(child);}}
    replaceChildren(...children) {this.children=[];this.append(...children);}
    remove() {this.parent.children=this.parent.children.filter(child=>child!==this);}
    setAttribute(name,value) {this[name]=value;}
    addEventListener(type,callback) {this.listeners.set(type,callback);}
    matches(selector) {return selector.startsWith('.')?this.className.split(' ').includes(selector.slice(1)):this.tag===selector;}
    closest(selector) {return this.matches(selector)?this:this.parent?.closest(selector);}
    querySelectorAll(selector) {return this.children.flatMap(child=>[...(selector.split(',').some(part=>child.matches(part))?[child]:[]),...child.querySelectorAll(selector)]);}
    querySelector(selector) {return this.querySelectorAll(selector)[0];}
    focus() {const previous=document.activeElement;document.activeElement=this;previous?.listeners.get('blur')?.({relatedTarget:this});}
  }
  const elements=Object.fromEntries(['rows','header','summary','add','closed'].map(id=>[id,new Element('div')]));
  document.createElement=tag=>new Element(tag);
  document.getElementById=id=>elements[id];
  const window={parent:{postMessage:message=>messages.push({at:now,...message})},addEventListener:(type,callback)=>listeners.set(type,callback)};
  const schedule=(callback,delay,period=0)=>{const id=++sequence;timers.set(id,{at:now+delay,callback,period});return id;};
  const context={window,document,crypto:{randomUUID:()=>`request-${++sequence}`},
    Date:{now:()=>now},setTimeout:(callback,delay)=>schedule(callback,delay),clearTimeout:id=>timers.delete(id),
    setInterval:(callback,period)=>schedule(callback,period,period)};
  runInNewContext(`${readFileSync(new URL('../assets/ledger_editor/state.js',import.meta.url),'utf8')}\nconst LedgerEditor=window.LedgerEditor;\n${readFileSync(new URL('../assets/ledger_editor/editor.js',import.meta.url),'utf8')}`,context);
  const rows=['a','b'].map(id=>({id,receipt_revision:1,draft_revision:0,raw:{chart_no:id,patient_name:'합성',payments:{카드:'100'}}}));
  const render=(extra={})=>listeners.get('message')({data:{type:'streamlit:render',args:{scope:'day',channels:['카드'],rows,...extra}}});
  const field=(name,id='a')=>elements.rows.children.find(row=>row.dataset.id===id).querySelectorAll('input,select').find(input=>input.dataset.field===name);
  const event=(input,type,extra={})=>input.listeners.get(type)?.({preventDefault(){},...extra});
  const edit=(name,value,id='a')=>{const input=field(name,id);input.focus();input.value=value;event(input,'input');return input;};
  function advance(milliseconds) {
    const end=now+milliseconds;
    for(;;) {
      const due=[...timers].filter(([,timer])=>timer.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];
      if(!due)break;
      const [id,timer]=due;now=timer.at;timers.delete(id);
      if(timer.period)timers.set(id,{...timer,at:now+timer.period});
      timer.callback();
    }
    now=end;
  }
  render();
  return {render,field,event,edit,advance,messages,
    commands:()=>messages.filter(message=>message.type==='streamlit:setComponentValue'&&message.value.kind!=='checkpoint'),
    checkpoints:()=>messages.filter(message=>message.type==='streamlit:setComponentValue'&&message.value.kind==='checkpoint')};
}

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
