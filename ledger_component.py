"""Small Streamlit v1 component; checkpoints stay in authenticated server session memory."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path


def component_scope(username, branch, business_date):
    return sha256(f"{username}\0{branch}\0{business_date}".encode()).hexdigest()[:24]


def merge_checkpoint(previous, incoming, known_scopes):
    """Checkpoint raw UI state, never client scope authority or a durable saved status."""
    previous = previous or {'serial': -1, 'models': {}}
    try:
        if (not isinstance(incoming, dict) or type(incoming.get('serial')) is not int
                or incoming['serial'] < previous['serial'] or not isinstance(incoming.get('models'), dict)
                or len(json.dumps(incoming, ensure_ascii=False, allow_nan=False).encode()) > 4_000_000):
            return previous
        models = deepcopy(previous['models'])
        for scope, snapshot in incoming['models'].items():
            if (scope in known_scopes and isinstance(snapshot, dict) and snapshot.get('scope') == scope
                    and isinstance(snapshot.get('rows'), list) and len(snapshot['rows']) <= 1000
                    and all(isinstance(row, dict) and isinstance(row.get('id'), str)
                            and isinstance(row.get('raw'), dict) for row in snapshot['rows'])
                    and (snapshot.get('active') is None or isinstance(snapshot['active'], dict))):
                models[scope] = deepcopy(snapshot)
        return {'serial': incoming['serial'], 'models': models}
    except (TypeError, ValueError, RecursionError):
        return previous


def checkpoint_pending(checkpoint):
    return any(snapshot.get('active') or any(row.get('dirty') or row.get('failed') or row.get('conflict')
               or row.get('status') in ('saving', 'error', 'conflict') for row in snapshot.get('rows', []))
               for snapshot in (checkpoint or {}).get('models', {}).values())


def ledger_editor(*, identity, scope, rows, channels, ack=None, closed=False, refund_options=None):
    import streamlit as st
    import streamlit.components.v1 as components
    widget_key = f'ledger_editor_{identity}'
    checkpoint_key, scopes_key = f'_ledger_client_{identity}', f'_ledger_scopes_{identity}'

    def remember_input():
        # Widget callbacks run before rerouting, so leaving the ledger cannot erase queued text.
        value = st.session_state.get(widget_key)
        if isinstance(value, dict) and isinstance(value.get('checkpoint'), dict):
            st.session_state[f'_ledger_checkpoint_event_{identity}'] = True
            st.session_state[checkpoint_key] = merge_checkpoint(st.session_state.get(checkpoint_key),
                                                               value['checkpoint'], st.session_state.get(scopes_key, {}))

    component = components.declare_component('clinic_receipt_ledger', path=str(Path(__file__).parent / 'assets' / 'ledger_editor'))
    return component(identity=identity, scope=scope, rows=rows, channels=list(channels), ack=ack,
                     checkpoint=st.session_state.get(checkpoint_key), closed=closed,
                     refund_options=refund_options or [], key=widget_key, default=None, on_change=remember_input)
