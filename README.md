# Derivative Trading Strategy API

## Comprehensive Overview

Welcome to the **Derivative Trading Strategy API**, an advanced quantitative trading platform built from the ground up with **C++17** for maximum performance and reliability. This comprehensive system integrates **market data retrieval**, **portfolio optimization**, and **sophisticated risk management** capabilities, serving as a complete solution for financial engineering, algorithmic trading research, and quantitative strategy development.

### Purpose & Vision

This project represents a full-stack trading system that bridges the gap between market data and actionable trading strategies. By leveraging **Polygon's API** as the primary data source, the system implements a robust architecture based on **inheritance polymorphism**, enabling elegant handling of diverse financial instruments including stocks, cryptocurrencies, forex, options, and indices. Each financial asset is represented as a specialized object derived from a common base, allowing for unified data processing and analysis.

The platform's core philosophy centers on:
- **Performance-First Design**: Built with modern C++ to deliver low-latency data processing and real-time portfolio calculations
- **Quantitative Rigor**: Implementing industry-standard metrics (VaR, Sharpe Ratio, Beta) and optimization techniques (mean-variance optimization)
- **Extensibility**: Modular architecture facilitating the addition of new trading strategies, risk models, and data sources
- **Research-Ready**: Comprehensive backtesting framework enabling systematic evaluation of trading strategies against historical data

### Key Differentiators

Unlike traditional trading platforms, this system provides:
- **Object-Oriented Financial Modeling**: Inheritance-based design allows seamless integration of new asset classes while maintaining code reusability
- **Integrated Data Pipeline**: From API requests to database storage to portfolio optimization—all in one cohesive system
- **Research-to-Production Path**: Strategies developed and backtested can transition to live trading with minimal modification
- **Academic & Professional Applications**: Suitable for educational purposes, quantitative research, and production trading systems

### Future Vision

The roadmap includes ambitious enhancements:
- **Machine Learning Integration**: Implementing statistical, stochastic, and ML-based forecasting models for portfolio prediction and trade signal generation
- **Advanced Risk Modeling**: Multi-factor risk models using Eigen decomposition and covariance matrix analysis
- **Real-Time Trading Capabilities**: WebSocket integration for live market data and trade execution
- **Gamification & Community**: Trading competitions, performance leaderboards, and AI-driven challenges to foster engagement
- **Broker Integration**: Direct connectivity with platforms like Alpaca, Interactive Brokers, and Binance for live trading

## Features & Functional Capabilities

### 📈 Asset Data Retrieval & Management
- **Multi-Asset Support**: Retrieve and manage stocks, cryptocurrencies, forex pairs, options, and market indices
- **Historical Data Access**: Query historical price data, volume, and market statistics across multiple timeframes
- **Real-Time Updates**: Integration with market data APIs for current pricing and portfolio valuation
- **Flexible Data Storage**: SQLite database backend for efficient local data persistence and quick retrieval

### 📊 Portfolio Optimization & Strategy Development
- **Mean-Variance Optimization**: Implement Modern Portfolio Theory (MPT) for efficient frontier calculation and optimal asset allocation
- **Custom Portfolio Construction**: Build and manage multiple portfolios with different risk profiles and investment strategies
- **Automatic Rebalancing**: Portfolio weight adjustments based on market movements and optimization parameters
- **Strategy Customization**: Framework for implementing various trading strategies (momentum, mean reversion, pairs trading, etc.)

### 📉 Risk Management & Metrics
- **Value-at-Risk (VaR)**: Compute portfolio risk exposure using historical simulation methods
- **Sharpe Ratio Calculation**: Risk-adjusted return metrics for portfolio performance evaluation
- **Beta & Market Correlation**: Measure systematic risk and correlation with market benchmarks
- **Real-Time Risk Monitoring**: Continuous portfolio risk assessment with automatic metric updates

### 📡 Backtesting Framework
- **Historical Simulation**: Test trading strategies using historical market data to evaluate performance
- **Performance Analytics**: Calculate returns, drawdowns, win rates, and other key performance indicators
- **Strategy Comparison**: Run multiple strategies simultaneously to identify optimal approaches
- **Parameter Optimization**: Systematic testing of strategy parameters to maximize risk-adjusted returns

### 🔍 Market Data Analysis
- **Trend Analysis**: Identify market trends and momentum indicators
- **Statistical Analysis**: Perform correlation analysis, volatility calculations, and statistical tests
- **Time Series Processing**: Handle and analyze time series data with specialized classes for each asset type
- **Data Quality Management**: Robust parsing and validation of market data from multiple sources

### 🚀 API & Integration Capabilities
- **RESTful API**: Crow-powered web framework exposing comprehensive HTTP endpoints for all functionality
- **External Data Sources**: Integrated with Polygon API with extensible architecture for additional providers
- **Quantitative Research Tools**: Connect with external modeling tools and data analysis platforms
- **Cross-Platform Compatibility**: C++ core with potential for language bindings (Python, JavaScript)

## Technical Architecture

### Core Technologies & Dependencies

The system is built on a robust technical stack designed for performance, maintainability, and extensibility:

#### **Primary Frameworks**
- **Crow Web Framework**: Lightweight, header-only C++ web framework providing the RESTful API layer. Enables fast HTTP request handling with minimal overhead
- **SQLite3**: Embedded relational database for efficient local data storage and retrieval. Provides ACID compliance without requiring a separate database server
- **Eigen3**: High-performance C++ template library for linear algebra operations. Essential for portfolio optimization, covariance matrix calculations, and numerical analysis
- **cURL (libcurl)**: Industry-standard library for HTTP client operations, enabling seamless integration with external APIs like Polygon

#### **Additional Dependencies**
- **Boost Libraries**: 
  - `boost_system`: Core system-level functionality
  - `boost_thread`: Multi-threading support for concurrent operations
  - `boost_filesystem`: File system operations and path management
- **OpenSSL**: Cryptographic library for secure HTTPS connections (`libssl`, `libcrypto`)
- **WebSocketPP**: WebSocket protocol implementation for real-time data streaming (future integration)
- **pthread**: POSIX threads library for low-level concurrency management

