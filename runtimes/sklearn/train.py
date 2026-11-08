"""Container-only trusted scikit-learn training. Never imported by control plane."""

def solve(payload):
    import base64
    import gzip
    import pickle
    import warnings
    import sklearn
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.neural_network import MLPClassifier

    parameters=payload['params']
    if payload['algorithm']=='logistic_regression':
        estimator=LogisticRegression(C=parameters.get('C',1.0),max_iter=200,random_state=0)
    elif payload['algorithm']=='random_forest':
        estimator=RandomForestClassifier(n_estimators=parameters.get('n_estimators',12),max_depth=parameters.get('max_depth',4),random_state=0,n_jobs=1)
    elif payload['algorithm']=='mlp':
        estimator=MLPClassifier(hidden_layer_sizes=tuple(parameters.get('hidden_layers',[8])),alpha=parameters.get('alpha',.001),solver='lbfgs',max_iter=200,random_state=0)
    else:
        raise ValueError('unsupported_algorithm')
    model=make_pipeline(StandardScaler(),estimator)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        model.fit(payload['features'],payload['labels'])
    serialized=base64.b64encode(gzip.compress(pickle.dumps(model,protocol=5),mtime=0)).decode('ascii')
    return {'model':serialized,'sklearn_version':sklearn.__version__,'training_count':len(payload['features']),
            'feature_count':len(payload['features'][0]),'random_state':0,
            'warnings':[{'category':type(w.message).__name__,'message':str(w.message)[:500]} for w in caught]}
