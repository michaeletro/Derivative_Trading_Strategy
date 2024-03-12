from Derivative_Trading_Strategy.Utilities.Utilities_Resources import *

class API_Connection:

    def __init__(self, asset, time_multiplier="1", time_span="day", start_date="2023-01-09",
                 end_date=datetime.today().strftime("%Y-%m-%d"), adjusted="True", sort="asc", limit=1200, *args):
        print('Generating an API Connection Class')

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

        self.attributes      = {"apikey"          : self.apiKey,     "payload"         : self.apiKey,          "headers"    : self.headers,
                                "asset"           : self.asset,      "time_multiplier" : self.time_multiplier, "timespan"   : self.timespan,
                                "start_date"      : self.start_date, "end_date"        : self.end_date,        "adjusted"   : self.adjusted,
                                "sort"            : self.sort,       "limit"           : self.limit,
                                "other_arguments" : self.other_arguments}


        if len(self.asset) > 5:
            self.asset = f'O:{asset}'

        self.url = f"https://api.polygon.io/v2/aggs/ticker/{self.asset}/range/{self.time_multiplier}/{self.timespan}/{self.start_date}/{self.end_date}?adjusted={self.adjusted}&sort={self.sort}&limit={self.limit}&apiKey={self.apiKey}"
        self.response = self.generate_request()

    def __setattr__(self, value_replacing, value_to_be_replaced):
        super().__setattr__(value_replacing, value_to_be_replaced)

    def generate_request(self):

        self.response = request("GET", self.url, headers=self.headers, data=self.payload).json()

        if self.response['status'] == 'ERROR':
            return list(["ERROR", self.response['error']])
        else:
            return self.response