### Architectural Design Patterns

#### **Inheritance Polymorphism**
The system employs object-oriented design principles with inheritance hierarchies for financial instruments:
- **Base Classes**: `AssetClass` serves as the abstract base for all financial instruments, defining common interfaces
- **Derived Classes**: Specialized implementations for each asset type:
  - `StockClass`: Equity securities with dividend tracking
  - `CryptoClass`: Cryptocurrency assets with 24/7 market characteristics
  - `ForexClass`: Foreign exchange pairs with bid/ask spreads
  - `OptionClass`: Derivative contracts with Greeks calculations
  - `TimeSeriesClass`: Generic time series data container

This polymorphic design enables:
- **Code Reusability**: Common functionality implemented once in base classes
- **Type Safety**: Compile-time type checking with runtime polymorphism
- **Extensibility**: New asset types can be added by inheriting from base classes

#### **Modular Component Structure**
```
src/backend/cpp_src/
├── headers/
│   ├── APIConnection/        # API client and data fetching logic
│   ├── AssetClasses/          # Financial instrument class definitions
│   ├── TimeSeriesClasses/     # Time series data structures
│   ├── DataBase/              # Database interface and operations
│   ├── CrowRouteHandling/     # HTTP route handlers
│   ├── DataStructs/           # Core data structures
│   └── Utilities/             # Helper functions and utilities
├── src/                       # Implementation files (.cpp)
└── Makefile                   # Build system configuration
```

#### **Data Flow Architecture**
1. **API Request Layer**: Client makes HTTP request to Crow server
2. **Route Handler**: `CrowRouteHandler` processes request and validates parameters
3. **Business Logic**: Appropriate service classes execute the requested operation
4. **Data Access Layer**: `DataBaseClass` handles SQLite queries and transactions
5. **External API Integration**: `APIConnectionClass` fetches data from Polygon API when needed
6. **Response Generation**: Results formatted as JSON and returned to client

### Build System
- **GNU Make**: Configured with automatic dependency detection and parallel compilation
- **C++17 Standard**: Modern C++ features including lambda expressions, structured bindings, and optional types
- **Compiler Optimization**: `-O2` flag for production builds with `-g` debug mode available
- **Header-Only Libraries**: Crow and Eigen3 provide template-based implementations without linking overhead

## Setup & Installation

### Prerequisites

Before building the Derivative Trading Strategy API, ensure your development environment has the following dependencies installed:

#### **Required Tools**
- **C++ Compiler**: GCC 7.0+ or Clang 5.0+ with C++17 support
  ```sh
  # Verify compiler version
  g++ --version  # Should be 7.0 or higher
  ```

#### **Required Libraries**

**On Ubuntu/Debian:**
```sh
sudo apt-get update
sudo apt-get install -y \
  build-essential \
  libsqlite3-dev \
  libeigen3-dev \
  libcurl4-openssl-dev \
  libssl-dev \
  libboost-system-dev \
  libboost-thread-dev \
  libboost-filesystem-dev
```

**On macOS:**
```sh
# Using Homebrew
brew install sqlite eigen curl openssl boost

# Using MacPorts
sudo port install sqlite3 eigen3 curl openssl boost
```

**On Fedora/RHEL/CentOS:**
```sh
sudo dnf install -y \
  gcc-c++ \
  sqlite-devel \
  eigen3-devel \
  libcurl-devel \
  openssl-devel \
  boost-devel
```

#### **Crow Framework Installation**
Crow is a header-only library that can be installed in multiple ways:

**Option 1: System-wide installation**
```sh
# Clone Crow repository
git clone https://github.com/CrowCpp/Crow.git
cd Crow
sudo cp -r include/crow /usr/local/include/

# Or use package manager if available
# Ubuntu 22.04+: sudo apt-get install libcrow-dev
```

**Option 2: Bundled with project**
The Crow headers may already be included in the project structure, check if `headers/crow` exists.

### Installation & Build Instructions

#### **Step 1: Clone the Repository**
```sh
git clone https://github.com/michaeletro/Derivative_Trading_Strategy.git
cd Derivative_Trading_Strategy
```

#### **Step 2: Navigate to Build Directory**
```sh
cd src/backend/cpp_src
```

#### **Step 3: Verify Dependencies**
Before building, ensure all required headers can be found:
```sh
# Check for Eigen3
ls /usr/include/eigen3/Eigen/Dense

# Check for SQLite3
pkg-config --libs sqlite3

# Check for Boost
ldconfig -p | grep boost
```

#### **Step 4: Build the Project**
```sh
# Clean any previous builds
make clean

# Compile the project
make

# For debug build with symbols:
make debug
```

Expected output:
```
🛠️  Compiling src/main.cpp ➜ obj/main.o
🛠️  Compiling src/database/DataBaseClass.cpp ➜ obj/database/DataBaseClass.o
...
🚀 Linking executable...
✅ Executable built successfully!
✅ Build complete! Run bin/server
```

#### **Step 5: Configure API Keys (Optional)**
If using live data from Polygon API:
```sh
# Create environment variable or configuration file
export POLYGON_API_KEY="your_api_key_here"

# Or edit configuration file if available
nano config/api_config.json
```

#### **Step 6: Initialize Database**
```sh
# The database will be created automatically on first run
# Optionally, you can pre-populate with sample data:
./bin/server --init-db
```

#### **Step 7: Run the Server**
```sh
# Start the API server
./bin/server

# Run on custom port (if supported)
./bin/server --port 3000

# Run in verbose mode for debugging
./bin/server --verbose
```

The server should start and display:
```
Crow/0.3+ server is running at http://localhost:8080
Server listening on port 8080
```

#### **Step 8: Verify Installation**
Test that the API is responding correctly:

```sh
# Test health/status endpoint
curl http://localhost:8080/

# Fetch asset data
curl http://localhost:8080/api/asset_data

# Fetch specific asset
curl http://localhost:8080/api/asset_data/AAPL
```

### Common Installation Issues

