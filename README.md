# Derivative Trading Strategy API

## Overview
Welcome to the **Derivative Trading Strategy API**, a high-performance, C++-based trading API powered by **Crow**, **SQLite**, and **Eigen3**. This API facilitates **asset management**, **portfolio optimization**, and **market data retrieval** with a focus on **financial engineering and quantitative strategies**. Future updates will introduce gamification elements to enhance user engagement.

## Features
- 📈 **Asset Data Retrieval** – Fetch stock, crypto, forex, and options data.
- 📊 **Portfolio Optimization** – Implement mean-variance optimization for portfolio balancing.
- 📉 **Risk Metrics Calculation** – Compute VaR, Sharpe Ratio, and Beta.
- 🔍 **Market Data Analysis** – Perform real-time and historical trend analysis.
- 📡 **API for Quantitative Research** – Connect with external data sources for modeling.
- 🔄 **Real-time Portfolio Updates** – Automatically adjust portfolio values based on market fluctuations.
- 📡 **Backtesting Framework** – Simulate strategies using historical data.
- 🎯 **Customizable Trading Strategies** – Implement and test different trading models.
- 🚀 **Future Gamification Features** – Leaderboards, competitions, and AI-driven trading challenges.

## Getting Started
### Prerequisites
Ensure you have the following dependencies installed:
- **C++17+**
- **SQLite3** (`libsqlite3-dev` for Linux/macOS)
- **Eigen3** (`libeigen3-dev` for Linux/macOS)
- **Crow Web Framework**
- **cURL** (`libcurl4-openssl-dev`)

### Installation
1. Clone the repository:
   ```sh
   git clone https://github.com/yourusername/Derivative_Trading_Strategy.git
   cd Derivative_Trading_Strategy/src/backend/cpp_src
   ```
2. Build the project:
   ```sh
   make
   ```
3. Run the server:
   ```sh
   ./bin/server
   ```
4. Access the API:
   - Open your browser or use `curl` to test routes (e.g., `http://localhost:8080/api/asset_data`).

## API Endpoints
### **1️⃣ Asset Data Retrieval**
#### **Fetch All Assets**
```http
GET /api/asset_data
```
#### **Fetch Asset by Ticker**
```http
GET /api/asset_data/{ticker}
```
Example:
```sh
curl http://localhost:8080/api/asset_data/AAPL
```

### **2️⃣ Portfolio Management & Optimization**
#### **Fetch User Portfolio**
```http
GET /api/user/{username}/portfolio
```
#### **Optimize Portfolio**
```http
POST /api/portfolio/optimize
```
**Payload:**
```json
{
  "username": "trader123",
  "optimization_type": "mean_variance"
}
```

### **3️⃣ Risk Management Metrics**
#### **Calculate Value-at-Risk (VaR)**
```http
GET /api/portfolio/{username}/var
```
#### **Retrieve Portfolio Beta**
```http
GET /api/portfolio/{username}/beta
```

### **4️⃣ Trading Strategy & Backtesting**
#### **Run Backtest**
```http
POST /api/backtest
```
**Payload:**
```json
{
  "username": "trader123",
  "strategy": "momentum",
  "start_date": "2023-01-01",
  "end_date": "2023-12-31"
}
```

## Database Structure
### **Tables**
#### `assets`
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| ticker | TEXT | Asset ticker |
| date | TEXT | Date of record |
| open_price | REAL | Opening price |
| close_price | REAL | Closing price |
| high_price | REAL | High price |
| low_price | REAL | Low price |
| volume | INTEGER | Trade volume |

#### `portfolios`
| Column | Type | Description |
|--------|------|-------------|
| user_id | INTEGER | Primary key |
| balance | REAL | Available cash |
| total_value | REAL | Portfolio value |

## Contributing
1. **Fork the repo** and clone your copy.
2. **Create a new branch** for your feature or bug fix.
   ```sh
   git checkout -b feature-new-api-endpoint
   ```
3. **Make changes** and **commit**.
   ```sh
   git commit -m "Added new endpoint for XYZ"
   ```
4. **Push your changes** and open a **pull request**.
   ```sh
   git push origin feature-new-api-endpoint
   ```

## Future Enhancements 🚀
- **Real-time WebSocket API** for live trade updates.
- **Machine Learning Models** for trade signal generation.
- **Multi-factor Risk Modeling** using Eigen decomposition.
- **Integration with Alpaca or Binance APIs** for live trading.
- **Gamification Features** like trading competitions, rewards, and rankings.

## License
This project is licensed under the **MIT License**. See `LICENSE` for details.

---
💡 **Have ideas or feedback?** Open an issue or contribute to the project! 🚀

