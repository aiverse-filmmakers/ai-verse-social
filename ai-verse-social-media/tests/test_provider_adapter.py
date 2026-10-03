import io
import json
import unittest
from unittest.mock import patch

from social_video_ops.caption_integrity import encode_payload
from social_video_ops.providers import ProviderError, Zernio
from social_video_ops.util import UserError


class FakeResponse:
    def __init__(self, body, status=200):
        self.body=body
        self.status=status
    def __enter__(self): return self
    def __exit__(self,*args): return False
    def read(self): return self.body


class FakeOpener:
    def __init__(self,response):
        self.response=response
        self.request=None
        self.timeout=None
    def open(self,request,timeout):
        self.request=request; self.timeout=timeout
        return self.response


class ZernioAdapterTests(unittest.TestCase):
    def provider(self,response):
        client=Zernio.__new__(Zernio)
        client.timeout=37
        client.key="synthetic-secret"
        client.opener=FakeOpener(response)
        return client

    def test_create_sends_exact_utf8_caption_and_idempotency_key(self):
        response={"post":{"_id":"synthetic-post","content":"Bonjour\u00a0!\nשלום"}}
        client=self.provider(FakeResponse(json.dumps(response,ensure_ascii=False).encode()))
        payload={"content":"Bonjour\u00a0!\nשלום","publishNow":True,"platforms":[{"platform":"instagram","accountId":"acct"}]}
        result=client.create(payload,"stable-operation-id")
        request=client.opener.request
        self.assertEqual(result["post"]["_id"],"synthetic-post")
        self.assertEqual(request.get_method(),"POST")
        self.assertEqual(request.full_url,"https://zernio.com/api/v1/posts")
        self.assertEqual(request.get_header("Idempotency-key"),"stable-operation-id")
        self.assertEqual(request.get_header("Authorization"),"Bearer synthetic-secret")
        self.assertEqual(json.loads(request.data.decode("utf-8")),payload)
        self.assertEqual(request.data,encode_payload(payload))
        self.assertEqual(client.opener.timeout,37)

    def test_provider_server_error_is_classified_uncertain_without_leaking_body(self):
        import urllib.error
        headers={"Retry-After":"2"}
        error=urllib.error.HTTPError("https://zernio.com/api/v1/posts",503,"unavailable",headers,io.BytesIO(b'{"secret":"do not expose"}'))
        client=self.provider(None)
        client.opener.open=lambda *args,**kwargs: (_ for _ in ()).throw(error)
        with self.assertRaises(ProviderError) as caught:
            client.create({"content":"caption","platforms":[]},"idempotency")
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(caught.exception.status,503)
        self.assertEqual(caught.exception.retry_after,"2")
        self.assertNotIn("do not expose",str(caught.exception))

    def test_bulk_account_health_is_profile_scoped_and_requires_complete_rows(self):
        client=self.provider(FakeResponse(json.dumps({"summary":{"total":1},"accounts":[
            {"accountId":"acct-1","platform":"instagram","status":"healthy","canPost":True,"issues":[]}]}).encode()))
        rows=client.health_all(["profile-1"])
        self.assertEqual(rows[0]["accountId"],"acct-1")
        self.assertEqual(client.opener.request.get_method(),"GET")
        self.assertIn("/accounts/health?",client.opener.request.full_url)
        self.assertIn("profileId=profile-1",client.opener.request.full_url)
        client=self.provider(FakeResponse(b'{"summary":{"total":1},"accounts":[{}]}'))
        with self.assertRaisesRegex(UserError,"incomplete"):
            client.health_all(["profile-1"])

    def test_nonobject_json_response_is_safely_classified(self):
        for body in (b'[]',b'null',b'42'):
            client=self.provider(FakeResponse(body))
            with self.assertRaises(ProviderError) as caught: client.profiles()
            self.assertTrue(caught.exception.uncertain)

    def test_nonobject_http_error_body_keeps_safe_classification(self):
        import urllib.error
        error=urllib.error.HTTPError('https://zernio.com/api/v1/posts',400,'bad',{},io.BytesIO(b'[]'))
        client=self.provider(None)
        client.opener.open=lambda *args,**kwargs: (_ for _ in ()).throw(error)
        with self.assertRaises(ProviderError) as caught: client.create({'content':'caption'},'key')
        self.assertEqual(caught.exception.status,400); self.assertFalse(caught.exception.uncertain)


if __name__=="__main__":
    unittest.main()