**Issue: `crow.h` not found**
- Solution: Install Crow headers or verify include paths in Makefile

**Issue: Eigen3 headers not found**
- Solution: Check if Eigen3 is in `/usr/include/eigen3` or adjust `-I` flag in Makefile

**Issue: Boost linking errors**
- Solution: Ensure all Boost components are installed: `libboost-all-dev` on Ubuntu

**Issue: Permission denied when running server**
- Solution: Ensure executable has proper permissions: `chmod +x bin/server`

**Issue: Port 8080 already in use**
- Solution: Check for processes using the port: `lsof -i :8080` and kill or use different port

### Development Workflow

For active development:
```sh
# Use build script for quick compilation
./build.sh

# Or use make with automatic rebuild on changes
while true; do make && ./bin/server; sleep 2; done

# Run with debugger
gdb ./bin/server
```

## API Usage & Examples

The Derivative Trading Strategy API exposes a comprehensive RESTful interface for all trading operations. All endpoints return JSON responses and support standard HTTP status codes.

### Base URL
```
http://localhost:8080
```

### Response Format
All API responses follow a consistent format:
```json
{
  "status": "success|error",
  "data": { ... },
  "message": "Optional message",
  "timestamp": "ISO 8601 timestamp"
}
```

---

### 1️⃣ Asset Data Retrieval

#### **Fetch All Assets**
Retrieve a list of all assets currently tracked in the system.

**Endpoint:**
```http
GET /api/asset_data
```

**Query Parameters:**
- `asset_type` (optional): Filter by type (`stock`, `crypto`, `forex`, `option`)
- `limit` (optional): Maximum number of results (default: 100)
- `offset` (optional): Pagination offset (default: 0)

**Example Request:**
```sh
curl http://localhost:8080/api/asset_data?asset_type=stock&limit=10
```

**Example Response:**
```json
{
  "status": "success",
  "data": {
    "assets": [
      {
        "ticker": "AAPL",
        "name": "Apple Inc.",
        "asset_type": "stock",
        "last_price": 178.45,
        "last_updated": "2024-01-15T16:00:00Z"
      },
      {
        "ticker": "GOOGL",
        "name": "Alphabet Inc.",
        "asset_type": "stock",
        "last_price": 142.30,
        "last_updated": "2024-01-15T16:00:00Z"
      }
    ],
    "total": 2,
    "limit": 10,
    "offset": 0
  }
}
```

---

#### **Fetch Asset by Ticker**
Retrieve detailed information for a specific asset.

**Endpoint:**
```http
GET /api/asset_data/{ticker}
```

**Path Parameters:**
- `ticker` (required): Asset ticker symbol (e.g., AAPL, BTC-USD, EUR/USD)

**Query Parameters:**
- `include_history` (optional): Include historical price data (true/false, default: false)
- `days` (optional): Number of days of history to include (default: 30)

**Example Request:**
```sh
curl http://localhost:8080/api/asset_data/AAPL
```

**Example Response:**
```json
{
  "status": "success",
  "data": {
    "ticker": "AAPL",
    "name": "Apple Inc.",
    "asset_type": "stock",
    "current_price": 178.45,
    "open": 176.20,
    "high": 179.10,
    "low": 175.80,
    "volume": 58234567,
    "market_cap": 2845000000000,
    "pe_ratio": 29.34,
    "last_updated": "2024-01-15T16:00:00Z"
  }
}
```

**With Historical Data:**
```sh
curl "http://localhost:8080/api/asset_data/AAPL?include_history=true&days=7"
```

---

#### **Search Assets**
Search for assets by name or ticker.

**Endpoint:**
```http
GET /api/asset_data/search
```

**Query Parameters:**
- `q` (required): Search query
- `asset_type` (optional): Filter by asset type

**Example Request:**
```sh
curl "http://localhost:8080/api/asset_data/search?q=apple"
```

---

### 2️⃣ Portfolio Management & Optimization

#### **Fetch User Portfolio**
Retrieve complete portfolio information including holdings and performance metrics.

**Endpoint:**
```http
GET /api/user/{username}/portfolio
```

**Path Parameters:**
- `username` (required): User identifier

**Example Request:**
```sh
curl http://localhost:8080/api/user/trader123/portfolio
```

**Example Response:**
```json
{
  "status": "success",
  "data": {
    "username": "trader123",
    "portfolio_id": "PF-001",
    "balance": 25000.00,
    "total_value": 103450.75,
    "invested_value": 78450.75,
    "cash": 25000.00,
    "total_return": 8.5,
    "total_return_pct": 10.83,
    "holdings": [
      {
        "ticker": "AAPL",
        "quantity": 100,
        "avg_cost": 165.50,
        "current_price": 178.45,
        "total_value": 17845.00,
        "unrealized_pnl": 1295.00,
        "unrealized_pnl_pct": 7.82,
        "weight": 17.24
      },
      {
        "ticker": "GOOGL",
        "quantity": 150,
        "avg_cost": 138.20,
        "current_price": 142.30,
        "total_value": 21345.00,
        "unrealized_pnl": 615.00,
        "unrealized_pnl_pct": 2.97,
        "weight": 20.63
      }
    ],
    "last_updated": "2024-01-15T16:00:00Z"
  }
}
```

---

#### **Create Portfolio**
Initialize a new portfolio for a user.

**Endpoint:**
```http
POST /api/portfolio/create
```

**Request Body:**
```json
{
  "username": "trader123",
  "initial_balance": 100000.00,
  "portfolio_name": "Growth Portfolio",
  "risk_profile": "moderate"
}
```

**Example Request:**
```sh
curl -X POST http://localhost:8080/api/portfolio/create \
  -H "Content-Type: application/json" \
  -d '{
    "username": "trader123",
    "initial_balance": 100000.00,
    "portfolio_name": "Growth Portfolio"
  }'
```

---

#### **Optimize Portfolio**
Apply portfolio optimization algorithms to suggest optimal asset allocation.

**Endpoint:**
```http
POST /api/portfolio/optimize
```

