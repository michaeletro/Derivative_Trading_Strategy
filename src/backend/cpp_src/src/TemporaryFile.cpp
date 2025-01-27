#include "DatabaseClass.h"
#include <iostream>
#include <stdexcept>

DatabaseClass::DatabaseClass(const std::string& dbFile) {
    if (sqlite3_open(dbFile.c_str(), &db) != SQLITE_OK) {
        throw std::runtime_error("Failed to open database");
    }
}

DatabaseClass::~DatabaseClass() {
    sqlite3_close(db);
}

void DatabaseClass::executeSQL(const std::string& sql) {
    char* errMsg = nullptr;
    if (sqlite3_exec(db, sql.c_str(), nullptr, nullptr, &errMsg) != SQLITE_OK) {
        std::string error = errMsg;
        sqlite3_free(errMsg);
        throw std::runtime_error("SQL Error: " + error);
    }
}

void DatabaseClass::insertAssetData(const std::string& ticker, const std::string& date, double open, double close, double high, double low, double volume) {
    std::string sql = "INSERT INTO asset_data (ticker, date, open_price, close_price, high_price, low_price, volume) VALUES ('" +
                      ticker + "', '" + date + "', " + std::to_string(open) + ", " + std::to_string(close) + ", " +
                      std::to_string(high) + ", " + std::to_string(low) + ", " + std::to_string(volume) + ");";
    executeSQL(sql);
}

std::vector<std::vector<std::string>> DatabaseClass::queryAssetData(const std::string& ticker) {
    std::string sql = "SELECT * FROM asset_data WHERE ticker = '" + ticker + "';";
    sqlite3_stmt* stmt;
    std::vector<std::vector<std::string>> results;

    if (sqlite3_prepare_v2(db, sql.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
        throw std::runtime_error("Failed to prepare statement");
    }

    while (sqlite3_step(stmt) == SQLITE_ROW) {
        std::vector<std::string> row;
        for (int i = 0; i < sqlite3_column_count(stmt); i++) {
            const char* colText = reinterpret_cast<const char*>(sqlite3_column_text(stmt, i));
            row.push_back(colText ? colText : "NULL");
        }
        results.push_back(row);
    }
    sqlite3_finalize(stmt);
    return results;
}
