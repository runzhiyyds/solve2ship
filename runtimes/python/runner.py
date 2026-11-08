"""CONTAINER ONLY. Never import or execute this module in the STS control plane."""

import json
import os
import resource
import sys

# Hard limits complement Engine cgroups and the external control-plane deadline.
resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
resource.setrlimit(resource.RLIMIT_FSIZE, (1048576, 1048576))
payload = json.loads(os.environ.pop('STS_TASK'))
if os.environ.pop('STS_IMAGE_PACKAGES','0')=='1':
    import sysconfig
    sys.path.append(sysconfig.get_paths()['purelib'])
namespace = {'__name__': 'candidate'}
exec(compile(payload['source'], '<candidate>', 'exec'), namespace)
result = namespace['solve'](payload['input'])
encoded = json.dumps({'result': result}, ensure_ascii=False, allow_nan=False)
if len(encoded.encode('utf-8')) > 65536:
    raise ValueError('output_limit')
sys.stdout.write(encoded)
