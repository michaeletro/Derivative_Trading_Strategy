// DataBaseClass.cpp
// Author: Michael Perez
// Description: Implementation of SQLite-based database operations for asset data handling.

#include "../../headers/DataBase/DataBaseClass.h"
#include <stdexcept>
#include <iostream>
#include <sstream>
#include <fstream>
#include <filesystem>
#include <sys/stat.h>

/// @brief Checks if a file exists in the filesystem.
/// @param filename The full path to the file.
/// @return True if file exists, false otherwise.
bool DataBaseClass::fileExists(const std::string& filename) const {
    struct stat buffer;
    return (stat(filename.c_str(), &buffer) == 0);
}

/// @brief Constructs a new DataBaseClass object, initializing the SQLite DB.
/// @param db_path Path to SQLite database file.
/// @param csv_backup Path to CSV backup to restore if DB is empty or missing.
DataBaseClass::DataBaseClass(const std::string& db_path,
        const std::string& asset_backup,
        const std::string& stock_backup,
        const std::string& option_backup,
        bool create_if_missing,
        bool restore_from_csv) 
        : dbPath(db_path), 
            asset_data_path(asset_backup),
            stock_data_path(stock_backup),
            option_data_path(option_backup) {

    const bool existed = fileExists(dbPath);
    if (!existed && !create_if_missing) {
        std::cerr << "❌ Database file does not exist!" << std::endl;
        throw std::runtime_error("Database file does not exist!");
    }

    // Create parent directories if needed when allowed to create new DB.
    if (!existed && create_if_missing) {
        const std::filesystem::path dbP(dbPath);
        const auto parent = dbP.parent_path();
        if (!parent.empty()) {
            std::error_code ec;
            std::filesystem::create_directories(parent, ec);
            if (ec) {
                std::cerr << "❌ Failed to create DB directory: " << ec.message() << std::endl;
                throw std::runtime_error("Failed to create DB directory");
            }
        }
        std::cout << "📁 Creating database at " << dbPath << std::endl;
    }

    if (sqlite3_open(dbPath.c_str(), &db) != SQLITE_OK) {
        std::cerr << "❌ Error opening database: " << sqlite3_errmsg(db) << std::endl;
        throw std::runtime_error("Error opening database");
    }
    if (!createTables()) {
        std::cerr << "❌ Error creating tables!" << std::endl;
        throw std::runtime_error("Error creating tables");
    }

    if (db == nullptr) {
        std::cerr << "❌ Database connection is null!" << std::endl;
        throw std::runtime_error("Database connection is null");
    }
    if (sqlite3_exec(db, "PRAGMA foreign_keys = ON;", nullptr, nullptr, nullptr) != SQLITE_OK) {
        std::cerr << "❌ Error enabling foreign keys: " << sqlite3_errmsg(db) << std::endl;
        throw std::runtime_error("Error enabling foreign keys");
    }

    if (restore_from_csv) {
        if (isDatabaseEmpty("asset_data") && !asset_data_path.empty()) {
            std::cout << "📂 Restoring Asset database from CSV..." << std::endl;
            importFromCSV(asset_data_path, "asset_data");
            std::cout << "✅ Asset database restored from CSV successfully!" << std::endl;
        }
        if (isDatabaseEmpty("stock_data") && !stock_data_path.empty()) {
            std::cout << "📂 Restoring Stock database from CSV..." << std::endl;
            importFromCSV(stock_data_path, "stock_data");
            std::cout << "✅ Stock database restored from CSV successfully!" << std::endl;
        }
        if (isDatabaseEmpty("option_data") && !option_data_path.empty()) {
            std::cout << "📂 Restoring Option database from CSV..." << std::endl;
            importFromCSV(option_data_path, "option_data");
            std::cout << "✅ Option database restored from CSV successfully!" << std::endl;
        }
        if (!isDatabaseEmpty("asset_data") || !isDatabaseEmpty("stock_data") || !isDatabaseEmpty("option_data")) {
            std::cout << "✅ Database is already populated." << std::endl;
        }
    }

    std::cout << "✅ Database initialized successfully." << std::endl;
}

/// @brief Destructor that exports the current DB state to CSV and closes the DB.
DataBaseClass::~DataBaseClass() {
    if (db) {
        std::cout << "📂 Saving database to CSV before shutdown..." << std::endl;

        exportToCSV(asset_data_path, dbPath, "", 1000, "asset_data", "", "");
        exportToCSV(stock_data_path, dbPath, "", 1000, "stock_data", "", "");
        exportToCSV(option_data_path, dbPath, "", 1000, "option_data", "", "");

        std::cout << "✅ Database saved to CSV successfully!" << std::endl;
        if (sqlite3_close(db) != SQLITE_OK) {
            std::cerr << "❌ Error closing database: " << sqlite3_errmsg(db) << std::endl;
        } else {
            db = nullptr;
        }
        std::cout << "✅ Database closed successfully." << std::endl;
    }
}


