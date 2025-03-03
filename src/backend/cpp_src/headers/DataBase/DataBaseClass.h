#ifndef DataBaseClass_H
#define DataBaseClass_H

#include <sqlite3.h>
#include <string>
#include <vector>
#include "DataStructs/DataStructs.h"

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
        bool createTable();
        bool executeQuery(const std::string& query);
        bool addAssetData(const std::string& ticker,
                          const std::string& date,
                          double open_price,
                          double close_price,
                          double high_price,
                          double low_price,
                          double volume);
        bool insertAssetData(const std::vector<AssetData>& assets);
        bool deleteAssetData(const std::string& ticker, const std::string& date);

        // 
        std::vector<AssetData> fetchAssetData();
        std::vector<AssetData> queryAssetData(const std::string& ticker);

        // CSV Backup & Restore
        void exportToCSV(const std::string& csvFile);
        void importFromCSV(const std::string& csvFile);

        // Utility Functions
        bool isDatabaseEmpty();
        bool fileExists(const std::string& filename);

        // Execute arbitrary SQL (throw exception on failure)
        void executeSQL(const std::string& sql);

};

#endif // DataBaseClass_H
