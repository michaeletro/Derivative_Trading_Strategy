from datetime import date
from datetime import timedelta
from datetime import datetime
class API_Connection:

    def __init__(self, asset, time_multiplier="1", time_span="day", start_date="2023-01-09",
                 end_date=datetime.today().strftime("%Y-%m-%d"), adjusted="True", sort="asc", limit=1200, *args):

        self.apiKey          = "oIpqVdgJYK9nldqQ5j8JiPXxVVZptz0a"
        self.payload         = {}
        self.headers         = {
            'Accept': 'text/json',
            'Sec-Fetch-Site': 'same-site',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Sec-Fetch-Mode': 'cors',
            'Host': 'api.polygon.io',
            'Origin': 'https://polygon.io',
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2.1 Safari/605.1.15',
            'Referer': 'https://polygon.io/',
            'Connection': 'keep-alive',
            'Sec-Fetch-Dest': 'empty',
            'X-Polygon-Edge-User-Agent': 'useragent',
            'X-Polygon-Edge-IP-Address': '8.8.8.8',
            'X-Polygon-Edge-ID': 'sample_edge_id'
        }
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
                                "sort"            : self.sort,       "limit"           : self.limit,           "attributes" : self.attributes,
                                "other_arguments" : self.other_arguments}



        self.url = f"https://api.polygon.io/v2/aggs/ticker/{self.ticker}/range/{self.time_multiplier}/{self.timespan}/{self.start_date}/{self.end_date}?adjusted={self.adjusted}&sort={self.sort}&limit={self.limit}&apiKey={self.apiKey}"
        self.response = requests.request("GET", self.url, headers = self.headers, data=payload).json()