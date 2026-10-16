/* One immutable command at a time. Server ACK is the only saved indicator. */
(function (root) {
  const clone = value => JSON.parse(JSON.stringify(value));
  const receiptRaw = value => ({chart_no:value.chart_no || '',patient_name:value.patient_name || '',
    note:value.note || '',payments:value.payments || {},yeoshin_used:!!value.yeoshin_used,
    gangnamunni_used:!!value.gangnamunni_used,kind:value.kind || 'receipt',refund_of:value.refund_of || null});
  class EditorState {
    constructor(scope, uuid = () => crypto.randomUUID()) {
      this.scope=scope; this.uuid=uuid; this.rows=new Map(); this.active=null; this.composing=new Set();
    }
    sync(rows, focused = new Set()) {
      const ids = new Set(rows.map(row=>row.id));
      for (const incoming of rows) {
        const local=this.rows.get(incoming.id);
        if (!local) this.rows.set(incoming.id,{...clone(incoming),dirty:false,status:incoming.draft_revision?'draft':'saved'});
        else if (!local.dirty && !local.conflict && !local.failed && this.active?.id!==local.id && !focused.has(local.id)) {
          Object.assign(local,clone(incoming));
          local.status=incoming.draft_revision?'draft':'saved';
        }
      }
      for (const [id,row] of this.rows) {
        if (!ids.has(id) && !row.local && !row.dirty && !row.conflict && !row.failed && this.active?.id!==id && !focused.has(id)) this.rows.delete(id);
      }
    }
    add(raw, id=this.uuid()) {
      this.rows.set(id,{id,raw:clone(raw),receipt_revision:0,draft_revision:0,dirty:false,status:'new',local:true});
      return id;
    }
    edit(id,field,value,payment=false) {
      const row=this.rows.get(id);
      if (payment) row.raw.payments[field]=value; else row.raw[field]=value;
      row.dirty=true; row.status=row.conflict?'conflict':'editing';
    }
    next() {
      if (this.active) return clone(this.active);
      const retry=[...this.rows.values()].find(row=>row.retryRequested && row.failed);
      if (retry) {
        this.active={...retry.failed,attempt:(retry.failed.attempt||0)+1};
        retry.failed=null; retry.retryRequested=false; retry.status='saving';
        return clone(this.active);
      }
      const row=[...this.rows.values()].find(row=>row.dirty && !row.conflict && !row.failed && !this.composing.has(row.id));
      if (!row) return null;
      this.active={scope:this.scope,request_id:this.uuid(),id:row.id,action:row.action||'save',raw:clone(row.raw),
        receipt_revision:row.receipt_revision,draft_revision:row.draft_revision};
      row.dirty=false; row.status='saving'; row.action=null;
      return clone(this.active);
    }
    ack(ack) {
      if (!this.active || ack.request_id!==this.active.request_id || (ack.attempt||0)!==(this.active.attempt||0)) return;
      const row=this.rows.get(this.active.id);
      if (!row) return;
      if (ack.status==='error') {
        row.failed=clone(this.active); row.status='error'; row.message=ack.message; this.active=null; return;
      }
      this.active=null;
      if (ack.receipt_revision!==undefined) row.receipt_revision=ack.receipt_revision;
      if (ack.draft_revision!==undefined) row.draft_revision=ack.draft_revision;
      if (ack.voided!==undefined) {row.voided=ack.voided;if(ack.voided)row.dirty=false;}
      row.message=ack.message || '';
      if (ack.status==='conflict') {row.conflict=clone(ack.current||{}); row.status='conflict'; return;}
      if (ack.status==='discarded') {this.rows.delete(row.id); return;}
      row.local=false; row.status=row.dirty?'editing':ack.status;
    }
    resolve(id,choice) {
      const row=this.rows.get(id), current=row.conflict;
      if (!current) return;
      if (current.values) {
        row.draft_revision=current.revision;
        if (choice==='current') {
          row.raw=clone(current.values);
          if(Number.isInteger(current.values._receipt_revision))row.receipt_revision=current.values._receipt_revision;
        }
      } else {
        row.receipt_revision=current.revision || 0;
        if (choice==='current') row.raw=receiptRaw(current);
      }
      row.conflict=null; row.message='';
      // Selecting the current value still saves it, clearing the user's stale draft atomically.
      row.dirty=true; row.status='editing';
    }
    retry(id) {
      const row=this.rows.get(id);
      if (!row.failed) return;
      row.retryRequested=true;
    }
    discard(id) {
      const row=this.rows.get(id);
      if (!row.receipt_revision && !row.draft_revision && !row.dirty) {this.rows.delete(id); return;}
      row.action='discard'; row.conflict=null; row.failed=null; row.dirty=true;
    }
  }
  class EditorRegistry {
    constructor(uuid,checkpoint=null) {
      this.uuid=uuid;this.models=new Map();this.serial=checkpoint?.serial||0;
      for(const [scope,snapshot] of Object.entries(checkpoint?.models||{})) {
        const model=new EditorState(scope,uuid);
        model.rows=new Map((snapshot.rows||[]).map(row=>[row.id,clone(row)]));
        model.active=snapshot.active?clone(snapshot.active):null;
        this.models.set(scope,model);
      }
    }
    select(scope) {
      if(this.activeScope&&this.activeScope!==scope)this.models.get(this.activeScope)?.composing.clear();
      this.activeScope=scope;
      if(!this.models.has(scope))this.models.set(scope,new EditorState(scope,this.uuid));
      return this.models.get(scope);
    }
    snapshot() {
      return {serial:++this.serial,models:Object.fromEntries([...this.models].map(([scope,model])=>[
        scope,{scope,rows:clone([...model.rows.values()].filter(row=>row.local||row.dirty||row.failed||row.conflict||model.active?.id===row.id)),active:model.active?clone(model.active):null}]))};
    }
    pending() {
      return [...this.models.values()].some(model=>model.active||[...model.rows.values()].some(row=>
        row.dirty||row.failed||row.conflict||row.status==='saving'));
    }
  }
  const api={EditorState,EditorRegistry,receiptRaw};
  if (typeof module!=='undefined') module.exports=api;
  root.LedgerEditor=api;
})(typeof window!=='undefined'?window:globalThis);
