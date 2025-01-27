#ifndef DATABASECLASS_H
#define DATABASECLASS_H

#include <sqlite3.h>
#include <string>
#include <vector>

class DatabaseClass {
public:
    DatabaseClass(const std::string& dbFile);
    ~DatabaseClass();
    void insertAssetData(const std::string& ticker, const std::string& date, double open, double close, double high, double low, double volume);
    std::vector<std::vector<std::string>> queryAssetData(const std::string& ticker);

private:
    sqlite3* db;
    void executeSQL(const std::string& sql);
};

#endif
