/* Streamlit v1 postMessage transport. All data goes to the authenticated Python session. */
(() => {
  let model=null,registry=null,args={},timer=null,lastSentAt=0,lastRequest=null,layout=null,web=null;
  const nodes=new Map(), $=id=>document.getElementById(id);
  const send=(type,data={})=>window.parent.postMessage({isStreamlitMessage:true,type,...data},'*');
  const resize=()=>send('streamlit:setFrameHeight',{height:Math.min(760,document.body.scrollHeight+8)});
  const statusNames={new:'새 행',editing:'입력 중',saving:'저장 중…',saved:'저장완료',draft:'초안 저장 · 검토 필요',conflict:'충돌 · 내 입력 보존',error:'저장 실패 · 내 입력 보존'};
  const empty=()=>({chart_no:'',patient_name:'',note:'',payments:Object.fromEntries(args.channels.map(c=>[c,''])),
    yeoshin_used:false,gangnamunni_used:false,kind:'receipt',refund_of:null});
  function checkpoint() {
    if(registry&&model)send('streamlit:setComponentValue',{value:{kind:'checkpoint',scope:model.scope,checkpoint:registry.snapshot()},dataType:'json'});
  }
  function emit() {
    if (!model || args.closed) return;
    const command=model.next();
    if (!command) {render();return;}
    if (`${command.request_id}:${command.attempt||0}`!==lastRequest || Date.now()-lastSentAt>=4000) {
      send('streamlit:setComponentValue',{value:{...command,checkpoint:registry.snapshot()},dataType:'json'});
      lastSentAt=Date.now();lastRequest=`${command.request_id}:${command.attempt||0}`;
    }
    render();
  }
  function schedule(immediate=false) {
    clearTimeout(timer);
    timer=setTimeout(()=>{timer=null;emit();},immediate?0:1000);
  }
  function fieldInput(row,field,label,{payment=false,checkbox=false,css='',options=null}={}) {
    const input=document.createElement(options?'select':'input');
    if (!options) input.type=checkbox?'checkbox':'text';
    if (payment) {input.inputMode='numeric';input.className='money';}
    if (css) input.classList.add(css);
    input.setAttribute('aria-label',label); input.dataset.field=field;
    if(!options&&!checkbox)input.maxLength=payment?30:field==='chart_no'?80:field==='patient_name'?200:2000;
    if (options) for(const [value,text] of options) {const o=document.createElement('option');o.value=value;o.textContent=text;input.append(o);}
    const change=()=>{
      model.edit(row.id,field,checkbox?input.checked:input.value,payment);
      if(field==='refund_of'){
        const original=model.rows.get(input.value)?.raw||(args.refund_options||[]).find(value=>value.id===input.value);
        if(original){row.raw.chart_no=original.chart_no;row.raw.patient_name=original.patient_name;}
      }
      checkpoint();schedule();if(layout==='web'||['kind','refund_of'].includes(field))render();else renderStatus(row.id);
    };
    input.addEventListener(checkbox||options?'change':'input',change);
    input.addEventListener('compositionstart',()=>model.composing.add(row.id));
    input.addEventListener('compositionend',()=>{model.composing.delete(row.id);change();});
    input.addEventListener('blur',event=>{
      const next=rowRoot(event.relatedTarget);
      if(!event.relatedTarget?.closest?.('.web-local-control')&&next?.dataset.id!==row.id&&!model.composing.has(row.id))schedule(true);
    });
    input.addEventListener('keydown',event=>{
      if(event.key==='Enter'&&!event.isComposing&&!model.composing.has(row.id)) {
        event.preventDefault();schedule(true);
        const all=[...rowRoot(input).querySelectorAll('input,select')].filter(item=>!item.disabled),index=all.indexOf(input);
        if(index<all.length-1)all[index+1].focus();else add();
      }
    });
    return input;
  }
  function cell(row,field,label,options) {
    const td=document.createElement('td');td.append(fieldInput(row,field,label,options));return td;
  }
  const rowRoot=node=>node?.closest?.('[data-id]')||node?.closest?.('tr');
  function add() {
    if(args.closed)return;
    const id=model.add(empty());if(layout==='web')web.select(id);render();
    (layout==='web'?$('web-detail'):nodes.get(id))?.querySelector('input')?.focus();resize();
  }
  function build(row) {
    const tr=document.createElement('tr');tr.dataset.id=row.id;
    const state=document.createElement('td');state.className='state';tr.append(state);
    tr.append(cell(row,'chart_no','차트번호',{css:'chart'}),cell(row,'patient_name','성명',{css:'name'}));
    for(const channel of args.channels)tr.append(cell(row,channel,`${channel} 금액 원`,{payment:true}));
    tr.append(cell(row,'yeoshin_used','여신 사용',{checkbox:true}),cell(row,'gangnamunni_used','강언 사용',{checkbox:true}));
    const total=document.createElement('td');total.className='total';tr.append(total);
    tr.append(cell(row,'kind','거래 구분',{options:[['receipt','수납'],['refund','환불']]}));
    tr.append(cell(row,'refund_of','환불 원거래',{options:[['','원거래 선택']],css:'refund'}));
    tr.append(cell(row,'note','메모',{css:'note'}));
    const action=document.createElement('td');action.append(actions(row));tr.append(action);nodes.set(row.id,tr);return tr;
  }
  function actions(row) {
    const wrap=document.createElement('div');wrap.className='actions';
    const discard=document.createElement('button');discard.type='button';discard.textContent='초안 버리기';
    discard.onclick=()=>{if(confirm('내 초안을 버립니까? 이미 저장된 수납은 유지합니다.')){model.discard(row.id);checkpoint();schedule(true);render();}};
    const voidButton=document.createElement('button');voidButton.type='button';voidButton.textContent='수납 취소';
    voidButton.onclick=()=>{
      const reason=prompt('수납 취소 사유를 입력하세요. 환불은 연결된 환불 행으로 입력합니다.');
      if(reason?.trim()){row.raw._void_reason=reason.trim();row.action='void';row.dirty=true;checkpoint();schedule(true);}
    };
    wrap.append(discard,voidButton);return wrap;
  }
  function renderStatus(id) {
    const row=model.rows.get(id),tr=nodes.get(id);if(!row||!tr)return;
    tr.className=row.voided?'voided':row.status;
    fillStatus(row,tr.querySelector('.state'));
  }
  function fillStatus(row,td) {
    const id=row.id;td.replaceChildren();
    const status=document.createElement('span');status.className=`status ${row.status}`;status.textContent=statusNames[row.status]||row.status;td.append(status);
    if(row.updated_by){const actor=document.createElement('small');actor.textContent=`${row.updated_by} · 개정 ${row.receipt_revision}`;td.append(actor);}
    if(row.message){const note=document.createElement('div');note.className='notice';note.textContent=row.message;td.append(note);}
    if(row.conflict){
      const current=document.createElement('small');current.textContent=`현재 서버: ${JSON.stringify(row.conflict.values||LedgerEditor.receiptRaw(row.conflict))}`;td.append(current);
      const choices=document.createElement('div');choices.className='resolve';
      for(const [value,label] of [['current','서버 값 사용'],['mine','내 값으로 다시 저장']]){
        const button=document.createElement('button');button.type='button';button.textContent=label;
        button.onclick=()=>{model.resolve(id,value);checkpoint();render();schedule(true);};choices.append(button);
      }td.append(choices);
    }
    if(row.failed){const retry=document.createElement('button');retry.type='button';retry.textContent='다시 시도';retry.onclick=()=>{model.retry(id);checkpoint();lastRequest=null;schedule(true);};td.append(retry);}
  }
  function updateInput(row,input) {
    const field=input.dataset.field,payment=args.channels.includes(field);
    if(field==='refund_of'){
      const choices=[['','원거래 선택'],...(args.refund_options||[]).map(r=>[r.id,r.label]),...[...model.rows.values()].filter(r=>r.id!==row.id&&r.receipt_revision&&r.raw.kind!=='refund'&&!r.voided).map(r=>[r.id,`${r.raw.chart_no} ${r.raw.patient_name}`])];
      if(row.raw.refund_of&&!choices.some(([id])=>id===row.raw.refund_of))choices.push([row.raw.refund_of,`원거래 ${row.raw.refund_of}`]);
      if(document.activeElement!==input){input.replaceChildren();for(const [value,label]of choices){const option=document.createElement('option');option.value=value;option.textContent=label;input.append(option);}}
    }
    const value=payment?row.raw.payments[field]??'':row.raw[field]??'';
    if(document.activeElement!==input){if(input.type==='checkbox')input.checked=!!value;else input.value=value;}
    input.disabled=!!args.closed||!!row.voided||(model.active?.id===row.id&&model.active.action==='void')||(row.raw.kind==='refund'&&['chart_no','patient_name'].includes(field));
    if(field==='refund_of')input.disabled ||= row.raw.kind!=='refund';
  }
  function totalText(row) {
    const amounts=Object.values(row.raw.payments),valid=amounts.every(v=>v===''||typeof v==='number'&&Number.isSafeInteger(v)||typeof v==='string'&&/^(\d+|\d{1,3}(,\d{3})+)$/.test(v));
    return valid?`${amounts.reduce((s,v)=>s+BigInt(String(v).replaceAll(',','')||'0'),0n).toLocaleString()}원`:'금액 확인';
  }
  function updateActions(row,wrap) {
    const buttons=wrap.children;buttons[0].hidden=!row.draft_revision&&!row.local;
    buttons[1].hidden=!row.receipt_revision||row.voided;for(const button of buttons)button.disabled=!!args.closed||!!row.failed||!!row.conflict||row.status==='saving';
  }
  function setLayout(value) {
    if(layout===value||model?.composing.size)return;
    layout=value;nodes.clear();$('rows').replaceChildren();web?.reset(false);render();
  }
  function render() {
    if(!model)return;
    if($('legacy-view'))$('legacy-view').hidden=layout==='web';
    if($('web-view'))$('web-view').hidden=layout!=='web';
    for(const value of ['table','web'])$('layout-'+value)?.setAttribute('aria-pressed',String(layout===value));
    if(layout==='web')web.render(model,args);
    else {
      const existing=new Set(model.rows.keys());for(const [id,node] of nodes){if(!existing.has(id)){node.remove();nodes.delete(id);}}
      for(const row of model.rows.values()){
        let tr=nodes.get(row.id);if(!tr){tr=build(row);$('rows').append(tr);}
        for(const input of tr.querySelectorAll('input,select'))updateInput(row,input);
        tr.querySelector('.total').textContent=totalText(row);updateActions(row,tr.querySelector('.actions'));renderStatus(row.id);
      }
    }
    $('add').disabled=!!args.closed;$('closed').hidden=!args.closed;
    const pending=[...model.rows.values()].filter(row=>row.dirty||row.status==='saving').length;
    const review=[...model.rows.values()].filter(row=>row.conflict||row.failed||row.status==='draft').length;
    const notices=[pending?`저장 대기 ${pending}행`:'',review?`검토 필요 ${review}행 · 내 입력 보존`:''].filter(Boolean);
    $('summary').textContent=notices.join(' · ')||(registry.pending()?'다른 날짜·지점에 저장 대기 있음':'서버 저장 상태 표시');resize();
  }
  window.addEventListener('message',event=>{
    if(event.data?.type!=='streamlit:render')return;
    args=event.data.args;
    if(!registry)registry=new LedgerEditor.EditorRegistry(undefined,args.checkpoint);
    if(!web&&window.LedgerWeb&&$('web-view'))web=window.LedgerWeb.create($('web-view'),{fieldInput,updateInput,fillStatus,actions,updateActions,totalText,statusNames,render,resize});
    if(!layout)layout=web&&args.initial_layout==='web'?'web':'table';
    if(!model||model.scope!==args.scope){model=registry.select(args.scope);nodes.clear();$('rows').replaceChildren();web?.reset();lastRequest=null;}
    const acknowledging=args.ack&&model.active&&args.ack.request_id===model.active.request_id&&(args.ack.attempt||0)===(model.active.attempt||0);
    if(args.ack)model.ack(args.ack);
    const focused=rowRoot(document.activeElement)?.dataset.id;
    model.sync(args.rows,new Set(focused?[focused]:[]));
    if(!model.rows.size&&!args.closed)model.add(empty());
    const head=document.createElement('tr');
    for(const label of ['저장 상태','차트번호','성명',...args.channels,'여신 사용','강언 사용','합계','구분','환불 원거래','메모','관리']){const th=document.createElement('th');th.textContent=label;head.append(th);}
    $('header').replaceChildren(head);render();if(acknowledging)checkpoint();if(timer===null)schedule();
  });
  window.addEventListener('beforeunload',event=>{if(registry?.pending()){event.preventDefault();event.returnValue='';}});
  $('add').onclick=add;
  if($('layout-table'))$('layout-table').onclick=()=>setLayout('table');
  if($('layout-web'))$('layout-web').onclick=()=>setLayout('web');
  setInterval(()=>{if(model?.active)emit();},1000);
  send('streamlit:componentReady',{apiVersion:1});resize();
})();
