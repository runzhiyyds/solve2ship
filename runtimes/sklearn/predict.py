"""Container-only fitted model load. Never unpickle model artifacts on the host."""

def solve(payload):
    import base64
    import gzip
    import pickle
    model=pickle.loads(gzip.decompress(base64.b64decode(payload['model'],validate=True)))
    result=model.predict([payload['features']])[0]
    return result.item() if hasattr(result,'item') else result