/// @brief Checks whether the 'assets' table in the database is empty.
/// @return True if empty or error occurred, false otherwise.
bool DataBaseClass::isDatabaseEmpty(const std::string& table_name) {
    if (table_name.empty()) {
        std::cerr << "❌ Table name is empty!" << std::endl;
        return true;
    }
    if (table_name != "asset_data" && table_name != "stock_data" && table_name != "option_data") {
        std::cerr << "❌ Invalid table name: " << table_name << std::endl;
        return true;
    }
    if (!fileExists(dbPath)) {
        std::cerr << "❌ Database file does not exist!" << std::endl;
        return true;
    }
    if (!db) {
        std::cerr << "❌ Database connection is null!" << std::endl;
        return true;
    }

    std::ostringstream oss;
    oss << "SELECT COUNT(*) FROM " << table_name << ";";
    const auto query = oss.str();
    sqlite3_stmt* stmt;

    if (sqlite3_prepare_v2(db, query.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
        std::cerr << "❌ Error preparing SQL statement: " << sqlite3_errmsg(db) << std::endl;
        return true;
    }
    if (sqlite3_step(stmt) == SQLITE_ROW) {
        int count = sqlite3_column_int(stmt, 0);
        sqlite3_finalize(stmt);
        return (count == 0);
    }
    std::cerr << "❌ Error executing SQL statement: " << sqlite3_errmsg(db) << std::endl;
    sqlite3_finalize(stmt);
    return true;
}

/// @brief Imports asset data from a CSV file into the database. REFACTOR: This function is not used in the current implementation.
/// @param csvFile Path to the CSV file.
void DataBaseClass::importFromCSV(const std::string& csvFile, 
    [[maybe_unused]] const std::string& table_type) {
    std::ifstream file(csvFile);
    if (!file.is_open()) {
        std::cerr << "❌ Error opening CSV file for reading!" << std::endl;
        return;
    }

    std::string line;
    std::getline(file, line);  // Skip header line

    while (std::getline(file, line)) {
        std::stringstream ss(line);
        std::ostringstream query;

        AssetData asset;
        std::string token;

        std::getline(ss, asset.ticker, ',');
        std::getline(ss, token, ','); asset.open_price = std::stof(token);
        std::getline(ss, token, ','); asset.close_price = std::stof(token);
        std::getline(ss, token, ','); asset.high_price = std::stof(token);
        std::getline(ss, token, ','); asset.low_price = std::stof(token);
        std::getline(ss, token, ','); asset.volume = static_cast<uint64_t>(std::stoull(token));
        std::getline(ss, asset.date, ',');

        if (table_type == "stock_data") {
            StockData stock;
            static_cast<AssetData&>(stock) = asset;
            std::getline(ss, stock.stock_name, ',');
            std::getline(ss, stock.exchange, ',');
            std::getline(ss, stock.sector, ',');
            query << "INSERT INTO stock_data (ticker, open_price, close_price, high_price, low_price, volume, date, stock_name, exchange, sector) VALUES ('"
                  << stock.ticker << "', "
                  << stock.open_price << ", "
                  << stock.close_price << ", "
                  << stock.high_price << ", "
                  << stock.low_price << ", "
                  << stock.volume << ", '"
                  << stock.date << "', '"
                  << stock.stock_name << "', '"
                  << stock.exchange << "', '"
                  << stock.sector << "');";
            executeSQL(query.str());
        } else if (table_type == "option_data") {
            OptionData option;
            static_cast<AssetData&>(option) = asset;
            std::getline(ss, option.option_symbol, ',');
            std::getline(ss, option.option_type, ',');
            std::getline(ss, token, ','); option.expiry_date = std::stoull(token.empty() ? "0" : token);
            std::getline(ss, token, ','); option.strike_price = token.empty() ? 0.0F : std::stof(token);
            query << "INSERT INTO option_data (ticker, open_price, close_price, high_price, low_price, volume, date, option_symbol, option_type, expiry_date, strike_price) VALUES ('"
                  << option.ticker << "', "
                  << option.open_price << ", "
                  << option.close_price << ", "
                  << option.high_price << ", "
                  << option.low_price << ", "
                  << option.volume << ", '"
                  << option.date << "', '"
                  << option.option_symbol << "', '"
                  << option.option_type << "', "
                  << option.expiry_date << ", "
                  << option.strike_price << ");";
            executeSQL(query.str());
        } else {
            query << "INSERT INTO asset_data (ticker, open_price, close_price, high_price, low_price, volume, date) VALUES ('"
                  << asset.ticker << "', "
                  << asset.open_price << ", "
                  << asset.close_price << ", "
                  << asset.high_price << ", "
                  << asset.low_price << ", "
                  << asset.volume << ", '"
                  << asset.date << "');";
            executeSQL(query.str());
        }
    }

    file.close();
    std::cout << "✅ Database restored from CSV successfully!" << std::endl;
}


void DataBaseClass::exportToCSV(const std::string& csvFile, 
    [[maybe_unused]] const std::string& db_path,
    [[maybe_unused]] const std::string& ticker,
    [[maybe_unused]] int limit,
    [[maybe_unused]] const std::string& tableName,
    [[maybe_unused]] const std::string& date_start,
    [[maybe_unused]] const std::string& date_end) {
    std::ofstream file(csvFile);
    if (!file.is_open()) {
        std::cerr << "❌ Error opening CSV file for writing!" << std::endl;
        return;
    }
    std::vector<std::unique_ptr<AssetData>> results = queryData(tableName, ticker, date_start, date_end, limit, true);

    // 🧾 Write CSV header
    if (tableName == "asset_data") {
        file << "id,ticker,open_price,close_price,high_price,low_price,volume,date\n";
    } else if (tableName == "stock_data") {
        file << "id,ticker,open_price,close_price,high_price,low_price,volume,date,stock_name,exchange,sector\n";
    } else if (tableName == "option_data") {
        file << "id,ticker,open_price,close_price,high_price,low_price,volume,date,option_symbol,option_type,expiry_date,strike_price\n";
    } else {
        std::cerr << "❌ Invalid table name: " << tableName << std::endl;
        file.close();
        return;
    }

    for (const auto& row : results) {
        const AssetData* asset = row.get();
        file << asset->id << ","
             << asset->ticker << ","
             << asset->open_price << ","
             << asset->close_price << ","
             << asset->high_price << ","
             << asset->low_price << ","
             << asset->volume << ","
             << asset->date;

        if (tableName == "stock_data") {
            const StockData* stock = static_cast<const StockData*>(asset);
            file << "," << stock->stock_name << ","
                 << stock->exchange << ","
                 << stock->sector;
        } else if (tableName == "option_data") {
            const OptionData* option = static_cast<const OptionData*>(asset);
            file << "," << option->option_symbol << ","
                 << option->option_type << ","
                 << option->expiry_date << ","
                 << option->strike_price;
        }
        file << "\n";
    }
    file.close();
    std::cout << "✅ Data exported to CSV successfully!" << std::endl;

}



/// @brief Creates asset-related tables in the database if they don't exist.
/// @return True if all table creation queries succeed, false otherwise.
bool DataBaseClass::createTables() {
    const std::string assetTable = R"(
        CREATE TABLE IF NOT EXISTS asset_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            open_price REAL,
            close_price REAL,
            high_price REAL,
            low_price REAL,
            volume INTEGER,
            date TEXT,
            UNIQUE(ticker, id)
        );
    )";

    const std::string stockTable = R"(
        CREATE TABLE IF NOT EXISTS stock_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            open_price REAL,
            close_price REAL,
            high_price REAL,
            low_price REAL,
            volume INTEGER,
            date TEXT,
            stock_name TEXT,
            exchange TEXT,
            sector TEXT,
            FOREIGN KEY(id) REFERENCES asset_data(id)
        );
    )";

    const std::string optionTable = R"(
        CREATE TABLE IF NOT EXISTS option_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            open_price REAL,
            close_price REAL,
            high_price REAL,
            low_price REAL,
            volume INTEGER,
            date TEXT,
            option_symbol TEXT,
            option_type TEXT,
            expiry_date REAL,
            strike_price REAL,
            FOREIGN KEY(id) REFERENCES asset_data(id)
        );
    )";

    return executeSQL(assetTable) && executeSQL(stockTable) && executeSQL(optionTable);
}


