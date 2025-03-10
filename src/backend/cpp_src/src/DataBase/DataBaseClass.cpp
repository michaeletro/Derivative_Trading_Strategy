#include "../../headers/DataBase/DataBaseClass.h"
#include <stdexcept>
#include <iostream>
#include <sstream>
#include <fstream>
#include <sys/stat.h> // For file existence check

// Check if a file exists
bool DataBaseClass::fileExists(const std::string& filename) {
    struct stat buffer;
    return (stat(filename.c_str(), &buffer) == 0);
}

// Constructor
DataBaseClass::DataBaseClass(const std::string& db_path, const std::string& csv_backup) 
    : dbPath(db_path), csvPath(csv_backup) {

    bool dbExists = fileExists(db_path);

    if (sqlite3_open(dbPath.c_str(), &db) != SQLITE_OK) {
        std::cerr << "❌ Error opening database: " << sqlite3_errmsg(db) << std::endl;
        db = nullptr;
        return;
    }

    // Create table if it doesn't exist
    executeQuery("CREATE TABLE IF NOT EXISTS asset_data (id INTEGER PRIMARY KEY, ticker TEXT, date TEXT, open_price REAL, close_price REAL, high_price REAL, low_price REAL, volume REAL);");

    if (!dbExists || isDatabaseEmpty()) {
        std::cout << "🔄 Restoring database from CSV: " << csv_backup << std::endl;
        importFromCSV(csv_backup);
    } else {
        std::cout << "✅ Database exists and is not empty. No need to restore." << std::endl;
    }
}

// Destructor
DataBaseClass::~DataBaseClass() {
    if (db) {
        std::cout << "📂 Saving database to CSV before shutdown..." << std::endl;
        exportToCSV(csvPath);
        sqlite3_close(db);
        std::cout << "✅ Database successfully saved to CSV." << std::endl;
    }
}

bool DataBaseClass::isDatabaseEmpty() {
    std::string query = "SELECT COUNT(*) FROM asset_data;";
    sqlite3_stmt* stmt;

    if (sqlite3_prepare_v2(db, query.c_str(), -1, &stmt, nullptr) == SQLITE_OK) {
        if (sqlite3_step(stmt) == SQLITE_ROW) {
            int count = sqlite3_column_int(stmt, 0);
            sqlite3_finalize(stmt);
            return (count == 0);
        }
    }
    sqlite3_finalize(stmt);
    return true;
}

void DataBaseClass::exportToCSV(const std::string& csvFile) {
    std::ofstream file(csvFile);
    if (!file.is_open()) {
        std::cerr << "❌ Error opening CSV file for writing!" << std::endl;
        return;
    }

    file << "ticker,date,open_price,close_price,high_price,low_price,volume\n";

    std::string query = "SELECT ticker, date, open_price, close_price, high_price, low_price, volume FROM asset_data;";
    sqlite3_stmt* stmt;

    if (sqlite3_prepare_v2(db, query.c_str(), -1, &stmt, nullptr) == SQLITE_OK) {
        while (sqlite3_step(stmt) == SQLITE_ROW) {
            file << sqlite3_column_text(stmt, 0) << ","
                 << sqlite3_column_text(stmt, 1) << ","
                 << sqlite3_column_double(stmt, 2) << ","
                 << sqlite3_column_double(stmt, 3) << ","
                 << sqlite3_column_double(stmt, 4) << ","
                 << sqlite3_column_double(stmt, 5) << ","
                 << sqlite3_column_double(stmt, 6) << "\n";
        }
        sqlite3_finalize(stmt);
    } else {
        std::cerr << "❌ Error exporting to CSV: " << sqlite3_errmsg(db) << std::endl;
    }

    file.close();
    std::cout << "✅ Database exported to CSV successfully!" << std::endl;
}


void DataBaseClass::importFromCSV(const std::string& csvFile) {
    std::ifstream file(csvFile);
    if (!file.is_open()) {
        std::cerr << "❌ Error opening CSV file for reading!" << std::endl;
        return;
    }

    std::string line;
    std::getline(file, line);  // Skip header line

    while (std::getline(file, line)) {
        std::stringstream ss(line);
        std::string ticker, date, open, close, high, low, volume;

        std::getline(ss, ticker, ',');
        std::getline(ss, date, ',');
        std::getline(ss, open, ',');
        std::getline(ss, close, ',');
        std::getline(ss, high, ',');
        std::getline(ss, low, ',');
        std::getline(ss, volume, ',');

        std::string query = "INSERT INTO asset_data (ticker, date, open_price, close_price, high_price, low_price, volume) "
                            "VALUES ('" + ticker + "', '" + date + "', " + open + ", " + close + ", " + high + ", " + low + ", " + volume + ");";
        executeQuery(query);
    }

    file.close();
    std::cout << "✅ Database restored from CSV successfully!" << std::endl;
}


