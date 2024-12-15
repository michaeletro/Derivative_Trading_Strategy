from Utilities.Utilities_Resources import *
from requests import request
from datetime import datetime


class API_Connection:
    """
    A class to manage API connections and requests for financial asset data.

    Attributes:
    -----------
    apiKey : str
        The API key for authentication.
    payload : dict
        The payload for the API request.
    headers : dict
        The headers for the API request.
    asset : str
        The financial asset ticker symbol.
    time_multiplier : str
        The time multiplier for the data aggregation.
    timespan : str
        The time span for the data aggregation.
    start_date : str
        The start date for the data request.
    end_date : str
        The end date for the data request.
    adjusted : str
        Whether the data is adjusted.
    sort : str
        The sort order of the data.
    limit : int
        The limit on the number of data points.
    other_arguments : list
        Additional arguments for the API request.
    attributes : dict
        A dictionary of all attributes for the API request.
    url : str
        The URL for the API request.
    response : dict
        The response from the API request.

    Methods:
    --------
    __init__(self, asset, time_multiplier="1", time_span="day", start_date="2023-01-09", end_date=datetime.today().strftime("%Y-%m-%d"), adjusted="True", sort="asc", limit=1200, *args):
        Initializes the API_Connection class with the specified parameters and generates the request URL.

    __setattr__(self, value_replacing, value_to_be_replaced):
        Sets the attribute value for the specified attribute.

    generate_request(self):
        Sends the API request and returns the response.
    """
    def __init__(self,
                 asset,
                 time_multiplier="1",
                 time_span="day",
                 start_date="2023-01-09",
                 end_date=datetime.today().strftime("%Y-%m-%d"),
                 adjusted="True",
                 sort="asc",
                 limit=1200,
                 *args):
        print('Generating an API Connection Class')

        # Initialize the API connection with the specified parameters
        self.apiKey          = apiKey
        self.payload         = payload
        self.headers         = headers
        self.asset           = asset
        self.time_multiplier = time_multiplier
        self.timespan        = time_span
        self.start_date      = start_date
        self.end_date        = end_date
        self.adjusted        = adjusted
        self.sort            = sort
        self.limit           = limit
        self.other_arguments = list(*args)
        self.attributes      = {key: value for key, value in self.__dict__.items() if not key.startswith('__')}

        self.url = (f"https://api.polygon.io/v2/aggs/ticker/{self.asset}/range/"
                    f"{self.time_multiplier}/{self.timespan}/{self.start_date}/"
                    f"{self.end_date}?adjusted={self.adjusted}&sort={self.sort}"
                    f"&limit={self.limit}&apiKey={self.apiKey}")
        self.response = self.generate_request()

    def __setattr__(self, value_replacing, value_to_be_replaced):
        super().__setattr__(value_replacing, value_to_be_replaced)

    def generate_request(self):
        self.response = request(method="GET",
                                url=self.url,
                                headers=self.headers,
                                data=self.payload).json()

        if self.response['status'] == 'ERROR':
            return list(["ERROR", self.response['error']])
        else:
            return self.response
