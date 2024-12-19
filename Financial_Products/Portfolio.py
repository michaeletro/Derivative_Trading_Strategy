import plotly.express as px
from typing import List, Dict, Union
from .Asset import Asset_Class


class Portfolio_Class:
    """
    A class to represent and manage a portfolio of various financial assets.

    Attributes:
    -----------
    portfolio_of_assets : Dict[str, List[Asset_Class]]
        A dictionary containing all asset positions categorized by asset type.

    Methods:
    --------
    generate_combined_value() -> Dict[str, float]:
        Calculates the cumulative value of all assets in the portfolio.

    generate_portfolio_returns():
        Generates a plot of the portfolio's cumulative value over time.

    add_to_portfolio(asset_or_portfolio: Union[Asset_Class, 'PortfolioClass'], asset_type: str = None):
        Adds a specified asset or portfolio to the current portfolio.

    __add__(self, asset_or_portfolio: Union[Asset_Class, 'PortfolioClass']) -> 'PortfolioClass':
        Enables the use of the + operator to add assets or portfolios.
    """

    def __init__(
        self,
        stock_position: List[Asset_Class] = None,
        option_position: List[Asset_Class] = None,
        cash_position: List[Asset_Class] = None,
        fx_positions: List[Asset_Class] = None,
        crypto_position: List[Asset_Class] = None,
        index_positions: List[Asset_Class] = None,
    ):
        """
        Initializes the PortfolioClass with specified asset positions.

        Parameters:
        -----------
        stock_position : List[Asset_Class]
            List of stock positions in the portfolio.
        option_position : List[Asset_Class]
            List of option positions in the portfolio.
        cash_position : List[Asset_Class]
            List of cash positions in the portfolio.
        fx_positions : List[Asset_Class]
            List of forex positions in the portfolio.
        crypto_position : List[Asset_Class]
            List of cryptocurrency positions in the portfolio.
        index_positions : List[Asset_Class]
            List of index positions in the portfolio.
        """
        print("Initializing PortfolioClass...")

        self.portfolio_of_assets: Dict[str, List[Asset_Class]] = {
            "Stock": stock_position or [],
            "Option": option_position or [],
            "Cash": cash_position or [],
            "FX": fx_positions or [],
            "Crypto": crypto_position or [],
            "Index": index_positions or [],
        }

    def generate_combined_value(self) -> Dict[str, float]:
        """
        Calculates the cumulative value of all assets in the portfolio.

        Returns:
        --------
        Dict[str, float]:
            A dictionary of cumulative values for each asset type.
        """
        combined_values = {}
        for asset_type, assets in self.portfolio_of_assets.items():
            combined_values[asset_type] = sum(
                asset.price_vector.sum() for asset in assets
            )
        return combined_values

    def generate_portfolio_returns(self) -> None:
        """
        Generates a plot of the portfolio's cumulative value over time.
        """
        combined_value = self.generate_combined_value()
        fig = px.bar(
            x=combined_value.keys(),
            y=combined_value.values(),
            title="Portfolio Cumulative Value by Asset Type",
        )
        fig.show()

    def add_to_portfolio(
        self, asset_or_portfolio: Union[Asset_Class, "PortfolioClass"]
    ) -> None:
        """
        Adds an asset or another portfolio to the current portfolio.

        Parameters:
        -----------
        asset_or_portfolio : Union[Asset_Class, PortfolioClass]
            The asset or portfolio to add.
        asset_type : str, optional
            The type of the asset if adding a single asset.
        """
        if isinstance(asset_or_portfolio, PortfolioClass):
            for key, value in asset_or_portfolio.portfolio_of_assets.items():
                self.portfolio_of_assets[key].extend(value)
        elif isinstance(asset_or_portfolio, Asset_Class):
            asset_type = asset_or_portfolio.__class__.__name__.replace("_Class", "")
            self.portfolio_of_assets[asset_type].append(asset_or_portfolio)
        else:
            raise TypeError("Unsupported type for addition to portfolio.")

    def subtract_from_portfolio(
            self, asset_or_portfolio: Union[Asset_Class, "PortfolioClass"]
    ) -> None:
        """Removes an asset or another portfolio from the current portfolio."""
        asset_type = asset_or_portfolio.__class__.__name__.replace("_Class", "")
        asset_name = asset_or_portfolio.asset_name

        if isinstance(asset_or_portfolio, PortfolioClass):
            for key, value in asset_or_portfolio.portfolio_of_assets.items():
                for asset in value:
                    if asset in self.portfolio_of_assets[key]:
                        self.portfolio_of_assets[key].remove(asset)
        elif isinstance(asset_or_portfolio, Asset_Class):
            if asset_type not in self.portfolio_of_assets:
                raise ValueError(f"Invalid asset type: {asset_or_portfolio}")
            if asset_name in self.portfolio_of_assets[asset_type]:
                self.portfolio_of_assets[asset_type].remove(asset_or_portfolio)
            else:
                raise ValueError(f"Asset not found in {asset_type} portfolio.")
        else:
            raise TypeError("Invalid input: must specify asset type for a single asset or provide a portfolio.")

    def __add__(
        self, asset_or_portfolio: Union[Asset_Class, "PortfolioClass"]
    ) -> "PortfolioClass":
        """
        Enables the use of the + operator to add assets or portfolios.

        Parameters:
        -----------
        asset_or_portfolio : Union[Asset_Class, PortfolioClass]
            The asset or portfolio to add.

        Returns:
        --------
        PortfolioClass:
            A new portfolio containing the combined assets.
        """
        new_portfolio = PortfolioClass(
            stock_position=self.portfolio_of_assets["Stock"][:],
            option_position=self.portfolio_of_assets["Option"][:],
            cash_position=self.portfolio_of_assets["Cash"][:],
            fx_positions=self.portfolio_of_assets["FX"][:],
            crypto_position=self.portfolio_of_assets["Crypto"][:],
            index_positions=self.portfolio_of_assets["Index"][:],
        )
        if isinstance(asset_or_portfolio, PortfolioClass):
            new_portfolio.add_to_portfolio(asset_or_portfolio)
        elif isinstance(asset_or_portfolio, Asset_Class):
            new_portfolio.add_to_portfolio(asset_or_portfolio)
        else:
            raise TypeError("Unsupported type for addition to portfolio.")

        return new_portfolio

