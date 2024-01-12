from datetime import datetime
from Portfolio import Portfolio
from Derivative_Trading_Strategy.Utilities.Utilities import apiKey
class Stock(Portfolio):

    def __init__(self, ticker, time_multiplier="1", timespan="day", start_date="2023-01-09",
                 end_date=datetime.today().strftime("%Y-%m-%d"), adjusted="True", sort="asc", limit=1200,
                 api_key=apiKey):
        self.ticker = ticker
        self.time_multiplier = time_multiplier
        self.timespan = timespan
        self.start_date = start_date
        self.end_date = end_date
        self.adjusted = adjusted
        self.sort = sort
        self.limit = limit
        self.api_key = api_key
        self.stock_info = self.generate_stock_info(ticker=self.ticker, time_multiplier=self.time_multiplier,
                                                   timespan=self.timespan, start_date=self.start_date,
                                                   end_date=self.end_date, adjusted=self.adjusted, sort=self.sort,
                                                   limit=self.limit, api_key=self.api_key)

    def generate_stock_info(self, ticker, time_multiplier="1", timespan="day", start_date="2023-01-09",
                            end_date=datetime.today().strftime("%Y-%m-%d"), adjusted="True", sort="asc",
                            limit=1200, api_key=apiKey):
        url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/{time_multiplier}/{timespan}/{start_date}/{end_date}?adjusted={adjusted}&sort={sort}&limit={limit}&apiKey={apiKey}"

        response = requests.request("GET", url, headers=headers, data=payload).json()

        # Adjust the MS timespan to proper time
        for i in range(0, len(response['results'])):
            response['results'][i]['t'] = pd.to_datetime(response['results'][i]['t'], unit='ms')
            response['results'][i]['Time'] = response['results'][i].pop('t')
            response['results'][i]['Volume_Weighted'] = response['results'][i].pop('vw')
            response['results'][i]['Open_Price'] = response['results'][i].pop('o')
            response['results'][i]['Lowest_Price'] = response['results'][i].pop('l')
            response['results'][i]['Highest_Price'] = response['results'][i].pop('h')
            response['results'][i]['Close_Price'] = response['results'][i].pop('c')
            response['results'][i]['Volume'] = response['results'][i].pop('v')
            response['results'][i]['Lot_Size'] = response['results'][i].pop('n')

        return response