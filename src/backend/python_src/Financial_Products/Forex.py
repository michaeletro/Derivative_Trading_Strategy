from .Asset import Asset_Class

class Forex_Class(Asset_Class):
    """
    A class to represent and manage forex data for a given financial asset.

    This class inherits from the Asset_Class and initializes forex data
    for a specified currency swap position.

    Attributes:
    -----------
    asset_Asset : Asset_Class
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
