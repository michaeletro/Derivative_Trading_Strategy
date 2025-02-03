#ifndef DataBaseClass_H
#define DataBaseClass_H

#include <sqlite3.h>
#include <string>
#include <vector>

// Struct to store asset data
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

class DataBaseClass {
public:
    // Constructor & Destructor
    explicit DataBaseClass(const std::string& db_path);
    ~DataBaseClass();

    // Fetch all asset data from the database
    std::vector<AssetData> fetchAssetData();

    // Insert new asset data into the database
    bool addAssetData(const std::string& ticker, const std::string& date, double open_price, double close_price,
                      double high_price, double low_price, double volume);

    // Execute a raw SQL query (used internally)
    bool executeQuery(const std::string& query);

    // Execute arbitrary SQL (throw exception on failure)
    void executeSQL(const std::string& sql);

    // Insert asset data with exception handling
    void insertAssetData(const std::string& ticker, const std::string& date, double open, double close, double high, double low, double volume);

    // Query asset data for a specific ticker
    std::vector<std::vector<std::string>> queryAssetData(const std::string& ticker);

private:
    sqlite3* db;
};

#endif // DataBaseClass_H
