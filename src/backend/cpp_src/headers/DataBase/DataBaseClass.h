#ifndef DATABASECLASS_H
#define DATABASECLASS_H

#include <sqlite3.h>
#include <string>
#include <vector>
#include "../DataStructs/DataStructs.h"

class DataBaseClass {
private:
    sqlite3* db;
    std::string dbPath;
    std::string csvPath;

    // 🔹 Utility Functions
    bool fileExists(const std::string& filename);
    bool isDatabaseEmpty();
    bool executeQuery(const std::string& query);
    void executeSQL(const std::string& sql);
    
public:
    // 🔹 Constructor & Destructor
    DataBaseClass(const std::string& db_path, const std::string& csv_backup);
    ~DataBaseClass();

    // 🔹 Database Operations
    bool createTable();
    bool addAssetData(const AssetData& asset);
    bool insertAssetData(const std::vector<AssetData>& assets);
    bool deleteAssetData(const std::string& ticker, const std::string& date);
    std::vector<AssetData> fetchAssetData();
    //std::vector<AssetData> queryAssetData(const std::string& ticker);
    std::vector<AssetData> queryAssetData(const std::string& ticker,
                                          const std::string& startDate,
                                          const std::string& endDate, 
                                          int limit = 1000, 
                                          bool ascending = true) const;

    // 🔹 CSV Backup & Restore
    void exportToCSV(const std::string& csvFile);
    void importFromCSV(const std::string& csvFile);
};

#endif // DATABASECLASS_H
