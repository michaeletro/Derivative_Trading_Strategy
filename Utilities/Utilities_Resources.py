import datetime
import plotly

import pandas as pd
import yfinance as yf
import plotly.express as px

from datetime import date
from datetime import timedelta
from datetime import datetime
from requests import request

apiKey = "oIpqVdgJYK9nldqQ5j8JiPXxVVZptz0a"
payload = {}
headers = {
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

