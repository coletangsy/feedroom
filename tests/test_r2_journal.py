import io
import unittest
from unittest.mock import patch

try:
    import boto3
    from botocore.response import StreamingBody
    from botocore.stub import ANY, Stubber
except ImportError:
    boto3 = None

from feedroom.journal import JournalError, R2Journal


@unittest.skipIf(boto3 is None, 'install feedroom[r2] to exercise the R2 adapter')
class R2JournalTests(unittest.TestCase):
    def setUp(self):
        client = boto3.client('s3', region_name='auto', endpoint_url='https://example.com',
                              aws_access_key_id='fixture', aws_secret_access_key='fixture')
        self.stubber = self.enterContext(Stubber(client))
        self.journal = R2Journal(client, 'fixture-bucket', '11')
        self.target = {'Bucket': 'fixture-bucket', 'Key': 'threads/11.json'}
        self.body = {**self.target, 'ContentType': 'application/json', 'Body': ANY}

    def test_initial_and_subsequent_writes_are_conditional(self):
        self.stubber.add_client_error('get_object', 'NoSuchKey', http_status_code=404, expected_params=self.target)
        self.stubber.add_response('put_object', {}, expected_params={**self.body, 'IfNoneMatch': '*'})
        self.stubber.add_response('get_object', {'Body': StreamingBody(io.BytesIO(b'{}'), 2), 'ETag': '"revision"'},
                                  expected_params=self.target)
        self.stubber.add_client_error('put_object', 'PreconditionFailed', http_status_code=412,
                                      expected_params={**self.body, 'IfMatch': '"revision"'})

        self.assertEqual(self.journal.read(), ({}, None))
        self.assertTrue(self.journal.write({}, None))
        self.assertEqual(self.journal.read(), ({}, '"revision"'))
        self.assertFalse(self.journal.write({}, '"revision"'))
        self.stubber.assert_no_pending_responses()

    def test_throttling_retries_the_same_condition_and_access_denied_fails_closed(self):
        expected = {**self.body, 'IfMatch': '"revision"'}
        self.stubber.add_client_error('put_object', 'SlowDown', http_status_code=429, expected_params=expected)
        self.stubber.add_response('put_object', {}, expected_params=expected)
        self.stubber.add_client_error('get_object', 'AccessDenied', http_status_code=403, expected_params=self.target)

        with patch('feedroom.journal.sleep') as wait:
            self.assertTrue(self.journal.write({}, '"revision"'))
        wait.assert_called_once_with(1)
        with self.assertRaises(JournalError):
            self.journal.read()
        self.stubber.assert_no_pending_responses()
