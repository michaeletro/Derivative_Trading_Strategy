#ifndef DATABASECLASS_H
#define DATABASECLASS_H

#include <string>
#include <vector>
#include <sqlite3.h>

// Define the AssetData structure
struct AssetData {
    int id;
    std::string ticker;
    std::string date;
    double open_price;
    double close_price;
    double high_price;
    double low_price;
    double volume;
};

class DatabaseClass {
public:
    DatabaseClass(const std::string& db_path);
    ~DatabaseClass();

    // Fetch asset data from the database
    std::vector<AssetData> fetchAssetData();

    // Add asset data to the database
    bool addAssetData(const std::string& ticker, const std::string& date, double open_price, double close_price,
                      double high_price, double low_price, double volume);

private:
    sqlite3* db;
    bool executeQuery(const std::string& query);
};

#endif // DATABASECLASS_H