**Request Body:**
```json
{
  "username": "trader123",
  "optimization_type": "mean_variance",
  "target_return": 0.12,
  "risk_tolerance": "moderate",
  "constraints": {
    "max_weight_per_asset": 0.25,
    "min_weight_per_asset": 0.02,
    "allow_short": false
  }
}
```

**Optimization Types:**
- `mean_variance`: Modern Portfolio Theory optimization
- `min_variance`: Minimize portfolio variance
- `max_sharpe`: Maximize Sharpe ratio
- `risk_parity`: Equal risk contribution

**Example Request:**
```sh
curl -X POST http://localhost:8080/api/portfolio/optimize \
  -H "Content-Type: application/json" \
  -d '{
    "username": "trader123",
    "optimization_type": "mean_variance",
    "target_return": 0.12
  }'
```

**Example Response:**
```json
{
  "status": "success",
  "data": {
    "optimization_type": "mean_variance",
    "recommended_weights": {
      "AAPL": 0.18,
      "GOOGL": 0.22,
      "MSFT": 0.15,
      "AMZN": 0.20,
      "SPY": 0.25
    },
    "expected_return": 0.1245,
    "expected_volatility": 0.1567,
    "sharpe_ratio": 0.794,
    "changes_required": [
      {
        "ticker": "AAPL",
        "action": "buy",
        "quantity": 50,
        "estimated_cost": 8922.50
      },
      {
        "ticker": "GOOGL",
        "action": "sell",
        "quantity": 25,
        "estimated_proceeds": 3557.50
      }
    ]
  }
}
```

---

#### **Add Position**
Add a new position to the portfolio.

**Endpoint:**
```http
POST /api/portfolio/{username}/position
```

**Request Body:**
```json
{
  "ticker": "AAPL",
  "quantity": 100,
  "price": 178.45,
  "transaction_type": "buy",
  "transaction_date": "2024-01-15"
}
```

**Example Request:**
```sh
curl -X POST http://localhost:8080/api/portfolio/trader123/position \
  -H "Content-Type: application/json" \
  -d '{
    "ticker": "AAPL",
    "quantity": 100,
    "price": 178.45,
    "transaction_type": "buy"
  }'
```

---

### 3️⃣ Risk Management Metrics

#### **Calculate Value-at-Risk (VaR)**
Compute portfolio Value-at-Risk using historical simulation.

**Endpoint:**
```http
GET /api/portfolio/{username}/var
```

**Query Parameters:**
- `confidence_level` (optional): Confidence level (0.90, 0.95, 0.99, default: 0.95)
- `time_horizon` (optional): Time horizon in days (default: 1)
- `method` (optional): Calculation method (`historical`, `parametric`, `monte_carlo`)

**Example Request:**
```sh
curl "http://localhost:8080/api/portfolio/trader123/var?confidence_level=0.95&time_horizon=1"
```

**Example Response:**
```json
{
  "status": "success",
  "data": {
    "username": "trader123",
    "portfolio_value": 103450.75,
    "var_amount": -2587.54,
    "var_percentage": -2.50,
    "confidence_level": 0.95,
    "time_horizon_days": 1,
    "method": "historical",
    "interpretation": "With 95% confidence, portfolio will not lose more than $2,587.54 over 1 day",
    "calculated_at": "2024-01-15T16:00:00Z"
  }
}
```

---

#### **Calculate Sharpe Ratio**
Calculate risk-adjusted returns for the portfolio.

**Endpoint:**
```http
GET /api/portfolio/{username}/sharpe
```

**Query Parameters:**
- `risk_free_rate` (optional): Annual risk-free rate (default: 0.04)
- `period` (optional): Calculation period in days (default: 252 for 1 year)

**Example Request:**
```sh
curl "http://localhost:8080/api/portfolio/trader123/sharpe?risk_free_rate=0.04"
```

**Example Response:**
```json
{
  "status": "success",
  "data": {
    "sharpe_ratio": 1.23,
    "annualized_return": 0.145,
    "annualized_volatility": 0.085,
    "risk_free_rate": 0.04,
    "interpretation": "Portfolio generates 1.23 units of return per unit of risk"
  }
}
```

---

#### **Retrieve Portfolio Beta**
Calculate portfolio systematic risk relative to market benchmark.

**Endpoint:**
```http
GET /api/portfolio/{username}/beta
```

**Query Parameters:**
- `benchmark` (optional): Benchmark ticker (default: SPY)
- `period` (optional): Calculation period in days (default: 252)

**Example Request:**
```sh
curl "http://localhost:8080/api/portfolio/trader123/beta?benchmark=SPY"
```

**Example Response:**
```json
{
  "status": "success",
  "data": {
    "beta": 1.15,
    "alpha": 0.023,
    "correlation": 0.87,
    "r_squared": 0.76,
    "benchmark": "SPY",
    "interpretation": "Portfolio is 15% more volatile than the market"
  }
}
```

---

#### **Calculate All Risk Metrics**
Get comprehensive risk analysis in a single request.

**Endpoint:**
```http
GET /api/portfolio/{username}/risk_metrics
```

**Example Request:**
```sh
curl http://localhost:8080/api/portfolio/trader123/risk_metrics
```

**Example Response:**
```json
{
  "status": "success",
  "data": {
    "portfolio_value": 103450.75,
    "var_95": -2587.54,
    "var_99": -3724.18,
    "sharpe_ratio": 1.23,
    "sortino_ratio": 1.45,
    "beta": 1.15,
    "alpha": 0.023,
    "max_drawdown": -0.08,
    "volatility": 0.085,
    "correlation_with_market": 0.87
  }
}
```

---

### 4️⃣ Trading Strategy & Backtesting

#### **Run Backtest**
Execute a backtesting simulation for a specific trading strategy.

**Endpoint:**
```http
POST /api/backtest
```

**Request Body:**
```json
{
  "username": "trader123",
  "strategy": "momentum",
  "tickers": ["AAPL", "GOOGL", "MSFT", "AMZN"],
  "start_date": "2023-01-01",
  "end_date": "2023-12-31",
  "initial_capital": 100000,
  "parameters": {
    "lookback_period": 20,
    "holding_period": 5,
    "rebalance_frequency": "monthly"
  }
}
```

