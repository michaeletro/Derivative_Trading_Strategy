from Utilities import API_Connection
from datetime import datetime


class Time_Series(API_Connection):

    def __init__(self, asset_name, start_date="2023-01-09", end_date=datetime.today().strftime("%Y-%m-%d"), time_multiplier="1"):
        print('Generating an Time Class')
        self.asset_name = asset_name
        self.start_date = start_date
        self.end_date = end_date
        self.time_multiplier = time_multiplier
        self.api_object = API_Connection(asset=asset_name, time_multiplier=self.time_multiplier, start_date=self.start_date,
                                         end_date=self.end_date)