/// @brief Executes a single SQL statement for schema creation.
/// @param sql Raw SQL statement.
/// @return True if execution is successful, false otherwise.

bool DataBaseClass::executeSQL(const std::string& sql) {
    char* errMsg = nullptr;
    if (sqlite3_exec(db, sql.c_str(), nullptr, nullptr, &errMsg) != SQLITE_OK) {
        std::cerr << "❌ SQL Error: " << errMsg << std::endl;
        sqlite3_free(errMsg);
        return false;
    }
    return true;
}


/**
 * @brief Query asset data from a specified table in the SQLite database.
 *
 * This method supports querying from multiple tables—`stock_data`, `option_data`, or `asset_data`—
 * using a common interface. It constructs the query based on input parameters, executes it using
 * parameter binding to avoid SQL injection, and returns the result as a vector of `AssetData` structs.
 *
 * Depending on the table queried, fields like `strike_price`, `implied_volatility`, `option_type`,
 * `is_call`, and `expiry_date` will be populated only if present (e.g., for `option_data`).
 *
 * @param table_name     The name of the table to query: "stock_data", "option_data", or "asset_data".
 * @param ticker         The ticker symbol to filter on.
 * @param startDate      Optional start date for filtering (inclusive).
 * @param endDate        Optional end date for filtering (inclusive).
 * @param limit          The maximum number of results to return.
 * @param ascending      Whether to sort results by date ascending (true) or descending (false).
 * 
 * @return std::vector<AssetData> A vector of AssetData structs containing the query result.
 *         Returns an empty vector if the query fails or if the table name is invalid.
 *
 * @note Make sure the schema for the provided table name matches the expected column layout
 *       or adjust the column indices accordingly.
 */
