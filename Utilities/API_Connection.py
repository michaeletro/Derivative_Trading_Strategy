import logging
from datetime import datetime, timedelta
from typing import Dict, Optional
from urllib.error import HTTPError

import requests

# Custom error handling classes
from Error_Handling.api_errors import CustomAPIError, APIDelayedError, APIEmptyResponseError, APIInvalidRequestError

# API key for authentication
from Utilities.Utilities_Resources import apiKey

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

class API_Connection:
    """
    A class to manage API connections and requests for financial asset data.

    Attributes:
    -----------
    asset : str
        The financial asset ticker symbol.
    time_multiplier : str
        The time multiplier for data aggregation.
    time_span : str
        The time span for data aggregation.
    start_date : str
        The start date for the data request.
    end_date : str
        The end date for the data request.
    adjusted : bool
        Whether the data is adjusted.
    sort : str
        The sort order of the data ("asc" or "desc").
    limit : Optional[int]
        The limit on the number of data points.
    debug : bool
        Whether to enable debug mode.

    Methods:
    --------
    generate_request() -> Dict:
        Sends the API request and returns the response.

    build_url() -> str:
        Constructs the API request URL.

    validate_parameters():
        Validates input parameters to ensure they meet API requirements.
    """

    API_BASE_URL = "https://api.polygon.io/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{start}/{end}"

    def __init__(
        self,
        asset_name: str,
        time_multiplier: str = "1",
        time_span: str = "day",
        start_date: str = (datetime.today()-timedelta(days=10)).strftime("%Y-%m-%d"),
        end_date: str = datetime.today().strftime("%Y-%m-%d"),
        adjusted: bool = True,
        sort: str = "asc",
        limit: Optional[int] = 5000,
        api_key: str = apiKey,
        debug: bool = False,
    ):
        """
        Initializes the APIConnection class with the specified parameters.

        Parameters:
        -----------
        asset : str
            The financial asset ticker symbol.
        time_multiplier : str
            The time multiplier for data aggregation.
        time_span : str
            The time span for data aggregation (e.g., "minute", "day").
        start_date : str
            The start date for the data request.
        end_date : str
            The end date for the data request.
        adjusted : bool
            Whether the data is adjusted.
        sort : str
            The sort order of the data ("asc" or "desc").
        limit : Optional[int]
            The limit on the number of data points.
        api_key : str
            API key for authentication.
        debug : bool
            Whether to enable debug mode.
        """
        self.response = None
        self.asset_name = asset_name
        self.time_multiplier = time_multiplier
        self.time_span = time_span
        self.start_date = start_date
        self.end_date = end_date
        self.adjusted = adjusted
        self.sort = sort
        self.limit = limit
        self.api_key = api_key
        self.debug = debug
        self.headers = {"Accept": "application/json"}
        self.payload = None

        self.url = self._build_url()

        if self.debug:
            logging.info("Generating a API Connection Class...")
            print(self.url)

    def _validate_response(self):
        """Validates the API response output."""
        status = self.response.get('status')
        results_count = self.response.get('resultsCount', 0)

        if status == 'ERROR':
            if self.debug:
                logging.info(self.response)
            logging.warning("API Error encountered. Retrying...")
            raise CustomAPIError("API Error encountered.", self.response)

        if status == 'DELAYED' and results_count == 0:
            if self.debug:
                logging.info(self.response)
            logging.warning("Response delayed. Retrying...")
            raise APIDelayedError(self.response)

        if status == 'OK' and results_count == 0:
            if self.debug:
                logging.info(self.response)
            logging.error("Unexpected empty response. Retrying...")
            raise APIEmptyResponseError(self.response)

        if self.debug:
            logging.info("Response validated successfully.")


    def _validate_parameters(self):
        """Validates input parameters to ensure they meet API requirements."""
        valid_time_spans = {"minute", "hour", "day", "week", "month"}
        valid_sort_orders = {"asc", "desc"}

        if self.time_span not in valid_time_spans:
            raise ValueError(f"Invalid time_span: {self.time_span}. Must be one of {valid_time_spans}.")
        if self.sort not in valid_sort_orders:
            raise ValueError(f"Invalid sort order: {self.sort}. Must be one of {valid_sort_orders}.")
        if not self.api_key:
            raise ValueError("API key is required.")

        if self.debug:
            logging.info("Parameters validated successfully.")

    def _build_url(self) -> str:
        """Constructs the API request URL."""
        url = self.API_BASE_URL.format(
            ticker=self.asset_name,
            multiplier=self.time_multiplier,
            timespan=self.time_span,
            start=self.start_date,
            end=self.end_date,
        )
        url += f"?adjusted={str(self.adjusted).lower()}&sort={self.sort}"
        if self.limit:
            url += f"&limit={self.limit}"
        if self.debug:
            logging.debug(f"Constructed URL: {url}")
        url += f"&apiKey={self.api_key}"
        return url

    def generate_request(self) -> Dict:
        """
        Sends the API request and returns the response.

        Returns:
        --------
        Dict:
            The API response as a dictionary.

        Raises:
        -------
        requests.RequestException:
            If the request fails due to a network or server error.
        """
        try:
            self._validate_parameters()

            response = requests.get(self.url, headers=self.headers)
            self.response = response.json()

            self._validate_response()
            return self.response

        except HTTPError as e:
            logging.error(f"API request failed: {e}")
            #raise APIInvalidRequestError(e)

    def __repr__(self):
        """Returns a string representation of the APIConnection instance."""
        return f"<APIConnection(asset={self.asset_name}, url={self.url})>"