**Available Strategies:**
- `momentum`: Price momentum-based strategy
- `mean_reversion`: Mean reversion strategy
- `pairs_trading`: Statistical arbitrage pairs trading
- `buy_and_hold`: Passive benchmark strategy
- `moving_average_crossover`: Technical indicator-based strategy

**Example Request:**
```sh
curl -X POST http://localhost:8080/api/backtest \
  -H "Content-Type: application/json" \
  -d '{
    "username": "trader123",
    "strategy": "momentum",
    "start_date": "2023-01-01",
    "end_date": "2023-12-31",
    "initial_capital": 100000
  }'
```

**Example Response:**
```json
{
  "status": "success",
  "data": {
    "strategy": "momentum",
    "backtest_period": {
      "start": "2023-01-01",
      "end": "2023-12-31",
      "trading_days": 252
    },
    "performance": {
      "initial_capital": 100000,
      "final_value": 118450.75,
      "total_return": 0.1845,
      "annualized_return": 0.1845,
      "max_drawdown": -0.12,
      "sharpe_ratio": 1.34,
      "sortino_ratio": 1.56,
      "win_rate": 0.58,
      "total_trades": 87,
      "profitable_trades": 50,
      "average_win": 845.30,
      "average_loss": -523.15
    },
    "equity_curve": [
      {"date": "2023-01-01", "value": 100000},
      {"date": "2023-01-02", "value": 100234.50},
      "..."
    ],
    "trades": [
      {
        "date": "2023-01-15",
        "ticker": "AAPL",
        "action": "buy",
        "quantity": 100,
        "price": 165.50,
        "total_cost": 16550.00
      }
    ]
  }
}
```

---

#### **Compare Strategies**
Compare multiple strategies side by side.

**Endpoint:**
```http
POST /api/backtest/compare
```

**Request Body:**
```json
{
  "strategies": ["momentum", "mean_reversion", "buy_and_hold"],
  "start_date": "2023-01-01",
  "end_date": "2023-12-31",
  "initial_capital": 100000
}
```

---

#### **List Available Strategies**
Get list of all implemented trading strategies.

**Endpoint:**
```http
GET /api/strategies
```

**Example Response:**
```json
{
  "status": "success",
  "data": {
    "strategies": [
      {
        "name": "momentum",
        "description": "Price momentum strategy based on recent performance",
        "parameters": ["lookback_period", "holding_period"]
      },
      {
        "name": "mean_reversion",
        "description": "Statistical mean reversion strategy",
        "parameters": ["z_score_threshold", "lookback_period"]
      }
    ]
  }
}
```

---

### 5️⃣ Market Data Analysis

#### **Get Historical Data**
Retrieve historical price data for an asset.

**Endpoint:**
```http
GET /api/market_data/{ticker}/history
```

**Query Parameters:**
- `start_date` (required): Start date (YYYY-MM-DD)
- `end_date` (required): End date (YYYY-MM-DD)
- `interval` (optional): Data interval (`1d`, `1h`, `5m`, default: `1d`)

**Example Request:**
```sh
curl "http://localhost:8080/api/market_data/AAPL/history?start_date=2023-01-01&end_date=2023-12-31"
```

---

#### **Calculate Correlation Matrix**
Compute correlation matrix for a set of assets.

**Endpoint:**
```http
POST /api/market_data/correlation
```

**Request Body:**
```json
{
  "tickers": ["AAPL", "GOOGL", "MSFT"],
  "start_date": "2023-01-01",
  "end_date": "2023-12-31"
}
```

**Example Response:**
```json
{
  "status": "success",
  "data": {
    "correlation_matrix": {
      "AAPL": {"AAPL": 1.00, "GOOGL": 0.75, "MSFT": 0.82},
      "GOOGL": {"AAPL": 0.75, "GOOGL": 1.00, "MSFT": 0.79},
      "MSFT": {"AAPL": 0.82, "GOOGL": 0.79, "MSFT": 1.00}
    }
  }
}
```

---

### Error Handling

The API uses standard HTTP status codes:

- `200 OK`: Request successful
- `201 Created`: Resource created successfully
- `400 Bad Request`: Invalid request parameters
- `404 Not Found`: Resource not found
- `500 Internal Server Error`: Server error

**Error Response Format:**
```json
{
  "status": "error",
  "error": {
    "code": "INVALID_TICKER",
    "message": "Asset with ticker 'XYZ123' not found",
    "details": "Please verify the ticker symbol is correct"
  },
  "timestamp": "2024-01-15T16:00:00Z"
}
```

### Rate Limiting

Currently, the API does not implement rate limiting, but production deployments should consider:
- Per-user request limits
- Global request limits
- Backtest execution limits (computationally expensive)

### Authentication (Future)

Future versions will implement authentication via:
- JWT tokens
- API keys
- OAuth 2.0 integration

## Database Structure

The system uses SQLite3 as its embedded relational database, providing ACID-compliant storage without requiring a separate database server. The schema is designed for efficient querying and supports the core functionality of asset management, portfolio tracking, and historical data analysis.

### Core Tables

#### **`assets`**
Stores real-time and historical asset information for all tracked securities.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| id | INTEGER | PRIMARY KEY, AUTOINCREMENT | Unique asset record identifier |
| ticker | TEXT | NOT NULL, INDEX | Asset ticker symbol (e.g., AAPL, BTC-USD) |
| asset_type | TEXT | NOT NULL | Type of asset (`stock`, `crypto`, `forex`, `option`, `index`) |
| name | TEXT | | Full name of the asset |
| date | TEXT | NOT NULL | Date of the record (ISO 8601 format) |
| open_price | REAL | | Opening price for the period |
| close_price | REAL | NOT NULL | Closing price for the period |
| high_price | REAL | | Highest price during the period |
| low_price | REAL | | Lowest price during the period |
| volume | INTEGER | | Trading volume |
| adjusted_close | REAL | | Adjusted closing price (for splits/dividends) |
| created_at | TEXT | DEFAULT CURRENT_TIMESTAMP | Record creation timestamp |

