from requests import request
from datetime import datetime
import pandas as pd
from Derivative_Trading_Strategy.Utilities.API_Connection import API_Connection
import yfinance as yf

class Time_Series(API_Connection):

    def __init__(self, ticker, start_date="2023-01-09", end_date=datetime.today().strftime("%Y-%m-%d"), time_multiplier="1"):
        print('Generating an Time Class')
        self.ticker = ticker
        self.start_date = start_date
        self.end_date = end_date
        self.time_multiplier = time_multiplier

        self.api_object = API_Connection(asset=ticker, time_multiplier=self.time_multiplier, start_date=self.start_date,
                                         end_date=self.end_date)

    def __iter__(self):
        self.current_index = 0
        return self



