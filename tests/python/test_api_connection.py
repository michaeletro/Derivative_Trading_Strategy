import importlib
import os
from pathlib import Path
import sys
import traceback
import unittest
from unittest.mock import Mock, patch

import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src/backend/Arch/python_src"))
module = importlib.import_module("Utilities.API_Connection")
API_Connection = module.API_Connection


class APIConnectionTests(unittest.TestCase):
    def client(self, **kwargs):
        return API_Connection("DEMO", api_key="test-not-a-real-key", **kwargs)

    def test_missing_credential(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                API_Connection("DEMO")

    def test_credential_read_at_construction(self):
        with patch.dict(os.environ, {"POLYGON_API_KEY": "test-one"}):
            one = API_Connection("DEMO")
        with patch.dict(os.environ, {"POLYGON_API_KEY": "test-two"}):
            two = API_Connection("DEMO")
        self.assertEqual(one.api_key, "test-one")
        self.assertEqual(two.api_key, "test-two")

    def test_credential_absent_from_url_and_repr(self):
        client = self.client(debug=True)
        self.assertNotIn(client.api_key, client.url)
        self.assertNotIn(client.api_key, repr(client))
        self.assertNotIn("apiKey", client.url)

    def test_no_io_at_construction(self):
        with patch("requests.get") as get:
            self.client()
            get.assert_not_called()

    def test_bad_parameters(self):
        for kwargs in ({"limit": 0}, {"limit": True}, {"time_multiplier": "0"},
                       {"timeout": float("nan")}, {"timeout": 0}, {"sort": "bad"},
                       {"start_date": "2026-02-30"},
                       {"start_date": "2026-03-01", "end_date": "2026-02-01"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.client(**kwargs)

    def test_symbol_is_path_encoded(self):
        client = API_Connection("C:EUR/USD", api_key="test-key")
        self.assertIn("C%3AEUR%2FUSD", client.url)

    def test_get_has_bounded_timeout_header_and_no_redirect(self):
        response = Mock(status_code=200)
        response.json.return_value = {"status": "OK", "resultsCount": 1, "results": [{"c": 100}]}
        client = self.client(timeout=7)
        with patch("requests.get", return_value=response) as get:
            self.assertEqual(client.generate_request()["resultsCount"], 1)
        kwargs = get.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 7)
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-not-a-real-key")
        response.raise_for_status.assert_called_once()

    def test_network_error_does_not_leak_credentials(self):
        client = self.client()
        with patch("requests.get", side_effect=requests.ConnectionError(client.api_key)):
            try:
                client.generate_request()
            except module.APIConnectionError:
                self.assertNotIn(client.api_key, traceback.format_exc())
            else:
                self.fail("expected sanitized network error")

    def test_timeout(self):
        with patch("requests.get", side_effect=requests.Timeout("secret")):
            with self.assertRaises(module.APITimeoutError):
                self.client().generate_request()

    def test_http_failure(self):
        response = Mock(status_code=401)
        response.raise_for_status.side_effect = requests.HTTPError("private details", response=response)
        with patch("requests.get", return_value=response):
            with self.assertRaises(module.APIUnexpectedStatusCodeError) as error:
                self.client().generate_request()
        self.assertEqual(error.exception.status_code, 401)
        self.assertNotIn("private", str(error.exception))

    def test_redirect_rejected(self):
        with patch("requests.get", return_value=Mock(status_code=302)):
            with self.assertRaises(module.APIUnexpectedStatusCodeError):
                self.client().generate_request()

    def test_non_json_and_non_object_rejected(self):
        response = Mock(status_code=200)
        response.json.side_effect = ValueError("private response")
        with patch("requests.get", return_value=response), self.assertRaises(module.APIDataParsingError):
            self.client().generate_request()
        response.json.side_effect = None
        response.json.return_value = [1, 2]
        client = self.client()
        with patch("requests.get", return_value=response), self.assertRaises(module.APIDataParsingError):
            client.generate_request()
        self.assertIsNone(client.response)

    def test_empty_and_delayed_responses(self):
        for status, exception in (("OK", module.APIEmptyResponseError), ("DELAYED", module.APIDelayedError)):
            response = Mock(status_code=200)
            response.json.return_value = {"status": status, "resultsCount": 0}
            with patch("requests.get", return_value=response), self.assertRaises(exception):
                self.client().generate_request()

    def test_failure_clears_old_result(self):
        client = self.client()
        client.response = {"status": "OK", "resultsCount": 1}
        with patch("requests.get", side_effect=requests.Timeout()), self.assertRaises(module.APITimeoutError):
            client.generate_request()
        self.assertIsNone(client.response)


if __name__ == "__main__":
    unittest.main()