**Indexes:**
- `idx_ticker_date` on (`ticker`, `date`) - For fast time-series queries
- `idx_asset_type` on (`asset_type`) - For filtering by asset class

**Sample Data:**
```sql
INSERT INTO assets (ticker, asset_type, name, date, open_price, close_price, high_price, low_price, volume)
VALUES ('AAPL', 'stock', 'Apple Inc.', '2024-01-15', 176.20, 178.45, 179.10, 175.80, 58234567);
```

---

#### **`portfolios`**
Tracks user portfolio metadata and cash balances.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| portfolio_id | INTEGER | PRIMARY KEY, AUTOINCREMENT | Unique portfolio identifier |
| user_id | INTEGER | NOT NULL, UNIQUE | User identifier (foreign key to users table) |
| username | TEXT | NOT NULL, UNIQUE | Username for quick lookup |
| balance | REAL | NOT NULL, DEFAULT 0 | Available cash balance |
| total_value | REAL | NOT NULL, DEFAULT 0 | Total portfolio value (cash + holdings) |
| initial_capital | REAL | | Starting capital when portfolio was created |
| created_at | TEXT | DEFAULT CURRENT_TIMESTAMP | Portfolio creation date |
| updated_at | TEXT | DEFAULT CURRENT_TIMESTAMP | Last update timestamp |

**Sample Data:**
```sql
INSERT INTO portfolios (user_id, username, balance, total_value, initial_capital)
VALUES (1, 'trader123', 25000.00, 103450.75, 100000.00);
```

---

#### **`holdings`**
Tracks individual asset positions within portfolios.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| holding_id | INTEGER | PRIMARY KEY, AUTOINCREMENT | Unique holding identifier |
| portfolio_id | INTEGER | NOT NULL, FOREIGN KEY | References `portfolios.portfolio_id` |
| ticker | TEXT | NOT NULL | Asset ticker symbol |
| quantity | REAL | NOT NULL | Number of shares/units held |
| avg_cost | REAL | NOT NULL | Average cost basis per unit |
| current_price | REAL | | Most recent price (cached for performance) |
| last_updated | TEXT | DEFAULT CURRENT_TIMESTAMP | Last price update timestamp |

**Indexes:**
- `idx_portfolio_ticker` on (`portfolio_id`, `ticker`) - For portfolio queries
- `idx_ticker_holdings` on (`ticker`) - For asset-specific queries

**Sample Data:**
```sql
INSERT INTO holdings (portfolio_id, ticker, quantity, avg_cost, current_price)
VALUES (1, 'AAPL', 100, 165.50, 178.45);
```

---

#### **`transactions`**
Records all buy/sell transactions for audit trail and performance calculation.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| transaction_id | INTEGER | PRIMARY KEY, AUTOINCREMENT | Unique transaction identifier |
| portfolio_id | INTEGER | NOT NULL, FOREIGN KEY | References `portfolios.portfolio_id` |
| ticker | TEXT | NOT NULL | Asset ticker symbol |
| transaction_type | TEXT | NOT NULL | `buy`, `sell`, `deposit`, `withdrawal` |
| quantity | REAL | NOT NULL | Number of shares/units |
| price | REAL | NOT NULL | Price per unit at time of transaction |
| total_amount | REAL | NOT NULL | Total transaction value (quantity × price) |
| fees | REAL | DEFAULT 0 | Transaction fees/commissions |
| transaction_date | TEXT | NOT NULL | Date of transaction |
| created_at | TEXT | DEFAULT CURRENT_TIMESTAMP | Record creation timestamp |

**Indexes:**
- `idx_portfolio_date` on (`portfolio_id`, `transaction_date`) - For date-range queries
- `idx_ticker_transactions` on (`ticker`) - For asset-specific transaction history

---

#### **`users`** (Optional - for multi-user systems)
Stores user account information.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| user_id | INTEGER | PRIMARY KEY, AUTOINCREMENT | Unique user identifier |
| username | TEXT | NOT NULL, UNIQUE | Username |
| email | TEXT | UNIQUE | Email address |
| password_hash | TEXT | | Hashed password (for authentication) |
| created_at | TEXT | DEFAULT CURRENT_TIMESTAMP | Account creation date |
| last_login | TEXT | | Last login timestamp |

---

#### **`strategy_results`** (For backtesting)
Stores backtesting results for analysis and comparison.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| result_id | INTEGER | PRIMARY KEY, AUTOINCREMENT | Unique result identifier |
| portfolio_id | INTEGER | FOREIGN KEY | References `portfolios.portfolio_id` |
| strategy_name | TEXT | NOT NULL | Name of the strategy tested |
| start_date | TEXT | NOT NULL | Backtest start date |
| end_date | TEXT | NOT NULL | Backtest end date |
| initial_capital | REAL | NOT NULL | Starting capital |
| final_value | REAL | NOT NULL | Ending portfolio value |
| total_return | REAL | | Total return percentage |
| sharpe_ratio | REAL | | Risk-adjusted return metric |
| max_drawdown | REAL | | Maximum drawdown percentage |
| parameters | TEXT | | JSON string of strategy parameters |
| created_at | TEXT | DEFAULT CURRENT_TIMESTAMP | Result creation timestamp |

---

### Database Relationships

```
users (1) ─────→ (1) portfolios
                     ↓ (1)
                     ↓
                     ↓ (many)
                  holdings
                     
portfolios (1) ───→ (many) transactions

assets ───→ Referenced by holdings.ticker
       └──→ Referenced by transactions.ticker
```

### Database Initialization

The database is automatically initialized on first run. The schema can be manually created using:

```sql
-- Create assets table
CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    name TEXT,
    date TEXT NOT NULL,
    open_price REAL,
    close_price REAL NOT NULL,
    high_price REAL,
    low_price REAL,
    volume INTEGER,
    adjusted_close REAL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_ticker_date ON assets(ticker, date);
CREATE INDEX idx_asset_type ON assets(asset_type);

-- Create portfolios table
CREATE TABLE IF NOT EXISTS portfolios (
    portfolio_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL UNIQUE,
    username TEXT NOT NULL UNIQUE,
    balance REAL NOT NULL DEFAULT 0,
    total_value REAL NOT NULL DEFAULT 0,
    initial_capital REAL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Additional tables follow similar pattern...
```

