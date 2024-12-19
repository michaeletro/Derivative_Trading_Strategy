#---------------First Layer of Portfolio Errors---------------
class PortfolioError(Exception):
    """Base class for all portfolio-related errors."""
    pass

class InvalidAssetTypeError(PortfolioError):
    """Raised when an unsupported asset type is provided."""
    def __init__(self, asset_type, message="Invalid asset type provided."):
        self.asset_type = asset_type
        super().__init__(f"{message} Received: {asset_type}")

class DuplicateAssetError(PortfolioError):
    """Raised when attempting to add a duplicate asset to the portfolio."""
    def __init__(self, asset, message="Duplicate asset detected."):
        self.asset = asset
        super().__init__(f"{message} Asset: {asset}")

class AssetNotFoundError(PortfolioError):
    """Raised when an asset is not found in the portfolio."""
    def __init__(self, asset_name, message="Asset not found in the portfolio."):
        self.asset_name = asset_name
        super().__init__(f"{message} Asset: {asset_name}")

#---------------First Layer of Portfolio Errors---------------

class OperationError(PortfolioError):
    """Base class for errors related to operations on the portfolio."""
    pass

class EmptyPortfolioError(OperationError):
    """Raised when an operation is attempted on an empty portfolio."""
    def __init__(self, message="The portfolio is empty. Cannot perform the operation."):
        super().__init__(message)

class UnsupportedOperationError(OperationError):
    """Raised when an unsupported operation is attempted on the portfolio."""
    def __init__(self, operation, message="Unsupported operation."):
        self.operation = operation
        super().__init__(f"{message} Operation: {operation}")

