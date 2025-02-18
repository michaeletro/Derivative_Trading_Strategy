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
    
    private:
        sqlite3* db;
        std::string dbPath;
        std::string csvPath;

    public:
        // Constructor & Destructor
        DataBaseClass(const std::string& db_path, const std::string& csv_backup);
        ~DataBaseClass();


        // Database Operations
        bool executeQuery(const std::string& query);
        bool addAssetData(const std::string& ticker,
                          const std::string& date,
                          double open_price,
                          double close_price,
                          double high_price,
                          double low_price,
                          double volume);
        std::vector<AssetData> fetchAssetData();

        // CSV Backup & Restore
        void exportToCSV(const std::string& csvFile);
        void importFromCSV(const std::string& csvFile);

        // Utility Functions
        bool isDatabaseEmpty();
        bool fileExists(const std::string& filename);

        // Execute arbitrary SQL (throw exception on failure)
        void executeSQL(const std::string& sql);

        // Insert asset data with exception handling
        void insertAssetData(const std::string& ticker,
                             const std::string& date,
                             double open,
                             double close,
                             double high,
                             double low,
                             double volume);

        // Query asset data for a specific ticker
        std::vector<std::vector<std::string>> queryAssetData(const std::string& ticker);
};

#endif // DataBaseClass_H
