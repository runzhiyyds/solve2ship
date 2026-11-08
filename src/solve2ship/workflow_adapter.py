"""Bounded declarative compositions of reviewed adapters, never host code."""

import json
import re
import threading
import time

from .candidates import ArtifactError,RulesAdapter,PythonAdapter,PromptAdapter
from .contracts import canonical_json,sha256,validate_json
from .invocation import CallTraceGateway
from .gateway import ProviderError,BudgetError


STEP_ADAPTERS={'rules.v1':RulesAdapter,'python.v1':PythonAdapter,'prompt.v1':PromptAdapter}


def requires_model(artifact):
    return any(step['family']=='prompt.v1' for step in artifact['steps'])


def validate_binding(binding,available,depth=0):
    if type(binding) is not dict or depth>4:
        raise ArtifactError('invalid_workflow_binding')
    if binding.keys()=={'object'}:
        value=binding['object']
        if type(value) is not dict or not 1<=len(value)<=16 or any(type(k) is not str or len(k)>100 for k in value):
            raise ArtifactError('invalid_workflow_binding')
        for child in value.values():validate_binding(child,available,depth+1)
    elif binding.keys()=={'source','path'}:
        if (type(binding['source']) is not str or binding['source'] not in available or type(binding['path']) is not list
                or len(binding['path'])>5 or any(not (type(p) is str and len(p)<=100 or type(p) is int and 0<=p<=500) for p in binding['path'])):
            raise ArtifactError('invalid_workflow_reference')
    else:raise ArtifactError('invalid_workflow_binding')


def bind(binding,values):
    if 'object' in binding:return {k:bind(v,values) for k,v in binding['object'].items()}
    value=values[binding['source']]
    for key in binding['path']:
        if type(value) is dict and type(key) is str and key in value:value=value[key]
        elif type(value) is list and type(key) is int and 0<=key<len(value):value=value[key]
        else:raise ArtifactError('workflow_input_path_missing')
    return value


def bounded_value(value):
    # References can repeat large prior values: cap encoded expansion before
    # hashing/validation, without constructing one unbounded serialized string.
    size=0
    for chunk in json.JSONEncoder(ensure_ascii=False,allow_nan=False).iterencode(value):
        size+=len(chunk.encode('utf-8'))
        if size>65536:raise ArtifactError('workflow_value_too_large')
    validate_json(value)
    return value


class WorkflowAdapter:
    family='workflow.v1'
    description=('Optional bounded DAG, autonomously compose 2..4 steps using rules.v1, python.v1 or prompt.v1. '
        'Exact artifact={"steps":[{"id":"normalize","family":"python.v1","artifact":{"source":"def solve(value): ..."},'
        '"input":{"source":"input","path":[]}},...],"output":{"source":"last_step_id","path":[]}}. '
        'A binding is {"source":"input"|prior_step_id,"path":[string object keys or integer array indices]} '
        'or {"object":{"field":binding,...}}. Steps run in listed topological order. Unique IDs, no forward/cyclic refs. '
        'Prompt steps use exactly the registered prompt.v1 artifact {"system_prompt":string} and the metered model gateway. '
        'Each Python solve runs only in a separate constrained Docker container without credentials or network; '
        'do not embed a workflow or unfitted model in a step. Prefer a simpler family when composition adds no value.')

    def __init__(self,sandbox,step_guard=None):
        if sandbox is None:raise ArtifactError('verified_sandbox_required')
        self.adapters={'rules.v1':RulesAdapter(),'python.v1':PythonAdapter(sandbox),'prompt.v1':PromptAdapter()}
        self.step_guard=step_guard or (lambda:True)
        self.local=threading.local()

    @staticmethod
    def validate(artifact):
        validate_json(artifact)
        if (type(artifact) is not dict or artifact.keys()!={'steps','output'} or type(artifact['steps']) is not list
                or not 2<=len(artifact['steps'])<=4 or len(canonical_json(artifact).encode())>60000):
            raise ArtifactError('invalid_workflow_artifact')
        available={'input'}
        for step in artifact['steps']:
            if (type(step) is not dict or step.keys()!={'id','family','artifact','input'} or type(step['id']) is not str
                    or not re.fullmatch('[a-z][a-z0-9_]{0,31}',step['id']) or step['id'] in available
                    or type(step['family']) is not str or step['family'] not in STEP_ADAPTERS):
                raise ArtifactError('invalid_workflow_step')
            validate_binding(step['input'],available)
            STEP_ADAPTERS[step['family']].validate(step['artifact'])
            available.add(step['id'])
        validate_binding(artifact['output'],available-{'input'})

    def runtime_receipt(self):return getattr(self.local,'receipt',None)

    def execute(self,artifact,sample,gateway):
        self.local.receipt={'provider':'bounded_workflow','policy':'bounded-dag-v2','steps':[],
                            'status':'running','model_calls':0,'model_call_ids':[],'max_steps':4,'attempt_unit':'whole_workflow_invocation'}
        try:
            self.validate(artifact)
            if requires_model(artifact) and gateway is None:raise ArtifactError('model_gateway_required')
        except ValueError:
            self.local.receipt['status']='failed';raise
        values={'input':sample}
        for step in artifact['steps']:
            if not self.step_guard():
                self.local.receipt['status']='cancelled';raise ArtifactError('workflow_cancelled')
            adapter=self.adapters[step['family']]
            if hasattr(adapter,'sandbox'):adapter.sandbox.clear_receipt()
            started=time.perf_counter();record={'id':step['id'],'family':step['family']}
            calls=CallTraceGateway(gateway)
            self.local.receipt['steps'].append(record)
            try:
                value=bounded_value(bind(step['input'],values));record['input_sha256']=sha256(value)
                output,call_id=adapter.execute(step['artifact'],value,calls)
                calls.remember(call_id)
                bounded_value(output);encoded=canonical_json(output).encode()
                record.update({'output_sha256':sha256(output),'output_bytes':len(encoded),'output_omitted':len(encoded)>8192})
                if len(encoded)<=8192:record['output']=output
                values[step['id']]=output
                record['status']='succeeded'
            except Exception as error:
                self.local.receipt['status']='failed'
                code=error.code if isinstance(error,ProviderError) else 'budget_exhausted' if isinstance(error,BudgetError) else 'workflow_step_failed'
                record.update({'status':'failed','error':code})
                raise
            finally:
                record['latency_ms']=(time.perf_counter()-started)*1000
                record['model_call_ids']=calls.call_ids
                self.local.receipt['model_call_ids'].extend(calls.call_ids)
                self.local.receipt['model_calls']=len(self.local.receipt['model_call_ids'])
                if hasattr(adapter,'runtime_receipt'):record['runtime']=adapter.runtime_receipt()
        try:output=bounded_value(bind(artifact['output'],values))
        except ValueError:
            self.local.receipt['status']='failed';raise
        self.local.receipt['status']='succeeded'
        ids=self.local.receipt['model_call_ids']
        return output,ids[-1] if ids else None