std::vector<std::unique_ptr<AssetData>> DataBaseClass::queryData(
    const std::string& table_name,
    const std::string& ticker,
    const std::string& startDate,
    const std::string& endDate,
    int limit,
    bool ascending) const
{
    std::vector<std::unique_ptr<AssetData>> results;

    // 🛡️ Validate the table name
    if (table_name != "stock_data" && table_name != "option_data" && table_name != "asset_data") {
        std::cerr << "❌ Invalid table name provided to queryAssetData: " << table_name << std::endl;
        return results;
    }

    // 🧠 Build base query
    std::ostringstream oss;
    oss << "SELECT * FROM " << table_name;

    std::vector<std::string> conditions;
    if (!ticker.empty()) conditions.emplace_back("ticker = ?");
    if (!startDate.empty()) conditions.emplace_back("date >= ?");
    if (!endDate.empty()) conditions.emplace_back("date <= ?");

    if (!conditions.empty()) {
        oss << " WHERE ";
        for (size_t i = 0; i < conditions.size(); ++i) {
            if (i > 0) oss << " AND ";
            oss << conditions[i];
        }
    }

    oss << " ORDER BY date " << (ascending ? "ASC" : "DESC")
        << " LIMIT ?;";

    std::string query = oss.str();

    // 📋 Prepare statement
    sqlite3_stmt* stmt;
    if (sqlite3_prepare_v2(db, query.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
        std::cerr << "❌ Failed to prepare query: " << sqlite3_errmsg(db) << std::endl;
        return results;
    }

    // 🎯 Bind parameters
    int paramIndex = 1;
    if (!ticker.empty()) sqlite3_bind_text(stmt, paramIndex++, ticker.c_str(), -1, SQLITE_TRANSIENT);
    if (!startDate.empty()) sqlite3_bind_text(stmt, paramIndex++, startDate.c_str(), -1, SQLITE_TRANSIENT);
    if (!endDate.empty()) sqlite3_bind_text(stmt, paramIndex++, endDate.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_int(stmt, paramIndex++, limit);

    // 🧠 Extract data row-by-row
    while (sqlite3_step(stmt) == SQLITE_ROW) {
        auto data = std::make_unique<AssetData>();

        // Common Fields
        data->id = sqlite3_column_int(stmt, 0);
        data->ticker = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 1));
        data->open_price = static_cast<float>(sqlite3_column_double(stmt, 2));
        data->close_price = static_cast<float>(sqlite3_column_double(stmt, 3));
        data->high_price = static_cast<float>(sqlite3_column_double(stmt, 4));
        data->low_price = static_cast<float>(sqlite3_column_double(stmt, 5));
        data->volume = sqlite3_column_int(stmt, 6);
        const auto* dateText = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 7));
        data->date = dateText ? std::string(dateText) : std::string();

        if (table_name == "stock_data") {
            auto stock_data = std::make_unique<StockData>();
            *static_cast<AssetData*>(stock_data.get()) = *data;
            // Stock-specific Fields
            stock_data->stock_name = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 8));
            stock_data->exchange = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 9));
            stock_data->sector = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 10));
            results.push_back(std::move(stock_data));
        }
        // Option-specific Fields
        else if (table_name == "option_data") {
            auto option_data = std::make_unique<OptionData>();
            *static_cast<AssetData*>(option_data.get()) = *data;
            option_data->option_symbol = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 8));
            option_data->option_type = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 9));
            option_data->expiry_date = sqlite3_column_int64(stmt, 10);
            option_data->strike_price = static_cast<float>(sqlite3_column_double(stmt, 11));
            results.push_back(std::move(option_data));
        }
        else if (table_name == "asset_data") {
            results.push_back(std::move(data));
        } else {
            std::cerr << "❌ Invalid table name provided to queryAssetData: " << table_name << std::endl;
            sqlite3_finalize(stmt);
            return results;
        }
    }

    sqlite3_finalize(stmt);
    return results;
}

/// @brief Convenience ctor when no CSV backups are provided.
DataBaseClass::DataBaseClass(const std::string& db_path)
    : DataBaseClass(db_path, "", "", "", true, false) {}