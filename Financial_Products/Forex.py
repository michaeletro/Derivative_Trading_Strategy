from Asset import Asset_Class

class Forex_Class(Asset_Class):
    def __init__(self, currency_swap_position):
        print('Generating a Currency Class')

        self.asset_time_series = Asset_Class(currency_swap_position)
        try:
            self.price_data_frame = self.asset_time_series.price_data_frame
            self.price_vector = self.asset_time_series.price_vector
        except TypeError:
            self.price_vector = []
            self.price_vector = []