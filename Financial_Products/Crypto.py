from Asset import Asset_Class

class Crypto_Class(Asset_Class):
    def __int__(self, asset):
        print('Generating a Crypto Class')

        self.asset_time_series = Asset(asset)
        try:
            self.price_data_frame = self.asset_time_series.price_data_frame
            self.price_vector = self.asset_time_series.price_vector
        except TypeError:
            self.price_vector = []
            self.price_vector = []