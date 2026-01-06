# Derivative Trading Strategy Framework

## Overview

A **quantitative finance framework** for financial asset modeling, market data retrieval, and portfolio management. The project implements object-oriented financial instruments in both **C++17** and **Python**, providing a foundation for algorithmic trading research and quantitative analysis.

This framework demonstrates core software engineering principles applied to quantitative finance:
- **Object-Oriented Design**: Asset class hierarchies with inheritance and polymorphism
- **Data Integration**: Polygon.io API connectivity for real-time and historical market data
- **Persistent Storage**: SQLite database for efficient local data management
- **Dual Implementation**: Parallel C++ and Python codebases for performance and prototyping

## Project Status

**Current State**: Early-stage development framework with foundational components

This is an educational and research project showcasing financial software architecture. The codebase provides working implementations of core financial data structures and market data retrieval, suitable for:
- Learning quantitative finance programming
- Academic research and experimentation
- Building custom trading strategy prototypes
- Portfolio tracking and analysis

## Core Features

### C++ Implementation

**Asset Class Hierarchy**
- Base `Asset` class with polymorphic design
- Specialized classes: `Stock`, `Crypto`, `Forex`, `Option`
- Time series containers for historical price data
- Basic portfolio composition using asset collections

**Market Data Integration**
- Polygon.io API client implementation
- URL generation and parameter validation
- JSON parsing using RapidJSON
- HTTP requests via libcurl

**Data Persistence**
- SQLite3 database integration
- Asset data storage and retrieval
- Query interface with date range filtering
- CSV export/import capabilities

**REST API (In Development)**
- Crow C++ web framework integration
- Basic CRUD endpoints for asset data
- Database query routes

### Python Implementation

**Financial Instruments**
- Object-oriented asset classes (`Asset_Class` base)
- `Stock_Class`, `Crypto_Class`, `Forex_Class`, `Option_Class`, `Cash_Class`, `Index_Class`
- Price vector management and cumulative value calculations
- Plotly integration for visualization

**Portfolio Management**
- `Portfolio_Class` for multi-asset portfolios
- Asset aggregation and combined value computation
- Portfolio composition tracking
- Return visualization capabilities

**Utilities**
- API connection utilities
- Error handling framework
- Resource management utilities

## Technical Architecture

### Technology Stack

#### C++ Backend
- **Language**: C++17
- **Web Framework**: Crow (header-only, lightweight HTTP server)
- **Database**: SQLite3 (embedded relational database)
- **HTTP Client**: libcurl
- **JSON Parsing**: RapidJSON
- **Linear Algebra**: Eigen3 (included for future quantitative computations)
- **Build System**: GNU Make with automatic dependency detection

#### Python Backend
- **Language**: Python 3.x
- **Data Processing**: NumPy, Pandas
- **Visualization**: Plotly
- **Data Fetching**: Custom API connection utilities

### Project Structure

```
Derivative_Trading_Strategy/
├── src/
│   ├── backend/
│   │   ├── cpp_src/                    # C++ implementation
│   │   │   ├── headers/                # Header files
│   │   │   │   ├── AssetClasses/       # Stock, Crypto, Forex, Option, Portfolio
│   │   │   │   ├── TimeSeriesClasses/  # Time series containers
│   │   │   │   ├── APIConnection/      # Polygon API client
│   │   │   │   ├── DataBase/           # SQLite interface
│   │   │   │   ├── CrowRouteHandling/  # REST API routes
│   │   │   │   ├── DataStructs/        # Core data structures
│   │   │   │   └── Utilities/          # Helper functions
│   │   │   ├── src/                    # Implementation files
│   │   │   └── Makefile
│   │   └── Arch/
│   │       └── python_src/             # Python implementation
│   │           ├── Financial_Products/ # Asset and Portfolio classes
│   │           ├── Utilities/          # API and utility functions
│   │           └── Error_Handling/     # Custom exceptions
│   └── frontend/                        # Web interface (minimal)
│       ├── HTML/
│       ├── CSS/
│       └── JavaScript/
├── data_files/                          # Data storage
├── README.md
└── LICENSE
```

### Design Patterns

**Inheritance and Polymorphism**
- Base `Asset` class defines common interface
- Derived classes (`Stock`, `Crypto`, `Forex`, `Option`) implement asset-specific behavior
- Virtual functions enable polymorphic data handling
- Template-based time series containers for type safety

**Data Flow**
1. API client constructs HTTP requests to Polygon.io
2. JSON responses parsed and validated
3. Data transformed into asset objects
4. Assets stored in SQLite database
5. Query interface retrieves data for analysis
6. Portfolio objects aggregate multiple assets

## Installation & Setup

### Prerequisites

