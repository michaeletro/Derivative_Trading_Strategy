from Utilities import API_Connection
import logging

# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')


class Time_Series_Class(API_Connection):
    """
    A class to represent and manage time series data for a given financial asset.

    This class inherits from the API_Connection class and initializes time series data
    for a specified asset within a given date range and time multiplier.

    Attributes:
    -----------
    asset_name : str
        The name of the financial asset.
    start_date : str
        The start date for the time series data (default is "2023-01-09").
    end_date : str
        The end date for the time series data (default is today's date).
    time_multiplier : str
        The time multiplier for the time series data (default is "1").
    api_object : API_Connection
        An instance of the API_Connection class initialized with the provided parameters.

    Methods:
    --------
    __init__(self, asset_name, start_date="2023-01-09", end_date=datetime.today().strftime("%Y-%m-%d"), time_multiplier="1"):
        Initializes the Time_Series_Class with the specified parameters and creates an API_Connection instance.
    """
    def __init__(self, asset_name):
        super().__init__(asset_name=asset_name)
        if self.debug:
            logging.info("Generating a Time Series Class...")