std::vector<AssetData> DataBaseClass::fetchAssetData() {
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

bool DataBaseClass::addAssetData(const std::string& ticker, const std::string& date, double open_price, double close_price,
                                 double high_price, double low_price, double volume) {
    std::ostringstream oss;
    oss << "INSERT INTO asset_data (ticker, date, open_price, close_price, high_price, low_price, volume) "
        << "VALUES ('" << ticker << "', '" << date << "', " << open_price << ", " << close_price << ", "
        << high_price << ", " << low_price << ", " << volume << ");";
    return executeQuery(oss.str());
}

bool DataBaseClass::executeQuery(const std::string& query) {
    char* errMsg = nullptr;
    if (sqlite3_exec(db, query.c_str(), nullptr, nullptr, &errMsg) != SQLITE_OK) {
        std::cerr << "SQL error: " << errMsg << std::endl;
        sqlite3_free(errMsg);
        return false;
    }
    return true;
}

void DataBaseClass::executeSQL(const std::string& sql) {
    char* errMsg = nullptr;
    if (sqlite3_exec(db, sql.c_str(), nullptr, nullptr, &errMsg) != SQLITE_OK) {
        std::string error = errMsg;
        sqlite3_free(errMsg);
        throw std::runtime_error("SQL Error: " + error);
    }
}

std::vector<std::vector<std::string>> DataBaseClass::queryAssetData(const std::string& ticker) {
    std::vector<std::vector<std::string>> results;  // ✅ Declare results at the start
    std::string sql = "SELECT * FROM asset_data WHERE ticker = ?;";
    sqlite3_stmt* stmt;

    if (sqlite3_prepare_v2(db, sql.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
        std::cerr << "❌ Failed to prepare statement: " << sqlite3_errmsg(db) << std::endl;
        return results;  // Return empty vector on failure
    }

    // ✅ Bind the ticker value
    sqlite3_bind_text(stmt, 1, ticker.c_str(), -1, SQLITE_STATIC);

    // ✅ Execute and fetch results
    while (sqlite3_step(stmt) == SQLITE_ROW) {
        std::vector<std::string> row;
        for (int i = 0; i < sqlite3_column_count(stmt); i++) {
            const char* col_text = reinterpret_cast<const char*>(sqlite3_column_text(stmt, i));
            row.push_back(col_text ? col_text : "NULL");  // Handle NULL values
        }
        results.push_back(row);  // ✅ Ensure results is available
    }

    sqlite3_finalize(stmt);
    return results;  // ✅ Return results at the end
}

std::vector<std::vector<std::string>> DataBaseClass::queryAssetData(const std::string& ticker, const std::string& startDate,
                                          const std::string& endDate, int limit = 1000, bool ascending = true){
    std::vector<std::vector<std::string>> results;
    std::string sql = "SELECT * FROM asset_data WHERE ticker = ? AND date BETWEEN ? AND ? "
                      "ORDER BY date " + std::string(ascending ? "ASC" : "DESC") + " LIMIT ?;";
    sqlite3_stmt* stmt;
    if (sqlite3_prepare_v2(db, query.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
        std::cerr << "❌ Failed to prepare statement: " << sqlite3_errmsg(db) << std::endl;
        return results;
    }
    sqlite3_bind_text(stmt, 1, ticker.c_str(), -1, SQLITE_STATIC);
    sqlite3_bind_test(stmt, 2, startDate.c_str(), -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 3, endDate.c_str(), -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 4, limit);

    while(sqlite3_step(stmt) == SQLITE_ROW) {
        AssetData data;
        data.ticker= reinterpret_cast<const char*>(sqlite3_column_text(stmt, 1));;
        data.date = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 2));
        data.open_price = sqlite3_column_double(stmt, 3);
        data.close_price = sqlite3_column_double(stmt, 4);
        data.high_price = sqlite3_column_double(stmt, 5);
        data.low_price = sqlite3_column_double(stmt, 6);
        data.volume = sqlite3_column_douuble(stmt, 7);
        results.push_back(data);
    }
    sqlite3_finalize(stmt);
    return results;
}