import unittest
from unittest.mock import Mock
from nhl_analytics.power import keep_awake


class PowerTests(unittest.TestCase):
    def kernel(self):
        kernel=Mock()
        kernel.PowerCreateRequest.return_value=123
        kernel.PowerSetRequest.return_value=1
        return kernel

    def test_requests_release_even_when_server_raises(self):
        kernel=self.kernel()
        with self.assertRaises(RuntimeError):
            with keep_awake(kernel) as active:
                self.assertTrue(active)
                self.assertEqual([c.args[1] for c in kernel.PowerSetRequest.call_args_list],[1,3])
                raise RuntimeError('server stopped')
        self.assertEqual([c.args[1] for c in kernel.PowerClearRequest.call_args_list],[3,1])
        kernel.CloseHandle.assert_called_once_with(123)

    def test_partial_failure_releases_request_and_keeps_api_available(self):
        kernel=self.kernel();kernel.PowerSetRequest.side_effect=[1,0]
        with keep_awake(kernel) as active:
            self.assertFalse(active)
        kernel.PowerClearRequest.assert_called_once_with(123,1)
        kernel.CloseHandle.assert_called_once_with(123)

    def test_invalid_handle_does_not_issue_requests(self):
        kernel=self.kernel();kernel.PowerCreateRequest.return_value=None
        with keep_awake(kernel) as active:
            self.assertFalse(active)
        kernel.PowerSetRequest.assert_not_called()
        kernel.CloseHandle.assert_not_called()