**C++ Build Environment**
- C++ compiler with C++17 support (GCC 7.0+, Clang 5.0+)
- GNU Make
- CMake (optional)

**Required Libraries**

Ubuntu/Debian:
```bash
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

macOS (using Homebrew):
```bash
brew install sqlite eigen curl openssl boost
```

**Python Environment**
```bash
pip install numpy pandas plotly
```

### Building the C++ Project

1. **Clone the repository**
```bash
git clone https://github.com/michaeletro/Derivative_Trading_Strategy.git
cd Derivative_Trading_Strategy
```

2. **Navigate to C++ source directory**
```bash
cd src/backend/cpp_src
```

3. **Build the project**
```bash
make clean
make
```

The executable will be generated as `bin/server`.

4. **Run the program** (when implemented)
```bash
./bin/server
```

### Configuration

**API Keys**

To use Polygon.io market data:
1. Sign up for a free account at [polygon.io](https://polygon.io)
2. Obtain your API key
3. Set as environment variable:
```bash
export POLYGON_API_KEY="your_api_key_here"
```

Or pass directly in code via the `QueryData` structure.

### Python Usage

```bash
cd src/backend/Arch/python_src
python3
```

```python
from Financial_Products import Stock_Class, Portfolio_Class

# Create stock instance
stock = Stock_Class(ticker="AAPL", date="2024-01-15")

# Create portfolio
portfolio = Portfolio_Class()
portfolio.add_to_portfolio(stock)
```

## Current Implementation Details

### What's Working

**C++ Components**
- ✅ Asset class hierarchy with inheritance
- ✅ Polygon.io API connection and data fetching
- ✅ SQLite database operations (create, insert, query, delete)
- ✅ JSON parsing and HTTP request handling
- ✅ Time series data structures
- ✅ CSV export functionality
- ✅ Basic Crow web framework integration

**Python Components**
- ✅ Asset class implementations
- ✅ Portfolio composition and tracking
- ✅ Cumulative value calculations
- ✅ Plotly visualization for returns
- ✅ API connection utilities
- ✅ Error handling framework

### In Development

- 🔨 REST API route implementation
- 🔨 Complete CRUD operations via HTTP endpoints
- 🔨 Frontend web interface
- 🔨 Integration between C++ and Python components

### Not Yet Implemented

The following features are **planned** but not currently available:
- ❌ Portfolio optimization algorithms (mean-variance, efficient frontier)
- ❌ Risk metrics (VaR, Sharpe ratio, beta, correlation analysis)
- ❌ Backtesting framework
- ❌ Trading strategy implementations
- ❌ Real-time WebSocket data streaming
- ❌ Advanced quantitative models (ARIMA, GARCH, ML models)
- ❌ Broker integration for live trading
- ❌ Authentication and user management
- ❌ Advanced portfolio analytics and attribution

## Use Cases

**Educational**
- Learn object-oriented design patterns in quantitative finance
- Understand financial data API integration
- Study C++ and Python implementation differences
- Practice database design for financial applications

**Research & Prototyping**
- Build custom asset pricing models
- Develop proprietary trading strategies
- Experiment with portfolio construction algorithms
- Test quantitative finance hypotheses

**Personal Portfolio Tracking**
- Track personal investment holdings
- Retrieve historical price data
- Visualize portfolio composition
- Calculate basic returns and valuations

## Code Examples

### C++ Example: Fetching Asset Data

```cpp
#include "headers/APIConnection/APIConnectionClass.h"
#include "headers/DataStructs/DataStructs.h"

int main() {
    // Configure query parameters
    QueryData query;
    query.asset_name = "AAPL";
    query.multiplier = 1;
    query.time_span = "day";
    query.start_date = "2024-01-01";
    query.end_date = "2024-01-31";
    query.api_key = "YOUR_API_KEY";
    query.debug = true;

    // Fetch data from Polygon API
    APIConnection api(query);
    auto response = api.fetchAPIData();
    
    // Parse and process results
    // ... (implementation details)
    
    return 0;
}
```

### Python Example: Portfolio Management

```python
from Financial_Products import Stock_Class, Portfolio_Class

# Create individual stock positions
aapl = Stock_Class(ticker="AAPL", date="2024-01-15")
googl = Stock_Class(ticker="GOOGL", date="2024-01-15")

# Initialize portfolio
portfolio = Portfolio_Class(
    stock_position=[aapl, googl]
)

# Calculate combined value
values = portfolio.generate_combined_value()
print(f"Total Stock Value: ${values['Stock']:.2f}")

