"""Local protocol tests must not transmit fixture bodies via system proxies."""

from urllib.parse import urlsplit
from urllib.request import Request,build_opener,ProxyHandler


def urlopen(request,timeout=3):
    address=request.full_url if isinstance(request,Request) else request
    if urlsplit(address).hostname not in ('127.0.0.1','localhost'):
        raise ValueError('loopback_test_transport_only')
    return build_opener(ProxyHandler({})).open(request,timeout=timeout)
