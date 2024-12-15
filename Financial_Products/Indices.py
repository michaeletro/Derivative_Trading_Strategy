from Asset import Asset_Class

class Index_Class(Asset_Class):
    def __int__(self, asset_name):
        print('Generating an Index Class')

        self.asset_time_series = Asset_Class(asset_name)
        try:
            self.price_data_frame = self.asset_time_series.price_data_frame
            self.price_vector = self.asset_time_series.price_vector
        except TypeError:
            self.price_vector = []
            self.price_vector = []
