from Derivative_Trading_Strategy.Financial_Products.Asset import Asset
class Crypto(Asset):
    def __int__(self, asset):
        self.asset_time_series = Asset(asset)
        try:
            self.price_data_frame = self.asset_time_series.price_data_frame
            self.price_vector = self.asset_time_series.price_vector
        except TypeError:
            self.price_vector = []
            self.price_vector = []