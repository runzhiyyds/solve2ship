"""Release payload/transport fixtures; never publish or use a real GitHub token."""

import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

spec=importlib.util.spec_from_file_location('publish_release',Path(__file__).resolve().parents[1]/'scripts/publish_release.py')
release=importlib.util.module_from_spec(spec);spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def test_matching_version_immutable_target_and_bilingual_notes_required(self):
        tag='v'+release.__version__;payload=release.release_payload(tag,'runzhiyyds/solve2ship','a'*40)
        self.assertTrue(payload['prerelease']);self.assertFalse(payload['draft']);self.assertIn('## 简体中文',payload['body'])
        for values in [('v999.0.0','runzhiyyds/solve2ship','a'*40),(tag,'owner/../bad','a'*40),(tag,'owner/repo','main')]:
            with self.subTest(values=values),self.assertRaises(ValueError):release.release_payload(*values)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'docs/releases').mkdir(parents=True);(root/'docs/releases'/f'{tag}.md').write_text('## English only')
            with patch.object(release,'ROOT',root),self.assertRaisesRegex(ValueError,'bilingual'):release.release_payload(tag,'owner/repo','a'*40)

    def test_existing_release_is_idempotent_without_republishing(self):
        class Transport:
            def __init__(self):self.methods=[]
            def open(self,request,timeout):
                self.methods.append(request.get_method());return io.BytesIO(json.dumps({'html_url':'https://github.com/owner/repo/releases/tag/v0.10.0'}).encode())
        transport=Transport();result=release.publish('owner/repo','fixture-token',{'tag_name':'v0.10.0'},transport)
        self.assertEqual(result['status'],'already_exists');self.assertEqual(transport.methods,['GET'])

    def test_failed_submission_has_no_retry_or_token_redirect(self):
        class Transport:
            def __init__(self):self.methods=[]
            def open(self,request,timeout):
                self.methods.append(request.get_method())
                raise HTTPError(request.full_url,404 if len(self.methods)==1 else 502,'fixture',None,None)
        transport=Transport()
        with self.assertRaises(HTTPError):release.publish('owner/repo','fixture-token',{'tag_name':'v0.10.0'},transport)
        self.assertEqual(transport.methods,['GET','POST'])
        self.assertIsNone(release.NoRedirect().redirect_request(None,None,None,None,None,None))
        with self.assertRaisesRegex(ValueError,'token'):release.publish('owner/repo','',{},transport)
