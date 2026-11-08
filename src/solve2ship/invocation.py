"""Additive per-invocation model attribution; no credentials or retry behavior."""

from .gateway import ProviderError


class CallTraceGateway:
    def __init__(self,gateway):
        self.gateway=gateway
        self.call_ids=[]

    def remember(self,call_id):
        if call_id and call_id not in self.call_ids:self.call_ids.append(call_id)

    def json(self,*args,**kwargs):
        try:
            value,call_id=self.gateway.json(*args,**kwargs)
        except ProviderError as error:
            self.remember(error.call_id)
            raise
        self.remember(call_id)
        return value,call_id


def invocation_runtime(adapter,call_ids):
    runtime=adapter.runtime_receipt() if hasattr(adapter,'runtime_receipt') else None
    if not call_ids:return runtime
    return {**(runtime or {'provider':'model_gateway'}),'model_call_ids':list(call_ids),'model_calls':len(call_ids)}


def invocation_call_ids(row):
    runtime=row.get('runtime') or {}
    return set(runtime.get('model_call_ids',[]))|({row['call_id']} if row.get('call_id') else set())