### Query Examples

**Fetch portfolio with holdings:**
```sql
SELECT p.*, h.ticker, h.quantity, h.avg_cost, h.current_price
FROM portfolios p
LEFT JOIN holdings h ON p.portfolio_id = h.portfolio_id
WHERE p.username = 'trader123';
```

**Calculate portfolio performance:**
```sql
SELECT 
    p.username,
    p.total_value,
    p.initial_capital,
    (p.total_value - p.initial_capital) as profit_loss,
    ((p.total_value - p.initial_capital) / p.initial_capital * 100) as return_pct
FROM portfolios p
WHERE p.username = 'trader123';
```

**Get asset price history:**
```sql
SELECT date, close_price, volume
FROM assets
WHERE ticker = 'AAPL'
AND date BETWEEN '2023-01-01' AND '2023-12-31'
ORDER BY date;
```

## Future Enhancements & Roadmap 🚀

The Derivative Trading Strategy platform has an ambitious roadmap focused on expanding functionality, improving performance, and introducing cutting-edge features for quantitative trading and research.

### Phase 1: Machine Learning & AI Integration (Q1-Q2 2024)

#### **Predictive Models for Portfolio Forecasting**
- **Statistical Time Series Models**: 
  - ARIMA (AutoRegressive Integrated Moving Average) for price prediction
  - GARCH (Generalized AutoRegressive Conditional Heteroskedasticity) for volatility forecasting
  - VAR (Vector AutoRegression) for multivariate time series analysis
  
- **Stochastic Models**:
  - Geometric Brownian Motion for asset price simulation
  - Monte Carlo simulations for risk assessment and portfolio optimization
  - Kalman filtering for dynamic parameter estimation
  
- **Machine Learning Models**:
  - LSTM (Long Short-Term Memory) neural networks for sequence prediction
  - Random Forest for feature importance and classification
  - Gradient Boosting (XGBoost, LightGBM) for return prediction
  - Reinforcement Learning for adaptive trading strategy development

#### **Trade Signal Generation**
- Sentiment analysis from news and social media
- Technical indicator ML ensembles
- Anomaly detection for market regime changes
- Multi-factor alpha generation models

### Phase 2: Real-Time Trading Capabilities (Q2-Q3 2024)

#### **WebSocket Integration**
- Real-time market data streaming via WebSocket connections
- Live portfolio updates with sub-second latency
- Order book depth visualization
- Real-time risk metric calculations

#### **Live Trading Integration**
- **Broker API Integrations**:
  - Alpaca API for commission-free stock trading
  - Interactive Brokers TWS API for professional trading
  - Binance API for cryptocurrency trading
  - Polygon.io WebSocket for streaming market data

- **Order Management System**:
  - Market, limit, stop, and trailing stop orders
  - Order routing and execution tracking
  - Position management and reconciliation
  - Commission and slippage modeling

### Phase 3: Advanced Risk & Analytics (Q3-Q4 2024)

#### **Multi-Factor Risk Modeling**
- Fama-French three-factor and five-factor models
- Principal Component Analysis (PCA) using Eigen decomposition
- Stress testing and scenario analysis
- Credit risk modeling for fixed income portfolios

#### **Advanced Portfolio Analytics**
- Attribution analysis (performance decomposition)
- Factor exposure analysis
- Regime-based risk metrics
- Tail risk measurement (Expected Shortfall/CVaR)

#### **Optimization Enhancements**
- Black-Litterman model for incorporating market views
- Robust optimization under uncertainty
- Transaction cost optimization
- Multi-period portfolio optimization

### Phase 4: Gamification & Community Features (Q4 2024)

#### **Competitive Trading Platform**
- **Leaderboards**: Rank traders by Sharpe ratio, returns, risk-adjusted performance
- **Trading Competitions**: Weekly/monthly contests with real-time standings
- **Paper Trading Challenges**: Risk-free strategy testing with competitive element
- **Achievement System**: Badges and rewards for milestones

#### **Social Features**
- Strategy sharing and collaboration
- Public portfolio performance tracking
- Discussion forums and strategy reviews
- Expert trader insights and tutorials

#### **AI-Driven Challenges**
- Algorithmic trading competitions
- ML model accuracy challenges
- Risk prediction contests
- Market timing challenges

### Phase 5: Platform & Infrastructure (Ongoing)

#### **Performance Optimization**
- Multi-threading for parallel portfolio calculations
- Database query optimization and caching
- In-memory data structures for hot data
- GPU acceleration for matrix operations (via CUDA)

#### **Scalability Improvements**
- Horizontal scaling with load balancing
- Distributed computing for backtesting
- Cloud deployment (AWS, GCP, Azure)
- Microservices architecture migration

#### **Developer Experience**
- Python bindings via pybind11 for easier integration
- JavaScript/Node.js client library
- GraphQL API alongside REST
- Comprehensive SDK for third-party developers

#### **Data & Infrastructure**
- Additional data sources (Bloomberg, Refinitiv, Quandl)
- Alternative data integration (satellite imagery, web scraping)
- Data quality monitoring and validation
- Automated data pipeline management

### Phase 6: Enterprise & Advanced Features

#### **Advanced Financial Instruments**
- Options Greeks calculation and hedging strategies
- Futures and commodities trading
- Fixed income and bond portfolio management
- Multi-asset class portfolio optimization

#### **Regulatory & Compliance**
- Transaction reporting and audit trails
- Risk limit monitoring and alerts
- Regulatory compliance checks (Reg T, pattern day trader rules)
- Tax loss harvesting optimization

#### **Research Tools**
- Jupyter notebook integration
- R language bindings for statistical analysis
- Factor research and testing framework
- Walk-forward optimization tools

### Technology Goals

