#include <iostream>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <vector>
#include <string>
#include "StockClass.h"
#include "OptionClass.h"

namespace fs = std::filesystem;

std::vector<std::vector<std::string>> readCSV(const std::string& filename) {
    std::ifstream file(filename);
    if (!file.is_open()) {
        throw std::runtime_error("Could not open file: " + filename);
    }

    std::vector<std::vector<std::string>> data;
    std::string line;

    while (std::getline(file, line)) {
        std::vector<std::string> row;
        std::istringstream stream(line);
        std::string cell;

        while (std::getline(stream, cell, ',')) {
            row.push_back(cell);
        }
        data.push_back(row);
    }

    file.close();
    return data;
}

int main() {

    std::string api_key = readCSV("./data_files/access_key.csv")[0][0];

    try {

        // Test StockClass
        StockClass stock("AAPL", "2024-01-01", "2025-01-10", "1", 
                         "day", "asc", api_key, 10000, true, true);

        stock.fetchStockData();
        stock.writeToCSV("./data_files/stock_data.csv");

        // Test OptionClass
        /*
        OptionClass option("AAPL", "2024-01-01", "2025-01-10", "100",
                           "hour", "asc", api_key, 5000, true, true);

        option.fetchOptionData();
        option.writeToCSV("option_data.csv");
        */

    } catch (const std::exception& ex) {
        std::cerr << "Error: " << ex.what() << std::endl;
    }

    return 0;
}
