/* A second view of the same editor state; storage and commands stay in editor.js. */
(() => {
  const make=(tag,css,text)=>{const node=document.createElement(tag);if(css)node.className=css;if(text!==undefined)node.textContent=text;return node;};
  window.LedgerWeb={create(host,helpers) {
    let model=null,args={},selected=null,detailId=null;
    const items=new Map();
    const heading=make('div','web-heading'),title=make('div'),context=make('small','web-context');
    title.append(make('span','web-eyebrow','RECEIPT LEDGER'),make('h2',null,'수납 원장'),context);
    heading.append(title,make('span','web-view-badge','B · 웹 원장'));host.append(heading);
    const stats=make('div','web-stats'),values={};
    for(const [key,label] of [['gross','총 수납'],['refund','총 환불'],['net','순수납']]) {
      const card=make('div','web-stat'),value=make('strong');value.id='web-'+key;values[key]=value;
      card.append(make('span',null,label),value);stats.append(card);
    }
    host.append(stats,make('p','web-confirmed-note','서버에 저장된 수납·환불 합계입니다. 입력 중인 금액과 초안은 포함하지 않으며, 정산 대조 결과와 별도입니다.'));
    const workspace=make('div','web-workspace'),listPanel=make('section','web-list-panel');
    const listHead=make('div','web-panel-head'),count=make('span','web-count');listHead.append(make('h3',null,'수납 내역'),count);
    const filters=make('div','web-filters'),search=make('input','web-local-control'),filter=make('select','web-local-control');
    search.id='web-search';search.type='search';search.placeholder='차트번호 · 성명 · 메모 검색';search.setAttribute('aria-label','수납 내역 검색');
    filter.id='web-filter';filter.setAttribute('aria-label','수납 내역 표시');
    for(const [value,label] of [['all','전체'],['pending','입력·검토 중'],['refund','환불'],['voided','취소']]){const option=make('option',null,label);option.value=value;filter.append(option);}
    filter.value='all';filters.append(search,filter);
    const labels=make('div','web-list-labels');labels.append(make('span',null,'성명 / 결제수단'),make('span',null,'금액 / 저장 상태'),make('span',null,'여신'),make('span',null,'강언'));
    const list=make('div','web-list'),empty=make('p','web-empty','표시할 내역이 없습니다. 검색 조건을 바꾸거나 다음 행을 추가하세요.');
    listPanel.append(listHead,filters,labels,list,empty);
    const detail=make('section','web-detail');detail.id='web-detail';detail.setAttribute('aria-label','선택한 수납 상세 입력');
    workspace.append(listPanel,detail);host.append(workspace);
    search.addEventListener('input',()=>{renderList();helpers.resize();});
    filter.addEventListener('change',()=>{renderList();helpers.resize();});
    function choose(id) {if(model?.composing.size)return;selected=id;renderDetail();renderList();helpers.resize();}
    function buildItem(row) {
      const node=make('div','web-receipt');node.dataset.id=row.id;
      const button=make('button','web-select web-local-control');button.type='button';button.setAttribute('aria-label','수납 상세 선택');
      const patient=make('strong','web-patient'),chart=make('small','web-chart'),payments=make('small','web-payments');button.append(patient,chart,payments);button.onclick=()=>choose(row.id);
      const figures=make('div','web-figures'),amount=make('strong','web-row-total'),state=make('span','web-row-state');figures.append(amount,state);
      node.append(button,figures);
      for(const [field,label] of [['yeoshin_used','여신 사용'],['gangnamunni_used','강언 사용']]) {
        const wrap=make('label','web-usage');wrap.setAttribute('aria-label',label);
        wrap.append(helpers.fieldInput(row,field,label,{checkbox:true}));node.append(wrap);
      }
      const item={node,button,patient,chart,payments,amount,state};items.set(row.id,item);list.append(node);return item;
    }
    function renderList() {
      if(!model)return;
      const query=search.value.trim().toLocaleLowerCase(),show=filter.value;
      for(const [id,item] of items)if(!model.rows.has(id)){item.node.remove();items.delete(id);}
      let visible=0;
      for(const row of model.rows.values()) {
        const item=items.get(row.id)||buildItem(row),raw=row.raw;
        const matches=[raw.chart_no,raw.patient_name,raw.note].some(value=>String(value||'').toLocaleLowerCase().includes(query));
        const filtered=show==='pending'?['new','editing','saving','draft','conflict','error'].includes(row.status):show==='refund'?raw.kind==='refund':show==='voided'?!!row.voided:true;
        item.node.hidden=!(matches&&filtered);if(!item.node.hidden)visible++;
        item.node.className=`web-receipt ${row.voided?'voided':row.status}${selected===row.id?' selected':''}`;
        item.button.setAttribute('aria-pressed',String(selected===row.id));
        item.button.setAttribute('aria-label',`${raw.patient_name||'새 수납'} · ${raw.chart_no||'차트번호 미입력'} 수납 상세 선택`);
        item.patient.textContent=raw.patient_name||'새 수납';item.chart.textContent=`${raw.chart_no||'차트번호 미입력'}${row.voided?' · 취소':raw.kind==='refund'?' · 환불':''}`;
        item.payments.textContent=Object.entries(raw.payments).filter(([,value])=>value!==''&&value!==0&&value!=='0').map(([key])=>key).join(' · ')||'결제수단 미입력';
        item.amount.textContent=helpers.totalText(row);item.state.textContent=helpers.statusNames[row.status]||row.status;item.state.className=`web-row-state ${row.status}`;
        for(const input of item.node.querySelectorAll('input'))helpers.updateInput(row,input);
      }
      empty.hidden=visible>0;count.textContent=`${visible} / ${model.rows.size}건`;
    }
    function addField(parent,row,field,label,options) {
      const wrap=make('label','web-field');wrap.append(make('span',null,label),helpers.fieldInput(row,field,label,options));parent.append(wrap);
    }
    function renderDetail() {
      const row=model?.rows.get(selected);
      if(!row){detail.replaceChildren(make('p','web-empty','수납 내역을 선택하면 여기에서 바로 수정할 수 있습니다.'));detailId=null;delete detail.dataset.id;return;}
      if(detailId!==row.id) {
        detail.replaceChildren();detail.dataset.id=row.id;detailId=row.id;
        const top=make('div','web-panel-head');top.append(make('h3',null,'수납 상세'),make('span','web-detail-caption','입력 후 자동 저장'));
        const body=make('div','web-detail-body'),identity=make('div','web-field-grid');
        addField(identity,row,'chart_no','차트번호');addField(identity,row,'patient_name','성명');body.append(identity);
        const type=make('div','web-field-grid');addField(type,row,'kind','거래 구분',{options:[['receipt','수납'],['refund','환불']]});addField(type,row,'refund_of','환불 원거래',{options:[['','원거래 선택']]});body.append(type);
        body.append(make('h4',null,'결제 금액'));
        const payments=make('div','web-money-grid');for(const channel of args.channels)addField(payments,row,channel,channel+' 금액 원',{payment:true});body.append(payments);
        const total=make('div','web-detail-total');total.append(make('span',null,'입력 합계 · 저장 전 포함'),make('strong','web-current-total'));body.append(total);
        addField(body,row,'note','메모');body.append(make('p','web-checkbox-note','여신·강언 사용 여부는 왼쪽 수납 내역에서 바로 체크하세요. 금액은 별도로 입력합니다.'));
        body.append(make('div','state web-detail-state'),helpers.actions(row));detail.append(top,body);
      }
      for(const input of detail.querySelectorAll('input,select'))helpers.updateInput(row,input);
      detail.querySelector('.web-current-total').textContent=helpers.totalText(row);
      helpers.fillStatus(row,detail.querySelector('.state'));helpers.updateActions(row,detail.querySelector('.actions'));
    }
    function render(nextModel,nextArgs) {
      model=nextModel;args=nextArgs;
      context.textContent=[args.context?.branch,args.context?.business_date].filter(Boolean).join(' · ');
      for(const key of ['gross','refund','net'])values[key].textContent=args.totals?.[key]!==undefined?`${BigInt(args.totals[key]).toLocaleString()}원`:'조회 중';
      if(!model.rows.has(selected))selected=model.rows.keys().next().value||null;
      renderList();renderDetail();
    }
    return {render,select(id){selected=id;search.value='';filter.value='all';},reset(scopeChanged=true){
      detailId=null;items.clear();list.replaceChildren();detail.replaceChildren();
      if(scopeChanged){selected=null;search.value='';filter.value='all';}
    }};
  }};
})();
