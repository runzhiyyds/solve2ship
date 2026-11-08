"""Mature-library numeric classifiers; fitting/loading happen only in Docker."""

import hashlib
import json
import math
import os
from pathlib import Path
import stat

from .candidates import ArtifactError
from .contracts import canonical_json,sha256
from .docker_runtime import DockerError,DockerSandbox,ROOT,ADAPTER_SHA256


def features_at(sample,path):
    value=sample
    for key in path:
        value=value.get(key) if type(value) is dict else None
    if (type(value) is not list or not 1<=len(value)<=16 or any(type(x) not in (int,float) or not math.isfinite(x) or abs(x)>1e12 for x in value)):
        raise ArtifactError('numeric_features_required')
    return value


def eligible_numeric_task(contract):
    from .evaluation import partition_indices
    definition=contract['definition'];labels={x['sample_index']:x['expected_output'] for x in definition['labels']}
    examples=[definition['samples'][i] for i in partition_indices(contract)['train'] if i in labels and type(labels[i]) in (str,int)]
    values=[labels[i] for i in partition_indices(contract)['train'] if i in labels and type(labels[i]) in (str,int)]
    if len(examples)<4 or len({canonical_json(value) for value in values})<2 or len({type(value) for value in values})!=1:return False
    paths=[]
    def visit(value,path):
        if len(path)>5:return
        if type(value) is list:
            try:features_at(value,[]);paths.append(path)
            except ArtifactError:pass
        elif type(value) is dict:
            for key,child in value.items():visit(child,path+[key])
    visit(examples[0],[])
    for path in paths:
        try:
            rows=[features_at(example,path) for example in examples]
            if len({len(row) for row in rows})==1:return True
        except ArtifactError:pass
    return False


class SklearnAdapter:
    family='sklearn.classifier.v1'
    description='Train a real scikit-learn numeric classifier on supplied TRAIN labels only. artifact={"feature_path":[object field names, or [] for a whole numeric input array],"algorithm":"logistic_regression"|"random_forest"|"mlp","params":{},"model":null}. Numeric vectors length 1..16, scalar string/integer class labels. Optional params: logistic_regression C=0.001..100; random_forest n_estimators=1..16,max_depth=1..5; mlp hidden_layers=[1..16] (1 or 2 layers),alpha=0.000001..1. Never invent model bytes: model MUST be null in a proposal, actual fitting creates it. No labels or case IDs may be used as inference features.'

    def __init__(self,sandbox):self.sandbox=sandbox

    @staticmethod
    def validate(artifact):
        if type(artifact) is not dict or artifact.keys()!={'feature_path','algorithm','params','model'}:
            raise ArtifactError('invalid_sklearn_artifact')
        path=artifact['feature_path'];algorithm=artifact['algorithm'];parameters=artifact['params'];model=artifact['model']
        allowed={'logistic_regression':{'C'},'random_forest':{'n_estimators','max_depth'},'mlp':{'hidden_layers','alpha'}}
        if (type(path) is not list or len(path)>5 or any(type(x) is not str or not 1<=len(x)<=100 for x in path)
                or type(algorithm) is not str or algorithm not in allowed or type(parameters) is not dict or parameters.keys()-allowed[algorithm]
                or model is not None and (type(model) is not str or not 1<=len(model)<=60000)):
            raise ArtifactError('invalid_sklearn_artifact')
        for key,value in parameters.items():
            if key=='hidden_layers':
                valid=type(value) is list and 1<=len(value)<=2 and all(type(x) is int and 1<=x<=16 for x in value)
            elif key in ('n_estimators','max_depth'):
                valid=type(value) is int and 1<=value<=(16 if key=='n_estimators' else 5)
            else:
                valid=type(value) in (int,float) and math.isfinite(value) and (0.001 if key=='C' else .000001)<=value<=(100 if key=='C' else 1)
            if not valid:raise ArtifactError('invalid_sklearn_params')

    def build(self,artifact,training_examples):
        self.sandbox.clear_receipt()
        self.validate(artifact)
        conflicts={}
        for row in training_examples:
            if 'expected_output' in row:conflicts.setdefault(sha256(row['input']),set()).add(canonical_json(row['expected_output']))
        rows=[row for row in training_examples if 'expected_output' in row and len(conflicts[sha256(row['input'])])==1
              and type(row['expected_output']) in (str,int)]
        if len(rows)<4 or len({canonical_json(row['expected_output']) for row in rows})<2:
            raise ArtifactError('classification_training_labels_required')
        features=[features_at(row['input'],artifact['feature_path']) for row in rows]
        if len({len(row) for row in features})!=1:raise ArtifactError('consistent_feature_dimensions_required')
        labels=[row['expected_output'] for row in rows]
        if len({type(value) for value in labels})!=1:raise ArtifactError('consistent_class_label_types_required')
        payload={'features':features,'labels':labels,'algorithm':artifact['algorithm'],'params':artifact['params']}
        try:
            result=self.sandbox.execute((self.sandbox.runtime_root/'runtimes/sklearn/train.py').read_text(),payload)
        except DockerError as error:raise ArtifactError(str(error)) from error
        if (type(result) is not dict or result.get('sklearn_version')!='1.9.1' or result.get('training_count')!=len(rows)
                or type(result.get('model')) is not str or len(result['model'])>60000):
            raise ArtifactError('invalid_trained_model')
        fitted={**artifact,'model':result['model']}
        self.validate(fitted)
        evidence={key:value for key,value in result.items() if key!='model'}
        evidence.update({'training_sample_indices':[row['sample_index'] for row in rows],
            'training_data_sha256':sha256(payload),'model_sha256':sha256(result['model']),
            'runtime':self.sandbox.receipt(),'label_source':'provided_unverified','final_test_used':False})
        return fitted,evidence

    @staticmethod
    def editable_artifact(artifact):return {**artifact,'model':None}

    def execute(self,artifact,sample,gateway):
        self.sandbox.clear_receipt()
        self.validate(artifact)
        if artifact['model'] is None:raise ArtifactError('trained_model_required')
        payload={'model':artifact['model'],'features':features_at(sample,artifact['feature_path'])}
        try:
            return self.sandbox.execute((self.sandbox.runtime_root/'runtimes/sklearn/predict.py').read_text(),payload),None
        except DockerError as error:raise ArtifactError(str(error)) from error

    def runtime_receipt(self):return self.sandbox.receipt()


def configured_ml_sandbox(path):
    path=Path(path)
    if not path.exists():return None
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode & 0o077:
        raise DockerError('ml_profile_permissions')
    config=json.loads(path.read_text())
    evidence=json.loads((path.parent/'runtime-acceptance/ml-runtime-acceptance.json').read_text())
    expected={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in ['runtimes/python/runner.py','runtimes/sklearn/train.py','runtimes/sklearn/predict.py','src/solve2ship/sklearn_adapter.py','runtimes/sklearn/requirements.lock']}
    if (not config.get('acceptance_passed') or config.get('adapter_sha256')!=ADAPTER_SHA256 or config.get('source_hashes')!=expected
            or config.get('acceptance_sha256')!=hashlib.sha256(json.dumps(evidence,sort_keys=True).encode()).hexdigest()):
        raise DockerError('ml_acceptance_requires_review')
    sandbox=DockerSandbox(config['image'],timeout=10)
    sandbox.accepted_engine_version=config['engine_version']
    return sandbox
