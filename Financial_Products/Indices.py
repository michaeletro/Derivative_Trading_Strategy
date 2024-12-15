from Financial_Products.Asset import Asset_Class

class Index_Class(Asset_Class):
    """
    A class to represent and manage index data for a given financial asset.

    This class inherits from the Asset_Class and initializes index data
    for a specified asset name.

    Attributes:
    -----------
    asset_time_series : Asset_Class
        An instance of the Asset_Class initialized with the asset name.
    price_data_frame : DataFrame
        The price data frame of the asset.
    price_vector : DataFrame
        The price vector data frame of the asset.

    Methods:
    --------
    __init__(self, asset_name):
        Initializes the Index_Class with the specified asset name and creates an Asset_Class instance.
    """
    def __int__(self, asset_name):
        print('Generating an Index Class')

        self.asset_time_series = Asset_Class(asset_name)
        try:
            self.price_data_frame = self.asset_time_series.price_data_frame
            self.price_vector = self.asset_time_series.price_vector
        except TypeError:
            self.price_vector = []
            self.price_vector = []
