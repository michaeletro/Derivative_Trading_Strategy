import logging
import re
from typing import Dict, Tuple, Union
from Financial_Products.Asset import Asset_Class
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from scipy.stats import norm
from scipy.optimize import minimize_scalar
import yfinance as yf
from datetime import datetime, timedelta

# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')


class Stock_Class(Asset_Class):
    """
    A class to represent and manage stock data for a given financial asset.

    Attributes:
    -----------
    ticker_data : yfinance.Ticker
        An instance of the Yahoo Finance Ticker class.
    option_data_frame : Dict[str, Union[pd.DataFrame, dict]]
        A dictionary containing call, put, and total options data.

    Methods:
    --------
    __init__(self, ticker: str):
        Initializes the Stock_Class with the specified ticker symbol.

    _generate_option_ticker_mid_point(self) -> Dict[str, Union[pd.DataFrame, dict]]:
        Generates option mid-point prices for calls and puts.

    parse_option_details(self) -> Tuple[datetime, str]:
        Parses option details to extract expiration date and option type.

    get_option_by_index(self, option_type: str, index_of_date: int, option_strike: int = None):
        Retrieves options data by date index and strike price.
    """

    def __init__(self, asset_name: str):
        """
        Initializes the Stock_Class with stock and options data.

        Parameters:
        -----------
        ticker : str
            Ticker symbol for the financial asset.
        """
        logging.info("Generating a Stock Class...")
        super().__init__(asset_name=asset_name)

        self.ticker_data = yf.Ticker(asset_name)
        self.option_data_frame = self._generate_option_ticker_mid_point()
        self.call_options_mid_point = self.option_data_frame['Calls']
        self.put_options_mid_point = self.option_data_frame['Puts']

        self.expirations = self.call_options_mid_point.columns
        self.strikes = self.call_options_mid_point.index
        self.current_price = self.price_vector.iloc[-1]
        self.risk_free_rate = 0.01

        #self.call_iv_surface = self.build_iv_surface(risk_free_rate=self.risk_free_rate, derivative="call")
        #self.put_iv_surface = self.build_iv_surface(risk_free_rate=self.risk_free_rate, derivative="put")

        #self.build_and_plot_iv_surface()

    def _interpolate_option_df(self, option_data: pd.DataFrame = None, interpolation_method='spline', order=3):
        full_strikes = np.arange(option_data.index.min(), option_data.index.max() + 1, step=0.5)
        full_dates = pd.date_range(option_data.columns.min(), option_data.columns.max())

        option_data = option_data.reindex(index=full_strikes, columns=full_dates)

        if interpolation_method in ['polynomial', 'spline'] and order is None:
            raise ValueError("You must specify 'order' for 'polynomial' or 'spline' interpolation.")

        option_data = option_data.interpolate(method=interpolation_method, axis=0, limit_direction='both', order=order)
        option_data = option_data.interpolate(method=interpolation_method, axis=1, limit_direction='both', order=order)

        return option_data.sort_index(axis=0).sort_index(axis=1)

    def _generate_option_ticker_mid_point(self) -> Dict[str, Union[pd.DataFrame, dict]]:
        """
        Generates a dictionary of mid-point prices for calls and puts for each option expiration date.

        Returns:
        --------
        Dict[str, Union[pd.DataFrame, dict]]:
            A dictionary containing:
                - 'Calls': Mid-point prices for call options.
                - 'Puts': Mid-point prices for put options.
                - 'Total': Raw options data frames.
        """
        try:
            master_dict = {"Calls": {}, "Puts": {}}

            for date in self.ticker_data.options:
                call_df, put_df = self._get_option_chain_data(date)

                master_dict["Calls"][date] = self._calculate_midpoints(call_df)
                master_dict["Puts"][date] = self._calculate_midpoints(put_df)

            if self.debug:
                logging.info("Option Mid-Point Prices:")
                logging.info(pd.DataFrame(master_dict['Calls']))
                logging.info(pd.DataFrame(master_dict['Puts']))

            return {'Calls': self._interpolate_option_df(pd.DataFrame(master_dict['Calls'])),
                    'Puts': self._interpolate_option_df(pd.DataFrame(master_dict['Puts']))}

        except Exception as e:
            logging.warning(f"Error retrieving option ticker: {e}")
            return {'Calls': pd.DataFrame(), 'Puts': pd.DataFrame()}

    def _get_option_chain_data(self, date: str) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        Retrieves option chain data for a specific expiration date.

        Parameters:
        -----------
        date : str
            Expiration date of the options.

        Returns:
        --------
        Tuple[pd.DataFrame, pd.DataFrame]:
            DataFrames for call and put options.
        """
        option_chain = self.ticker_data.option_chain(date=date)
        if self.debug:
            logging.info(f"Option Chain Data for {date}:")
            logging.info(option_chain)
        return option_chain.calls, option_chain.puts

    def _calculate_midpoints(self, df: pd.DataFrame) -> Dict[float, float]:
        """
        Calculates the mid-point prices for a given options DataFrame.

        Parameters:
        -----------
        df : pd.DataFrame
            Options DataFrame (calls or puts).

        Returns:
        --------
        Dict[float, float]:
            A dictionary of strike prices to mid-point prices.
        """
        df = {strike: (bid + ask) / 2 if (bid + ask) / 2 != 0 else last_price
              for strike, bid, ask, last_price in zip(df['strike'], df['bid'], df['ask'], df['lastPrice'])}
        if self.debug:
            logging.info(f"Calculating mid-point prices for {df.shape} options...")
        return df

    def parse_option_details(self) -> Tuple[datetime, str]:
        """
        Parses an option string to extract the expiration date and option type.

        Returns:
        --------
        Tuple[datetime, str]:
            Expiration date as a datetime object and option type ('C' or 'P').

        Raises:
        -------
        ValueError:
            If the option string format is invalid.
        """
        pattern = r'O:[A-Z]+(\d{6})([CP])\d+'
        match = re.search(pattern, self.asset_name)

        if match:
            date_str = match.group(1)
            option_type = match.group(2)
            expiration_date = datetime.strptime(date_str, '%y%m%d')
            return expiration_date, option_type
        else:
            raise ValueError("Invalid option string format")

    def get_option_by_index(self, option_type: str = 'call', index_of_date: int = 0, option_strike: int = None):
        """
        Retrieves option data by the date index and strike price.

        Parameters:
        -----------
        option_type : str
            Type of option ('call' or 'put').
        index_of_date : int
            Index of the expiration date.
        option_strike : int, optional
            Specific strike price. Defaults to the at-the-money price.

        Returns:
        --------
        pd.Series:
            Option prices for the given date and strike.
        """
        options = self.call_options_mid_point if option_type.lower() == "call" else self.put_options_mid_point
        farthest_date_column = options.columns[index_of_date]
        option_column = options[farthest_date_column].sort_index()

        if self.debug:
            logging.info(f"Option Data for {farthest_date_column}:")
            logging.info(option_column)

        if option_strike is None:
            option_strike = int(option_column.index[len(option_column) // 2])  # Default to mid-point strike

        if self.debug:
            logging.info(f"Option Data for Strike Price {option_strike}:")
            logging.info(option_column.loc[option_strike])

        return option_column.loc[option_strike]

    def plot_option_surface(self, include='both', interpolation_method='spline', order=3):
        """
        Generate a 3D surface plot for call and/or put options with interpolation.

        Parameters:
        -----------
        include : str, optional
            Option to include in the surface plot:
            - 'call': Include call options only.
            - 'put': Include put options only.
            - 'both': Include both call and put options.
        interpolation_method : str, optional
            Method for interpolating missing values. Default is 'spline'.
            Examples: 'linear', 'polynomial', 'spline', etc.
        order : int, optional
            The order of the polynomial or spline interpolation. Required for 'polynomial' or 'spline'.
        """
        surfaces = []
        if include in ['call', 'both']:
            surfaces.append(
                go.Surface(
                    z=self.call_options_mid_point.values,
                    x=self.call_options_mid_point.index,  # Strike prices
                    y=self.call_options_mid_point.columns,  # Expiration dates
                    name='Call Options',
                    colorscale='Viridis',
                    opacity=0.8
                )
            )
        if include in ['put', 'both']:
            surfaces.append(
                go.Surface(
                    z=self.put_options_mid_point.values,
                    x=self.put_options_mid_point.index,  # Strike prices
                    y=self.put_options_mid_point.columns,  # Expiration dates
                    name='Put Options',
                    colorscale='Cividis',
                    opacity=0.8
                )
            )

        # Step 5: Plot the surface
        fig = go.Figure(data=surfaces)

        fig.update_layout(
            title="3D Option Surface Plot (Interpolated)",
            scene=dict(
                xaxis_title="Strike Prices",
                yaxis_title="Expiration Dates",
                zaxis_title="Option Prices"
            ),
            template="plotly_dark"
        )

        fig.write_html(f'HTML_Diagrams/{self.asset_name}_option_surface.html')
        fig.show()

    def _black_scholes(self, S, K, T, r, sigma, derivative="call"):
        """Black-Scholes option pricing formula."""
        d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
        d2 = d1 - sigma * np.sqrt(T)
        if derivative == "call":
            return S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
        elif derivative == "put":
            return K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)

    def _calculate_iv(self, market_price, S, K, T, r, derivative="call"):
        """Solve for implied volatility using Black-Scholes."""

        print(market_price)
        print(S)
        print(K)
        print(T)
        print(r)
        def objective_function(sigma):
            return abs(self._black_scholes(S, K, T, r, sigma, derivative) - market_price)

        result = minimize_scalar(objective_function, bounds=(0.001, 3.0), method="bounded")
        return result.x if result.success else np.nan

    def build_iv_surface(self, risk_free_rate=0.01, derivative="call"):
        """
        Build the implied volatility surface using live market data.

        Parameters:
        -----------
        risk_free_rate : float
            Risk-free interest rate.
        option_type : str
            "call" or "put".

        Returns:
        --------
        pd.DataFrame:
            Implied volatility surface with spline interpolation.
        """
        iv_surface = {}
        current_price = self.current_price
        options_df = self.call_options_mid_point if derivative == "call" else self.put_options_mid_point

        for expiry in options_df.columns:
            expiry = expiry.to_pydatetime()
            if self.debug:
                logging.info(f"Calculating IV for {expiry}...")

            T = (expiry - datetime.now()).days/365
            print(pd.Series(options_df.index.values)[:])
            print(options_df[[expiry]][:])
            print(3)
            # Calculate mid-point price and IV
            iv_list = [
                self._calculate_iv(
                    market_price=midpoint,
                    S=current_price.values[0],
                    K=strike,
                    T=T,
                    r=risk_free_rate,
                    derivative=derivative,
                )
                for strike, midpoint in zip(pd.to_numeric(pd.Series(options_df.index.values)[:]), pd.to_numeric(options_df[[expiry]][:]))
            ]
            iv_surface[expiry] = pd.Series(iv_list, index=options_df.index)

        # Build IV surface DataFrame
        iv_df = pd.DataFrame(iv_surface)
        iv_df.index = pd.to_numeric(iv_df.index, errors='coerce')
        iv_df.columns = pd.to_datetime(iv_df.columns, errors='coerce')

        return iv_df.sort_index(axis=0).sort_index(axis=1)

    def plot_iv_surface(self, iv_surface_1 : pd.DataFrame=None, iv_surface_2: pd.DataFrame=None):
        """
        Plot the implied volatility surface as a 3D interactive plot with adjustable camera angles.
        Ensures all strike prices and expiration dates are correctly displayed.

        Parameters:
        -----------
        iv_surface_1 : pd.DataFrame
            Implied volatility surface 1 (call options).
        iv_surface_2 : pd.DataFrame, optional
            Implied volatility surface 2 (put options).
        derivative : str
            Type of options: 'call' or 'put'.
        """
        fig = go.Figure()

        # Plot Surface 1
        fig.add_trace(
            go.Surface(
                z=iv_surface_1.values,
                x=iv_surface_1.columns,  # Strike prices
                y=iv_surface_1.index,  # Expiration dates
                colorscale="Viridis",
                name=f"IV Surface (Surface 1)",
                opacity=0.8,
            )
        )

        # Plot Surface 2 (optional)
        if iv_surface_2 is not None:
            fig.add_trace(
                go.Surface(
                    z=iv_surface_2.values,
                    x=iv_surface_2.columns,
                    y=iv_surface_2.index,
                    colorscale="Cividis",
                    name=f"IV Surface (Surface 2)",
                    opacity=0.8,
                )
            )

        # Add Current Stock Price Indicator
        current_price = self.current_price
        stock_price_line_x = [current_price] * len(iv_surface_1.columns)
        stock_price_line_y = iv_surface_1.columns
        stock_price_line_z = [np.nanmean(iv_surface_1.values)] * len(iv_surface_1.columns)

        fig.add_trace(
            go.Scatter3d(
                x=stock_price_line_x,
                y=stock_price_line_y,
                z=stock_price_line_z,
                mode="lines+markers",
                name="Current Stock Price",
                line=dict(color="red", width=5),
                marker=dict(size=5, color="red"),
            )
        )

        # Update Layout
        fig.update_layout(
            title=f"{self.asset_name} Implied Volatility Surface",
            scene=dict(
                xaxis_title="Strike Prices",
                yaxis_title="Expiration Dates",
                zaxis_title="Implied Volatility",
                xaxis=dict(range=[iv_surface_1.columns.min(), iv_surface_1.columns.max()]),  # Force x-axis range
                yaxis=dict(range=[iv_surface_1.index.min(), iv_surface_1.index.max()]),  # Force y-axis range
                zaxis=dict(range=[iv_surface_1.values.min(), iv_surface_1.values.max()]),  # Force z-axis range
            ),
            template="plotly_dark",
        )
        fig.show()
        # Save and Display Plot
        if self.bool_to_save_paths:
            fig.write_html(f'{self.save_path}/{self.asset_name}_iv_surface.html')

    def build_and_plot_iv_surface(self, risk_free_rate=0.01):
        """Build and plot the interpolated implied volatility surface."""
        call_surface = self.build_iv_surface(risk_free_rate=risk_free_rate, derivative="call")
        put_surface = self.build_iv_surface(risk_free_rate=risk_free_rate, derivative="put")

        self.plot_iv_surface(put_surface, call_surface)
