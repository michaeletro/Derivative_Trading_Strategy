#include <iostream>
#include "../headers/AssetClasses/StockClass.h"
#include "../headers/AssetClasses/CryptoClass.h"
#include "../headers/AssetClasses/ForexClass.h"
#include "../headers/AssetClasses/OptionClass.h"
#include "../headers/AssetClasses/PortfolioClass.h"
#include "../headers/APIConnection/APIConnectionClass.h"
#include "../headers/DataBase/DataBaseClass.h"
#include "../headers/Utilities/server.h"

/*
void testAssetClasses() {
    std::cout << "🔍 Testing Asset Classes...\n";

    Stock stock("AAPL", 150.0);
    Crypto crypto("BTC", 42000.0);
    Forex forex("EUR/USD", 1.12);
    Option option("AAPL_20230630C150", 5.50);

    std::cout << "✅ Stock: " << stock.getTicker() << " - $" << stock.getPrice() << std::endl;
    std::cout << "✅ Crypto: " << crypto.getTicker() << " - $" << crypto.getPrice() << std::endl;
    std::cout << "✅ Forex: " << forex.getTicker() << " - $" << forex.getExchangeRate() << std::endl;
    std::cout << "✅ Option: " << option.getTicker() << " - $" << option.getPrice() << std::endl;
}

void testPortfolioManagement() {
    std::cout << "\n📊 Testing Portfolio Management...\n";

    PortfolioClass portfolio;
    portfolio.addAsset("AAPL", 10);
    portfolio.addAsset("BTC", 2);

    std::cout << "✅ Portfolio Total Value: $" << portfolio.calculateTotalValue() << std::endl;
    portfolio.displayPortfolio();
}

void testDatabase() {
    std::cout << "\n💾 Testing Database Connection...\n";

    DataBaseClass db;
    if (db.connect("trading_db.sqlite")) {
        std::cout << "✅ Database connected successfully!\n";
        db.fetchAllAssets();
    } else {
        std::cerr << "❌ Database connection failed!\n";
    }
}

void testAPIConnection() {
    std::cout << "\n🌐 Testing API Connection...\n";

    APIConnectionClass api;
    std::string response = api.getAssetData("AAPL");
    std::cout << "✅ API Response for AAPL: " << response << std::endl;
}

void startServer() {
    std::cout << "\n🚀 Starting Trading Strategy API Server...\n";
    startCrowServer();
}
*/

int main() {
    std::cout << "\n===== 🏦 Derivative Trading Strategy API Tests =====\n";

    // Uncomment tests as implementations are completed.
    //testAssetClasses();
    //testPortfolioManagement();
    //testDatabase();
    //testAPIConnection();

    // Start minimal HTTP server (health + echo routes).
    startServer();
    return 0;
}
// End of main.cpp
// End of file