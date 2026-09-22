"""Polygon aggregate-bar client with runtime credentials and bounded requests."""
import logging
import math
import os
from datetime import datetime, timedelta
from typing import Dict, Optional
from urllib.parse import quote, urlencode

import requests

from Error_Handling.api_errors import (
    CustomAPIError, APIDelayedError, APIEmptyResponseError, APIConnectionError,
    APITimeoutError, APIDataParsingError, APIUnexpectedStatusCodeError,
)

logger = logging.getLogger(__name__)


class API_Connection:
    """Fetch one aggregate-bar response. Pagination/retries are not implemented.

    Existing positional arguments are preserved. Date defaults and credentials
    are resolved when a client is constructed, not when the module is imported.
    No request is made during construction. Public URL/repr/logs omit the key.
    """
    API_BASE_URL = "https://api.polygon.io/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{start}/{end}"

    def __init__(self, asset_name: str, time_multiplier: str = "1",
                 time_span: str = "day", start_date: Optional[str] = None,
                 end_date: Optional[str] = None, adjusted: bool = True,
                 sort: str = "asc", limit: Optional[int] = 5000,
                 api_key: Optional[str] = None, debug: bool = False, *,
                 timeout: float = 15.0):
        today = datetime.today()
        self.response = None
        self.asset_name = asset_name
        self.time_multiplier = str(time_multiplier)
        self.time_span = time_span
        self.start_date = start_date if start_date is not None else (today - timedelta(days=10)).strftime("%Y-%m-%d")
        self.end_date = end_date if end_date is not None else today.strftime("%Y-%m-%d")
        self.adjusted = adjusted
        self.sort = sort
        self.limit = limit
        self.api_key = os.environ.get("POLYGON_API_KEY", "") if api_key is None else api_key
        self.debug = debug
        self.timeout = timeout
        self.headers = {"Accept": "application/json"}
        self.payload = None
        self._validate_parameters()
        self.url = self._build_url()

    def _validate_parameters(self):
        if not isinstance(self.asset_name, str) or not self.asset_name.strip():
            raise ValueError("asset_name must be nonempty")
        if self.time_span not in {"minute", "hour", "day", "week", "month"}:
            raise ValueError("Invalid time_span")
        if not self.time_multiplier.isdecimal() or int(self.time_multiplier) <= 0:
            raise ValueError("time_multiplier must be a positive integer")
        if self.sort not in {"asc", "desc"} or not isinstance(self.adjusted, bool):
            raise ValueError("Invalid sort or adjusted value")
        if self.limit is not None and (type(self.limit) is not int or not 1 <= self.limit <= 50000):
            raise ValueError("limit must be an integer in [1, 50000] or None")
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise ValueError("Set POLYGON_API_KEY or supply api_key explicitly")
        if not isinstance(self.timeout, (int, float)) or isinstance(self.timeout, bool) or not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        start = datetime.strptime(self.start_date, "%Y-%m-%d")
        end = datetime.strptime(self.end_date, "%Y-%m-%d")
        if start > end:
            raise ValueError("start_date must not be after end_date")

    def _build_url(self) -> str:
        url = self.API_BASE_URL.format(
            ticker=quote(self.asset_name, safe=""), multiplier=self.time_multiplier,
            timespan=self.time_span, start=self.start_date, end=self.end_date)
        params = {"adjusted": str(self.adjusted).lower(), "sort": self.sort}
        if self.limit is not None:
            params["limit"] = self.limit
        return url + "?" + urlencode(params)

    def _validate_response(self):
        if not isinstance(self.response, dict):
            raise APIDataParsingError()
        status = self.response.get("status")
        results = self.response.get("results", [])
        count = self.response.get("resultsCount", len(results) if isinstance(results, list) else 0)
        if status == "ERROR":
            raise CustomAPIError("Market-data API returned an error")
        if status == "DELAYED" and count == 0:
            raise APIDelayedError()
        if status == "OK" and count == 0:
            raise APIEmptyResponseError()

    def generate_request(self) -> Dict:
        self._validate_parameters()
        self.response = None  # Never expose the previous successful response after a failure.
        self.url = self._build_url()
        if self.debug:
            logger.info("Requesting aggregate market data for %s", self.asset_name)
        headers = dict(self.headers, Authorization="Bearer " + self.api_key)
        try:
            response = requests.get(self.url, headers=headers,
                                    timeout=self.timeout, allow_redirects=False)
            # Do not forward credentials to redirects or treat redirects as success.
            if 300 <= response.status_code < 400:
                raise APIUnexpectedStatusCodeError(response.status_code)
            response.raise_for_status()
        except requests.Timeout:
            raise APITimeoutError() from None
        except requests.HTTPError as error:
            code = error.response.status_code if error.response is not None else 0
            raise APIUnexpectedStatusCodeError(code) from None
        except requests.RequestException:
            raise APIConnectionError() from None
        try:
            parsed = response.json()
        except ValueError:
            raise APIDataParsingError() from None
        self.response = parsed
        try:
            self._validate_response()
        except CustomAPIError:
            self.response = None
            raise
        return self.response

    def __repr__(self):
        return f"<APIConnection(asset={self.asset_name!r})>"
