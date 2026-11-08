"""Tag-triggered GitHub release metadata; standard library, no runtime binaries."""

import argparse
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError
from urllib.request import Request,HTTPRedirectHandler,build_opener

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from solve2ship import __version__


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def release_payload(tag,repository,commit):
    if tag!='v'+__version__ or not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+',tag):raise ValueError('tag_version_mismatch')
    if not re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+',repository) or '..' in repository:raise ValueError('invalid_repository')
    if not re.fullmatch(r'[0-9a-f]{40}',commit):raise ValueError('immutable_target_commit_required')
    body=(ROOT/'docs/releases'/f'{tag}.md').read_text()
    if '## English' not in body or '## 简体中文' not in body:raise ValueError('bilingual_release_notes_required')
    return {'tag_name':tag,'target_commitish':commit,'name':f'Solve2Ship {tag} — Engineering preview / 工程预览',
            'body':body,'draft':False,'prerelease':True,'make_latest':'false'}


def publish(repository,token,payload,opener=None):
    if not token:raise ValueError('job_token_required')
    opener=opener or build_opener(NoRedirect)
    url=f'https://api.github.com/repos/{repository}/releases'
    headers={'Accept':'application/vnd.github+json','Authorization':'Bearer '+token,
             'Content-Type':'application/json','X-GitHub-Api-Version':'2026-03-10'}
    try:
        with opener.open(Request(url+'/tags/'+payload['tag_name'],headers=headers),timeout=30) as response:existing=json.load(response)
        return {'status':'already_exists','url':existing['html_url']}
    except HTTPError as error:
        if error.code!=404:raise
    # One submission only. A network ambiguity fails the job; rerun first checks the tag.
    with opener.open(Request(url,data=json.dumps(payload).encode(),headers=headers,method='POST'),timeout=30) as response:
        result=json.load(response)
    return {'status':'published','url':result['html_url']}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--dry-run',action='store_true');args=parser.parse_args()
    payload=release_payload(os.environ.get('GITHUB_REF_NAME',''),os.environ.get('GITHUB_REPOSITORY',''),os.environ.get('GITHUB_SHA',''))
    if args.dry_run:print(json.dumps(payload,ensure_ascii=False));return
    if os.environ.get('GITHUB_ACTIONS')!='true':raise ValueError('github_actions_job_required')
    print(json.dumps(publish(os.environ['GITHUB_REPOSITORY'],os.environ.get('GITHUB_TOKEN',''),payload)))


if __name__=='__main__':
    try:main()
    except HTTPError as error:raise SystemExit(f'GitHub release failed: HTTP {error.code}') from None
