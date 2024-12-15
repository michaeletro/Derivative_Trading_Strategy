from Financial_Products.Asset import Asset_Class

class Forex_Class(Asset_Class):
    """
    A class to represent and manage forex data for a given financial asset.

    This class inherits from the Asset_Class and initializes forex data
    for a specified currency swap position.

    Attributes:
    -----------
    asset_time_series : Asset_Class
        An instance of the Asset_Class initialized with the currency swap position.
    price_data_frame : DataFrame
        The price data frame of the asset.
    price_vector : DataFrame
        The price vector data frame of the asset.

    Methods:
    --------
    __init__(self, currency_swap_position):
        Initializes the Forex_Class with the specified currency swap position and creates an Asset_Class instance.
    """
    def __init__(self, currency_swap_position):
        print('Generating a Currency Class')

        self.asset_time_series = Asset_Class(currency_swap_position)
        try:
            self.price_data_frame = self.asset_time_series.price_data_frame
            self.price_vector = self.asset_time_series.price_vector
        except TypeError:
            self.price_vector = []
            self.price_vector = []