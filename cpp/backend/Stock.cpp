
#include "Stock.h"

Stock::Stock(const std::string& stockName) : Asset(stockName) {}

void Stock::fetchOptionData(APIConnection& apiConnection) {
    auto data = apiConnection.sendRequest("/option_data");
    for (const auto& item : data["options"]) {
        std::string type = item["type"];
        double price = item["price"];
        optionData[type].push_back(price);
    }
}

void Stock::calculateVolatilitySurface() {
    // Implement implied volatility surface calculation (placeholder for now)
}
