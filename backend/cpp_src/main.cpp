#include <iostream>
#include <memory>
#include "StockClass.h"
#include "OptionClass.h"
#include "PortfolioClass.h"
#include <fstream>

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
    const std::string api_key = readCSV("../data_files/access_key.csv")[0][0];

    try {
        // Create a StockClass object
        auto stock = std::make_shared<StockClass>("MSFT", "2024-06-01", "2025-01-10", 
                                                  "100", "day", "asc", api_key, true, true);
        stock->fetchTimeSeriesData();

        // Fetch asset data
        //stock->fetchAssetData();

        // Print fetched data
        //stock->printTimeSeriesData();

        // Write data to a CSV file
        stock->createNewCSV("stock_sssssss.csv");

        // Create a PortfolioClass object and add the stock to the portfolio
        PortfolioClass portfolio;
        portfolio.addToPortfolio(stock, "Stock");

        // Print portfolio contents
        portfolio.printPortfolio();

    } catch (const std::exception& e) {
        std::cerr << "Error: " << e.what() << std::endl;
        return EXIT_FAILURE;
    }

    return EXIT_SUCCESS;
}