- **Documentation**: Comprehensive API documentation using Swagger/OpenAPI
- **Testing**: Achieve 80%+ code coverage with unit and integration tests
- **CI/CD**: Automated build, test, and deployment pipelines
- **Monitoring**: Application performance monitoring (APM) and logging
- **Security**: API authentication, rate limiting, input validation, SQL injection prevention

### Community Contributions

We welcome contributions in any of these areas:
- New trading strategy implementations
- Additional asset class support
- Performance optimizations
- Bug fixes and code quality improvements
- Documentation enhancements
- Test coverage expansion

## Contributing

We welcome contributions from the community! Whether you're fixing bugs, adding features, improving documentation, or suggesting enhancements, your help is appreciated.

### How to Contribute

#### **1. Fork and Clone**
```sh
# Fork the repository on GitHub, then clone your fork
git clone https://github.com/yourusername/Derivative_Trading_Strategy.git
cd Derivative_Trading_Strategy
```

#### **2. Create a Feature Branch**
```sh
# Create a descriptive branch name
git checkout -b feature/add-new-strategy
# or
git checkout -b fix/portfolio-calculation-bug
```

#### **3. Make Your Changes**
- Write clean, well-documented code
- Follow existing code style and conventions
- Add comments for complex logic
- Update documentation if needed

#### **4. Test Your Changes**
```sh
# Build the project
cd src/backend/cpp_src
make clean && make

# Run tests if available
./bin/test_suite

# Manually test the feature
./bin/server
```

#### **5. Commit and Push**
```sh
# Stage your changes
git add .

# Write a descriptive commit message
git commit -m "Add momentum trading strategy with configurable parameters"

# Push to your fork
git push origin feature/add-new-strategy
```

#### **6. Open a Pull Request**
- Go to the original repository on GitHub
- Click "New Pull Request"
- Select your branch and provide a detailed description
- Reference any related issues (e.g., "Fixes #123")

### Contribution Guidelines

#### **Code Style**
- Use C++17 standard features appropriately
- Follow existing naming conventions:
  - Classes: `PascalCase` (e.g., `PortfolioClass`)
  - Functions: `camelCase` or `snake_case` consistently
  - Variables: `snake_case` (e.g., `total_value`)
  - Constants: `UPPER_SNAKE_CASE` (e.g., `MAX_ITERATIONS`)
- Keep functions focused and single-purpose
- Use meaningful variable and function names

#### **Documentation**
- Add docstring comments for new classes and functions
- Update the README if adding new features or endpoints
- Include usage examples for new functionality
- Document any new dependencies or setup requirements

#### **Testing**
- Test your changes thoroughly before submitting
- Include unit tests for new functionality when possible
- Verify that existing functionality still works
- Test edge cases and error conditions

#### **Areas We Need Help With**
- 🧪 **Testing**: Unit tests, integration tests, test coverage
- 📝 **Documentation**: API docs, tutorials, code comments
- 🐛 **Bug Fixes**: Check the issues page for open bugs
- ✨ **New Features**: Trading strategies, risk metrics, data sources
- 🎨 **Frontend**: Web interface for portfolio visualization
- ⚡ **Performance**: Optimization, profiling, benchmarking
- 🔒 **Security**: Authentication, input validation, security audits

### Development Setup

For contributors working on the codebase:

```sh
# Install development dependencies
sudo apt-get install -y valgrind gdb clang-format clang-tidy

# Format code (if clang-format is available)
find src -name "*.cpp" -o -name "*.h" | xargs clang-format -i

# Run static analysis
clang-tidy src/**/*.cpp -- -std=c++17

# Memory leak detection
valgrind --leak-check=full ./bin/server
```

### Reporting Issues

If you find a bug or have a feature request:

1. **Check existing issues** to avoid duplicates
2. **Provide detailed information**:
   - Description of the issue or feature
   - Steps to reproduce (for bugs)
   - Expected vs. actual behavior
   - System information (OS, compiler version, etc.)
   - Error messages or logs
3. **Use appropriate labels**: bug, enhancement, documentation, etc.

### Code of Conduct

- Be respectful and constructive
- Welcome newcomers and help them get started
- Focus on the code, not the person
- Accept constructive criticism gracefully
- Prioritize the community's best interests

### Recognition

Contributors will be:
- Listed in the project's contributors page
- Credited in release notes for significant contributions
- Invited to join the core team for sustained contributions

## License

This project is licensed under the **MIT License**. See the [LICENSE](LICENSE) file for complete details.

### MIT License Summary

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

**THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED.**

### Third-Party Licenses

This project uses several open-source libraries, each with their own licenses:
- **Crow**: BSD 3-Clause License
- **Eigen3**: MPL2 (Mozilla Public License 2.0)
- **SQLite**: Public Domain
- **Boost**: Boost Software License
- **cURL**: MIT/X derivate license

Please review the respective licenses when using or redistributing this software.

---

## Acknowledgments

Special thanks to:
- **Polygon.io** for providing comprehensive market data APIs
- The **Crow Framework** team for their excellent C++ web framework
- The **Eigen** project for high-performance linear algebra capabilities
- All contributors who have helped improve this project

---

## Contact & Support

- **GitHub Issues**: For bug reports and feature requests
- **Discussions**: For questions and general discussion
- **Email**: [Project maintainer email - if available]
- **Documentation**: [Link to wiki or docs site - if available]

---

<div align="center">

### 💡 Have ideas or feedback? 

**Open an issue or contribute to the project!** 🚀

[![GitHub stars](https://img.shields.io/github/stars/michaeletro/Derivative_Trading_Strategy?style=social)](https://github.com/michaeletro/Derivative_Trading_Strategy/starers)
[![GitHub forks](https://img.shields.io/github/forks/michaeletro/Derivative_Trading_Strategy?style=social)](https://github.com/michaeletro/Derivative_Trading_Strategy/network/members)
[![GitHub issues](https://img.shields.io/github/issues/michaeletro/Derivative_Trading_Strategy)](https://github.com/michaeletro/Derivative_Trading_Strategy/issues)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**Built with ❤️ by the quantitative trading community**

</div>

