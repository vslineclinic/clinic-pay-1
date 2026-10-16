import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';

// Minimal DOM + deterministic browser clock; execute the shipped scripts, never a copy of their logic.
export function browser(initial={}) {
  let now=0, sequence=0;
  const timers=new Map(), messages=[], listeners=new Map(), document={activeElement:null};
  class Element {
    constructor(tag) {
      this.tag=tag;this.children=[];this.dataset={};this.listeners=new Map();this.attributes={};
      this.className='';this.value='';this.style={};this.hidden=false;this._text='';
      this.classList={
        contains:value=>this.className.split(' ').includes(value),
        add:(...values)=>{this.className=[...new Set([...this.className.split(' ').filter(Boolean),...values])].join(' ');},
        remove:(...values)=>{this.className=this.className.split(' ').filter(value=>!values.includes(value)).join(' ');},
        toggle:(value,force)=>{const add=force??!this.classList.contains(value);this.classList[add?'add':'remove'](value);return add;}
      };
    }
    get parentElement(){return this.parent;}
    get textContent(){return this._text+this.children.map(child=>child.textContent).join('');}
    set textContent(value){this.replaceChildren();this._text=String(value??'');}
    get isConnected(){return this===document.body||!!this.parent?.isConnected;}
    append(...children) {for(const child of children){child.remove();child.parent=this;this.children.push(child);}}
    appendChild(child){this.append(child);return child;}
    replaceChildren(...children) {for(const child of this.children)child.parent=null;this.children=[];this._text='';this.append(...children);}
    remove() {if(this.parent)this.parent.children=this.parent.children.filter(child=>child!==this);this.parent=null;}
    setAttribute(name,value) {
      this.attributes[name]=String(value);
      if(name.startsWith('data-'))this.dataset[name.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=String(value);
      else this[name==='class'?'className':name]=value;
    }
    getAttribute(name){return name.startsWith('data-')?this.dataset[name.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]??null:this.attributes[name]??this[name]??null;}
    removeAttribute(name){delete this.attributes[name];delete this[name];}
    addEventListener(type,callback) {this.listeners.set(type,callback);}
    contains(other){return other===this||this.children.some(child=>child.contains(other));}
    matches(selector) {
      return selector.split(',').some(part=>{
        part=part.trim();
        const attributes=[...part.matchAll(/\[([\w-]+)(?:=["']?([^\]"']+)["']?)?\]/g)];
        part=part.replace(/\[[^\]]+\]/g,'');
        if(attributes.some(([,name,value])=>{const actual=this.getAttribute(name);return actual===null||(value!==undefined&&String(actual)!==value);}))return false;
        const id=part.match(/#([\w-]+)/)?.[1];if(id&&this.id!==id)return false;
        if([...part.matchAll(/\.([\w-]+)/g)].some(([,value])=>!this.classList.contains(value)))return false;
        const tag=part.match(/^[\w-]+/)?.[0];return !tag||this.tag===tag;
      });
    }
    closest(selector) {return this.matches(selector)?this:this.parent?.closest(selector);}
    querySelectorAll(selector) {return this.children.flatMap(child=>[...(child.matches(selector)?[child]:[]),...child.querySelectorAll(selector)]);}
    querySelector(selector) {return this.querySelectorAll(selector)[0]??null;}
    focus() {const previous=document.activeElement;if(previous===this)return;document.activeElement=this;previous?.listeners.get('blur')?.({relatedTarget:this});}
    setSelectionRange(start,end){this.selectionStart=start;this.selectionEnd=end;}
    scrollIntoView(){}
    click(){if(!this.disabled){this.onclick?.({target:this,preventDefault(){}});this.listeners.get('click')?.({target:this,preventDefault(){}});}}
  }
  document.body=new Element('body');document.body.scrollHeight=100;
  document.createElement=tag=>new Element(tag);
  document.getElementById=id=>document.body.querySelector(`#${id}`);
  document.querySelector=selector=>document.body.querySelector(selector);
  document.querySelectorAll=selector=>document.body.querySelectorAll(selector);
  const html=readFileSync(new URL('../assets/ledger_editor/index.html',import.meta.url),'utf8');
  const stack=[document.body];
  for(const [,closing,tag,attributes]of html.matchAll(/<(\/)?([a-z][\w-]*)([^>]*)>/g)) {
    if(['html','head','body','meta','link','script','title'].includes(tag))continue;
    if(closing){if(stack.at(-1).tag===tag)stack.pop();continue;}
    const element=new Element(tag);
    for(const [,name,quoted,bare]of attributes.matchAll(/([\w-]+)(?:="([^"]*)"|=([^\s>]+))?/g))element.setAttribute(name,quoted??bare??true);
    stack.at(-1).append(element);if(!['input','br','hr'].includes(tag))stack.push(element);
  }
  const elements=Object.fromEntries(document.body.querySelectorAll('[id]').map(element=>[element.id,element]));
  const window={parent:{postMessage:message=>messages.push({at:now,...message})},addEventListener:(type,callback)=>listeners.set(type,callback)};
  const schedule=(callback,delay,period=0)=>{const id=++sequence;timers.set(id,{at:now+delay,callback,period});return id;};
  const context={window,document,crypto:{randomUUID:()=>`request-${++sequence}`},
    Date:{now:()=>now},setTimeout:(callback,delay)=>schedule(callback,delay),clearTimeout:id=>timers.delete(id),
    setInterval:(callback,period)=>schedule(callback,period,period),confirm:()=>true,prompt:()=>null};
  const scripts=[...html.matchAll(/<script src="([^"]+)"/g)].map(([,src])=>readFileSync(new URL(`../assets/ledger_editor/${src}`,import.meta.url),'utf8'));
  scripts.splice(1,0,'const LedgerEditor=window.LedgerEditor;');
  runInNewContext(scripts.join('\n'),context);
  const rows=['a','b'].map(id=>({id,receipt_revision:1,draft_revision:0,raw:{chart_no:id,patient_name:'합성',payments:{카드:'100'}}}));
  const render=(extra={})=>listeners.get('message')({data:{type:'streamlit:render',args:{scope:'day',channels:['카드'],rows,...initial,...extra}}});
  const field=(name,id='a')=>elements.rows.children.find(row=>row.dataset.id===id).querySelectorAll('input,select').find(input=>input.dataset.field===name);
  const event=(input,type,extra={})=>input.listeners.get(type)?.({target:input,preventDefault(){},stopPropagation(){},...extra});
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
  return {render,field,event,edit,advance,messages,document,elements,
    commands:()=>messages.filter(message=>message.type==='streamlit:setComponentValue'&&message.value.kind!=='checkpoint'),
    checkpoints:()=>messages.filter(message=>message.type==='streamlit:setComponentValue'&&message.value.kind==='checkpoint')};
}
