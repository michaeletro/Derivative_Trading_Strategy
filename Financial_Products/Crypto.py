from Financial_Products.Asset import Asset_Class

class Crypto_Class(Asset_Class):
    """
    This module defines the `Crypto_Class` for managing cryptocurrency data in the financial products package.

    Classes:
    --------
    Crypto_Class(Asset_Class):
        A class to represent and manage cryptocurrency data for a given financial asset.

        Methods:
        --------
        __init__(self, asset):
            Initializes the Crypto_Class with the specified asset and creates an Asset_Class instance.
    """
    def __int__(self, asset):
        print('Generating a Crypto Class')

        self.asset_time_series = Asset_Class(asset)
        try:
            self.price_data_frame = self.asset_time_series.price_data_frame
            self.price_vector = self.asset_time_series.price_vector
        except TypeError:
            self.price_vector = []
            self.price_vector = []