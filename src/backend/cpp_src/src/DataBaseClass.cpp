#include "DatabaseClass.h"
#include <iostream>
#include <sstream>

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


DatabaseClass::DatabaseClass(const std::string& db_path) {
    if (sqlite3_open(db_path.c_str(), &db) != SQLITE_OK) {
        std::cerr << "Error opening database: " << sqlite3_errmsg(db) << std::endl;
        db = nullptr;
    }
}

DatabaseClass::~DatabaseClass() {
    if (db) {
        sqlite3_close(db);
    }
}

std::vector<AssetData> DatabaseClass::fetchAssetData() {
    std::vector<AssetData> results;
    std::string query = "SELECT * FROM asset_data;";
    sqlite3_stmt* stmt;

    if (sqlite3_prepare_v2(db, query.c_str(), -1, &stmt, nullptr) == SQLITE_OK) {
        while (sqlite3_step(stmt) == SQLITE_ROW) {
            AssetData data;
            data.id = sqlite3_column_int(stmt, 0);
            data.ticker = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 1));
            data.date = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 2));
            data.open_price = sqlite3_column_double(stmt, 3);
            data.close_price = sqlite3_column_double(stmt, 4);
            data.high_price = sqlite3_column_double(stmt, 5);
            data.low_price = sqlite3_column_double(stmt, 6);
            data.volume = sqlite3_column_double(stmt, 7);
            results.push_back(data);
        }
        sqlite3_finalize(stmt);
    } else {
        std::cerr << "Error executing query: " << sqlite3_errmsg(db) << std::endl;
    }

    return results;
}

bool DatabaseClass::addAssetData(const std::string& ticker, const std::string& date, double open_price, double close_price,
                                 double high_price, double low_price, double volume) {
    std::ostringstream oss;
    oss << "INSERT INTO asset_data (ticker, date, open_price, close_price, high_price, low_price, volume) "
        << "VALUES ('" << ticker << "', '" << date << "', " << open_price << ", " << close_price << ", "
        << high_price << ", " << low_price << ", " << volume << ");";
    return executeQuery(oss.str());
}

bool DatabaseClass::executeQuery(const std::string& query) {
    char* errMsg = nullptr;
    if (sqlite3_exec(db, query.c_str(), nullptr, nullptr, &errMsg) != SQLITE_OK) {
        std::cerr << "SQL error: " << errMsg << std::endl;
        sqlite3_free(errMsg);
        return false;
    }
    return true;
}
