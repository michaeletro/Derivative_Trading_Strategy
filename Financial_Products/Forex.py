from requests import request
from datetime import datetime

from Derivative_Trading_Strategy.Utilities.Utilities_Resources import apiKey
from Derivative_Trading_Strategy.Utilities.Utilities_Resources import headers
from Derivative_Trading_Strategy.Utilities.Utilities_Resources import payload

from Derivative_Trading_Strategy.Financial_Products.Portfolio import Asset

class Forex(Asset):
    def __init__(self, currency_swap_position):
        self.Forex_Position = currency_swap_position