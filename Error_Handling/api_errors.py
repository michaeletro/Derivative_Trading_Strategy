#-------------------------Custom API Errors Currently Handled-------------------------

class CustomAPIError(Exception):
    """
    Base class for all API-related errors.

    Attributes:
    -----------
    message : str
        A description of the error.
    response : dict, optional
        The API response that caused the error, if available.

    Methods:
    --------
    __str__():
        Returns a string representation of the error, including the response details if available.
    """
    def __init__(self, message: str, response: dict = None):
        super().__init__(message)
        self.response = response

    def __str__(self):
        base_message = super().__str__()
        if self.response:
            return f"{base_message} | Response: {self.response}"
        return base_message

class APIDelayedError(CustomAPIError):
    """
    Raised when the API response is delayed.

    Attributes:
    -----------
    response : dict, optional
        The API response that caused the error, if available.
    """
    def __init__(self, response=None):
        super().__init__("API response is delayed.", response)

class APIEmptyResponseError(CustomAPIError):
    """
    Raised when the API response is empty but successful.

    Attributes:
    -----------
    response : dict, optional
        The API response that caused the error, if available.
    """
    def __init__(self, response=None):
        super().__init__("API response is empty despite a successful status.", response)

class APIInvalidRequestError(CustomAPIError):
    """
    Raised when the API request is invalid or malformed.

    Attributes:
    -----------
    response : dict, optional
        The API response that caused the error, if available.
    """
    def __init__(self, response=None):
        super().__init__("Invalid API request. Please check the request parameters.", response)

#-------------------------Custom API Errors Not Implemented-------------------------

class APIConnectionError(CustomAPIError):
    """
    Raised when there is a connection issue with the API.

    Attributes:
    -----------
    response : dict, optional
        The API response that caused the error, if available.
    """
    def __init__(self, response=None):
        super().__init__("Failed to connect to the API server.", response)

class APIAuthenticationError(CustomAPIError):
    """
    Raised when authentication with the API fails.

    Attributes:
    -----------
    response : dict, optional
        The API response that caused the error, if available.
    """
    def __init__(self, response=None):
        super().__init__("Authentication failed. Please check your API key.", response)

class APIRateLimitError(CustomAPIError):
    """
    Raised when the API rate limit is exceeded.

    Attributes:
    -----------
    response : dict, optional
        The API response that caused the error, if available.
    """
    def __init__(self, response=None):
        super().__init__("API rate limit exceeded. Please retry after some time.", response)

class APITimeoutError(CustomAPIError):
    """
    Raised when the API request times out.

    Attributes:
    -----------
    response : dict, optional
        The API response that caused the error, if available.
    """
    def __init__(self, response=None):
        super().__init__("API request timed out.", response)

class APIDataParsingError(CustomAPIError):
    """
    Raised when the API response data cannot be parsed.

    Attributes:
    -----------
    response : dict, optional
        The API response that caused the error, if available.
    """
    def __init__(self, response=None):
        super().__init__("Failed to parse API response data.", response)

class APIUnexpectedStatusCodeError(CustomAPIError):
    """
    Raised when the API returns an unexpected status code.

    Attributes:
    -----------
    status_code : int
        The unexpected status code returned by the API.
    response : dict, optional
        The API response that caused the error, if available.
    """
    def __init__(self, status_code: int, response=None):
        super().__init__(f"Unexpected API status code: {status_code}.", response)
        self.status_code = status_code

class APIMissingDataError(CustomAPIError):
    """
    Raised when the API response is missing expected data.

    Attributes:
    -----------
    missing_key : str
        The key of the missing data in the API response.
    response : dict, optional
        The API response that caused the error, if available.
    """
    def __init__(self, missing_key: str, response=None):
        super().__init__(f"API response is missing required data: {missing_key}.", response)
        self.missing_key = missing_key
