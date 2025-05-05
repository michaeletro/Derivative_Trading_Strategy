#ifndef DATABASECLASS_H
#define DATABASECLASS_H

#include <sqlite3.h>
#include <string>
#include <vector>
#include <memory>
#include "../DataStructs/DataStructs.h"

class DataBaseClass {
private:
    sqlite3* db;
    std::string dbPath;
    std::string asset_data_path;
    std::string stock_data_path;
    std::string option_data_path;

    // 🔹 Utility Functions
    bool fileExists(const std::string& filename);
    bool isDatabaseEmpty(const std::string& table_name) const;
    void executeSQL(const std::string& sql);
    
public:
    // 🔹 Constructor & Destructor
    DataBaseClass(const std::string& db_path,
         const std::string& asset_data_path,
         const std::string& stock_data_path,
         const std::string& option_data_path);
    DataBaseClass(const std::string& db_path);
    ~DataBaseClass();

    // 🔹 Database Operations
    bool createTables();
    bool addAssetData(const AssetData& asset);
    bool insertAssetData(const std::vector<AssetData>& assets);
    bool deleteAssetData(const std::string& ticker, const std::string& date);
    
    std::vector<std::unique_ptr<AssetData>> queryData(const std::string& table_name,
                                          const std::string& ticker,
                                          uint64_t startDate,
                                          uint64_t endDate, 
                                          int limit = 1000, 
                                          bool ascending = true) const;
    

    // 🔹 CSV Backup & Restore
    void exportToCSV(const std::string& csvFile,     
        [[maybe_unused]] const std::string& db_path, 
        [[maybe_unused]] const std::string& ticker,
        [[maybe_unused]] int limit,         
        [[maybe_unused]] const std::string& tableName,
        [[maybe_unused]] uint64_t date_start,
        [[maybe_unused]] uint64_t date_end);

    void importFromCSV(const std::string& csvFile,
        [[maybe_unused]] const std::string& table_type);
    
    void restoreFromCSV(const std::string& csvFile,
         const std::string& table_type);
};

#endif // DATABASECLASS_H
