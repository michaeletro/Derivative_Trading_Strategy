from Derivative_Trading_Strategy.Financial_Products.Asset import Asset
class Stock(Asset):

    def __init__(self, ticker):

        self.asset_time_series = Asset(ticker)
        try:
            self.price_vector = self.asset_time_series.price_vector
        except TypeError as TE:
            self.price_vector = []
            print(f'Type Error of {TE}')

#a_1 = Stock('AAPL')
#a_2 = Stock('TSLY')