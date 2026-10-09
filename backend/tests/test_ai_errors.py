import io
import json
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch,MagicMock
from urllib.error import HTTPError,URLError

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from analysis.ai import complete,AIError,MODEL


class AIErrorsTests(unittest.TestCase):
    def invoke(self,envelope=None,error=None):
        opener=MagicMock()
        if error:opener.open.side_effect=error
        else:opener.open.return_value.__enter__.return_value.read.return_value=json.dumps(envelope).encode()
        with patch('analysis.ai.build_opener',return_value=opener):
            result=complete('private-test-key',{'candidates':[]})
        sent=json.loads(opener.open.call_args.args[0].data)
        self.assertEqual(sent['thinking'],{'type':'disabled'})
        self.assertEqual(sent['max_tokens'],4096)
        return result

    def test_http_errors_never_echo_private_body(self):
        for status in (400,401,402,403,404,422,429,500,503):
            error=HTTPError('https://api.deepseek.com',status,'private-test-key',{},io.BytesIO(b'private bill details'))
            with self.assertRaises(AIError) as got:self.invoke(error=error)
            self.assertEqual(got.exception.code,'http_'+str(status))
            self.assertNotIn('private',str(got.exception))

    def test_network_errors(self):
        for error,code in [(TimeoutError('secret'),'timeout'),(URLError(socket.gaierror('secret')),'dns'),(URLError('secret'),'connection')]:
            with self.assertRaises(AIError) as got:self.invoke(error=error)
            self.assertEqual(got.exception.code,code)
            self.assertNotIn('secret',str(got.exception))

    def test_response_errors_distinguished(self):
        for envelope,code in [({'model':'private-model'},'model_mismatch'),({'model':MODEL,'choices':[]},'response_structure'),({'model':MODEL,'choices':[{'finish_reason':'length'}]},'output_limit'),({'model':MODEL,'choices':[{'finish_reason':'stop','message':{'content':'private malformed reply'}}]},'invalid_json')]:
            with self.assertRaises(AIError) as got:self.invoke(envelope=envelope)
            self.assertEqual(got.exception.code,code)
            self.assertNotIn('private',str(got.exception))

    def test_success(self):
        result,_=self.invoke(envelope={'model':MODEL,'choices':[{'finish_reason':'stop','message':{'content':'{"decisions":[]}'}}]})
        self.assertEqual(result,{'decisions':[]})
