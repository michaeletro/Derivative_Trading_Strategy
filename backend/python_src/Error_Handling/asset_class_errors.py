# ------------------First Layer of Error Handling for Invalid Assets------------------

class AssetError(Exception):
    """
    Base class for all asset-related errors.

    Attributes:
    -----------
    message : str
        The error message associated with the exception.
    details : dict, optional
        Additional details about the error, if any.

    Methods:
    --------
    __str__():
        Returns a string representation of the error, including details if provided.
    """
    def __init__(self, message: str, details: dict = None):
        super().__init__(message)
        self.details = details

    def __str__(self):
        base_message = super().__str__()
        if self.details:
            return f"{base_message} | Details: {self.details}"
        return base_message

class InvalidAssetFormatError(AssetError):
    """
    Raised when an asset's format is invalid.

    Attributes:
    -----------
    asset_data : str
        The asset data that triggered the error.
    message : str
        The error message associated with the exception.
    details : dict, optional
        Additional details about the error, if any.
    """
    def __init__(self, asset_data: str, message: str = "Invalid asset format.", details: dict = None):
        super().__init__(message, details)
        self.asset_data = asset_data

class AssetTypeError(AssetError):
    """
    Raised when an unsupported asset type is encountered.

    Attributes:
    -----------
    asset_type : str
        The unsupported asset type that triggered the error.
    message : str
        The error message associated with the exception.
    details : dict, optional
        Additional details about the error, if any.
    sub_error : bool
        Indicates if this error is part of a larger set of errors.
    """
    def __init__(self, asset_type: str, message: str = "Unsupported asset type.",
                 details: dict = None, sub_error: bool = False):
        super().__init__(message, details)
        self.asset_type = asset_type
        self.sub_error = sub_error

# ------------------Second Layer of Error Handling for Invalid Conversions------------------

class AssetConversionError(AssetError):
    """
    Raised when an asset fails to convert to a specific subclass.

    Attributes:
    -----------
    asset_name : str
        The name of the asset being converted.
    target_class : str
        The target class for the conversion.
    message : str
        The error message associated with the exception.
    details : dict, optional
        Additional details about the error, if any.

    Methods:
    --------
    __str__():
        Returns a string representation of the error, including the asset name and target class.
    """
    def __init__(self, asset_name: str, target_class: str, message: str = "Asset conversion failed.", details: dict = None):
        super().__init__(message, details)
        self.asset_name = asset_name
        self.target_class = target_class

    def __str__(self):
        return f"Failed to convert from {self.asset_name} to {self.target_class}"

class StockConversionError(AssetConversionError):
    """
    Raised when there is an issue converting an asset class to a stock class.

    Attributes:
    -----------
    asset_name : str
        The name of the asset being converted.
    message : str
        The error message associated with the exception.
    details : dict, optional
        Additional details about the error, if any.
    """
    def __init__(self, asset_name: str, message: str = "Failed to convert to stock class.", details: dict = None):
        super().__init__(asset_name, "Stock_Class", message, details)
        pass

class CashConversionError(AssetConversionError):
    """
    Raised when there is an issue converting an asset class to a cash class.

    Attributes:
    -----------
    asset_name : str
        The name of the asset being converted.
    message : str
        The error message associated with the exception.
    details : dict, optional
        Additional details about the error, if any.
    """
    def __init__(self, asset_name: str, message: str = "Failed to convert to cash class.", details: dict = None):
        super().__init__(asset_name, "Cash_Class", message, details)

# ------------------Not Implemented Yet------------------

class AssetDataRetrievalError(AssetError):
    """
    Raised when there is an issue retrieving asset data.

    Attributes:
    -----------
    asset_name : str
        The name of the asset for which data retrieval failed.
    message : str
        The error message associated with the exception.
    details : dict, optional
        Additional details about the error, if any.
    """
    def __init__(self, asset_name: str, message: str = "Failed to retrieve asset data.", details: dict = None):
        super().__init__(message, details)
        self.asset_name = asset_name


class AssetOperationError(AssetError):
    """
    Raised when a general operation on an asset fails.

    Attributes:
    -----------
    operation : str
        The name of the operation that failed.
    message : str
        The error message associated with the exception.
    details : dict, optional
        Additional details about the error, if any.
    """
    def __init__(self, operation: str, message: str = "Asset operation failed.", details: dict = None):
        super().__init__(message, details)
        self.operation = operation


class AssetPlottingError(AssetError):
    """
    Raised when there is an issue generating a plot for the asset.

    Attributes:
    -----------
    plot_name : str
        The name of the plot that failed.
    message : str
        The error message associated with the exception.
    details : dict, optional
        Additional details about the error, if any.
    """
    def __init__(self, plot_name: str, message: str = "Failed to generate asset plot.", details: dict = None):
        super().__init__(message, details)
        self.plot_name = plot_name


class MissingAssetDataError(AssetError):
    """
    Raised when required data is missing for an asset.

    Attributes:
    -----------
    missing_key : str
        The key of the missing data.
    message : str
        The error message associated with the exception.
    details : dict, optional
        Additional details about the error, if any.
    """
    def __init__(self, missing_key: str, message: str = "Required asset data is missing.", details: dict = None):
        super().__init__(message, details)
        self.missing_key = missing_key
