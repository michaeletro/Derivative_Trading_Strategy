from .Asset import Asset_Class

class Index_Class(Asset_Class):
    """
    A class to represent and manage index data for a given financial asset.

    This class inherits from the Asset_Class and initializes index data
    for a specified asset name.

    Attributes:
    -----------
    asset_Asset : Asset_Class
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
    def __init__(self, asset):
        print('Generating an Index Class')