# Visualize portfolio
portfolio.generate_portfolio_returns()
```

## Database Schema

The system uses SQLite3 for data persistence with the following structure:

### Assets Table

Stores historical price data for all asset types.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| ticker | TEXT | Asset ticker symbol |
| asset_type | TEXT | Type: stock, crypto, forex, option, index |
| date | TEXT | Date in ISO format |
| open_price | REAL | Opening price |
| close_price | REAL | Closing price |
| high_price | REAL | Day's high price |
| low_price | REAL | Day's low price |
| volume | REAL | Trading volume |

**Indexes:**
- `idx_ticker_date` on (ticker, date)
- `idx_asset_type` on (asset_type)

### Sample Queries

```sql
-- Fetch historical data for a ticker
SELECT * FROM assets 
WHERE ticker = 'AAPL' 
  AND date BETWEEN '2024-01-01' AND '2024-01-31'
ORDER BY date ASC;

-- Get latest price for all stocks
SELECT ticker, close_price, date
FROM assets
WHERE asset_type = 'stock'
  AND date = (SELECT MAX(date) FROM assets WHERE ticker = assets.ticker);
```

## Development Roadmap

### Phase 1: Foundation (Current)
- ✅ Core asset class hierarchy
- ✅ API integration with Polygon.io
- ✅ Database operations
- ✅ Basic portfolio composition
- 🔨 Complete REST API implementation
- 🔨 Integration testing

### Phase 2: Quantitative Analytics (Planned)
- Portfolio optimization (mean-variance, efficient frontier)
- Risk metrics (VaR, CVaR, Sharpe ratio, Sortino ratio, beta)
- Correlation and covariance matrix calculations
- Performance attribution analysis
- Drawdown analysis

### Phase 3: Trading Strategies (Planned)
- Backtesting framework
- Strategy implementation interface
- Performance evaluation metrics
- Parameter optimization
- Walk-forward analysis

### Phase 4: Advanced Features (Future)
- Real-time data streaming (WebSocket)
- Machine learning integration
- Advanced risk models (GARCH, factor models)
- Broker integration for paper trading
- Web-based dashboard

### Phase 5: Production (Future)
- Authentication and authorization
- Multi-user support
- API rate limiting
- Comprehensive logging and monitoring
- Deployment automation

## Contributing

Contributions are welcome! This project is ideal for:
- Students learning quantitative finance and C++/Python
- Researchers prototyping new trading strategies
- Developers interested in financial software architecture

### How to Contribute

1. **Fork the repository**
2. **Create a feature branch** (`git checkout -b feature/your-feature`)
3. **Make your changes** with clear, documented code
4. **Test thoroughly** to ensure existing functionality works
5. **Commit your changes** (`git commit -m 'Add feature'`)
6. **Push to the branch** (`git push origin feature/your-feature`)
7. **Open a Pull Request**

### Areas for Contribution

- **Quantitative models**: Risk metrics, optimization algorithms, backtesting
- **Data sources**: Additional API integrations beyond Polygon.io
- **Asset classes**: New financial instruments and derivatives
- **Testing**: Unit tests, integration tests, test coverage
- **Documentation**: Code examples, tutorials, API documentation
- **Performance**: Optimization, profiling, benchmarking
- **Frontend**: Web dashboard for portfolio visualization

### Code Style

- **C++**: Follow C++17 best practices, use meaningful names, comment complex logic
- **Python**: PEP 8 style guide, type hints where appropriate
- **Documentation**: Add docstrings for public APIs
- **Testing**: Include tests for new functionality

## License

This project is licensed under the **MIT License**. See the [LICENSE](LICENSE) file for details.

### Third-Party Licenses

- **Crow**: BSD 3-Clause License
- **Eigen3**: MPL2 (Mozilla Public License 2.0)
- **SQLite**: Public Domain
- **Boost**: Boost Software License
- **RapidJSON**: MIT License
- **cURL**: MIT/X derivative license

## Acknowledgments

- **Polygon.io** for market data API
- **Crow Framework** for C++ web framework
- **Eigen** for linear algebra capabilities
- Open-source community for libraries and tools

## Contact

- **GitHub Issues**: For bug reports and feature requests
- **GitHub Discussions**: For questions and general discussion
- **Repository**: [michaeletro/Derivative_Trading_Strategy](https://github.com/michaeletro/Derivative_Trading_Strategy)

---

## Disclaimer

**This software is for educational and research purposes only.** 

- Not intended for live trading without extensive testing and validation
- No warranty of fitness for any particular purpose
- Past performance does not guarantee future results
- Trading involves substantial risk of loss
- Consult financial professionals before making investment decisions

The authors and contributors are not responsible for any financial losses incurred from using this software.

---

<div align="center">

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![C++17](https://img.shields.io/badge/C++-17-blue.svg)](https://isocpp.org/)
[![Python](https://img.shields.io/badge/Python-3.x-blue.svg)](https://www.python.org/)

**A quantitative finance framework for research and education**

</div>