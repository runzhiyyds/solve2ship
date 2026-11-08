"""Real local ML-image isolation/import/fit/predict checks; fixtures are diagnostics."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from solve2ship.docker_runtime import DockerSandbox,ADAPTER_SHA256
from solve2ship.sklearn_adapter import SklearnAdapter
from verify_docker import verify as verify_isolation


def verify():
    inventory=json.loads((ROOT/'dependencies.json').read_text())
    image=next(row['reference'] for row in inventory['container_images'] if row.get('runtime_id')=='sklearn')
    isolation,_=verify_isolation(image)
    sandbox=DockerSandbox(image,timeout=10);adapter=SklearnAdapter(sandbox)
    versions=sandbox.execute('''def solve(x):
 import importlib.metadata as m,hashlib
 names=x['names']
 return {'versions':{n:m.version(n) for n in names},'license_files':[{'package':n,'path':str(p),'sha256':hashlib.sha256(m.distribution(n).locate_file(p).read_bytes()).hexdigest()} for n in names for p in m.distribution(n).files if '.dist-info/' in str(p) and ('license' in str(p).lower() or 'copying' in str(p).lower())]}
''',{'names':[row['name'] for row in inventory['container_python_packages']]})
    if versions['versions']!={row['name']:row['version'] for row in inventory['container_python_packages']}:
        raise AssertionError('installed_packages_differ_from_review')
    rows=[{'sample_index':i,'input':{'features':[i-6,(i%3)-1]},'expected_output':'positive' if i>5 else 'negative'} for i in range(12)]
    fits=[]
    for algorithm in ('logistic_regression','random_forest','mlp'):
        fitted,evidence=adapter.build({'feature_path':['features'],'algorithm':algorithm,'params':{},'model':None},rows)
        build_receipt=sandbox.receipt()
        output,_=adapter.execute(fitted,{'features':[5,0]},None)
        if output!='positive':raise AssertionError('diagnostic_prediction_failed')
        fits.append({'algorithm':algorithm,'actual_fit':evidence,'prediction':output,'prediction_runtime':sandbox.receipt(),'diagnostic_only_not_optimization_result':True})
    return {'status':'passed','image':image,'engine_version':isolation['engine']['engine_version'],
            'isolation':isolation,'packages':versions,'diagnostic_fits':fits,'model_calls':0}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--activate',action='store_true');args=parser.parse_args()
    result=verify();name='ml-runtime-acceptance.json' if args.activate else 'ml-runtime-latest-check.json'
    reports=ROOT/'.sts/runtime-acceptance';reports.mkdir(parents=True,exist_ok=True,mode=0o700)
    (reports/name).write_text(json.dumps(result,indent=2)+'\n');os.chmod(reports/name,0o600)
    if args.activate:
        config={'acceptance_passed':True,'image':result['image'],'engine_version':result['engine_version'],'adapter_sha256':ADAPTER_SHA256,
                'acceptance_sha256':hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest(),
                'source_hashes':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in ['runtimes/python/runner.py','runtimes/sklearn/train.py','runtimes/sklearn/predict.py','src/solve2ship/sklearn_adapter.py','runtimes/sklearn/requirements.lock']}}
        path=ROOT/'.sts/ml-runtime.json';fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as stream:json.dump(config,stream)
        os.chmod(path,0o600)
    print(json.dumps({'status':result['status'],'activated':args.activate,'versions':result['packages']['versions'],'algorithms':[row['algorithm'] for row in result['diagnostic_fits']],'diagnostic_only':True}))
