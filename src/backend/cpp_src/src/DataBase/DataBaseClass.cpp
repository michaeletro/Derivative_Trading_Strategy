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

    std::cout << "✅ Database opened successfully." << std::endl;




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
    std::string query = "SELECT COUNT(*) FROM assets;";
    sqlite3_stmt* stmt;
    // This is a simple check to see if the database is empty by counting rows in the assets table.
    // If the table doesn't exist, we assume the database is empty.
    if (sqlite3_prepare_v2(db, query.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
        std::cerr << "❌ Error preparing SQL statement: " << sqlite3_errmsg(db) << std::endl;
        return true; // Assume empty if error occurs
    }

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

void DataBaseClass::exportToCSV(const std::string& csvFile, 
    [[maybe_unused]] const std::string& db_path, 
    [[maybe_unused]] const std::string& table_name,
    [[maybe_unused]] const std::string& date_start,
    [[maybe_unused]] const std::string& date_end) {
    // Check if the database is open
    if (db == nullptr) {
        std::cerr << "❌ Database is not open!" << std::endl;
        return;
    }
    // Check if the CSV file already exists
    std::ofstream file(csvFile);    
    // Check if the file opened successfully for writing 
    // If not, print an error message and return
    // This is important to ensure that the file is created or overwritten correctly.
    if (!file.is_open()) {
        std::cerr << "❌ Error opening CSV file for writing!" << std::endl;
        return;
    }

    // Write the header line to the CSV file 
    // This is important to ensure that the CSV file has a proper header for readability.
    // The header line contains the names of the columns in the database.
    // This is important for readability and to ensure that the CSV file can be easily understood.

    std::ostringstream query;
    // Depending on the table name, we will select different columns to export
    // This is important to ensure that we only export the relevant data for the specified table.
    // The table name is passed as a parameter to the function, allowing for flexibility in exporting different tables.
    // The query is constructed using a string stream to allow for easy concatenation of strings.
    switch (table_name) {
        case "asset_data":
            query << "SELECT id,ticker,asset_type,name,exchange,sector\n";
            break;
        case "stock_data":
            query << "SELECT id,ticker,date,open_price,close_price,high_price,low_price,volume\n";
            break;
        case "option_data":
            query << "SELECT id,ticker,asset_name,date,strike_price,open_price,close_price,high_price,low_price,option_type,is_call,expiry_date \n";
            break;
        case constant expression:
            /* code */
            break;
        default:
            std::cerr << "❌ Invalid table name!" << std::endl;
            file.close();
            return;
        // This is important to ensure that we are selecting the correct data from the database.
    }
    
    // Construct the SQL query to select data from the specified table
    std::string combined_query << query << "FROM " << table_name << "\n";
    // Depending on the table name, we will add different WHERE clauses to filter the data
    // This is important to ensure that we only export the relevant data for the specified date range.
    switch (table_name) {
        case "stock_data":
            combined_query << "WHERE date > '" << date_start << "' AND date < '" << date_end << "';";
            break;
        case "option_data":
            combined_query << "WHERE date > '" << date_start << "' AND date < '" << date_end << "';";
            break;
        case constant expression:
            /* code */
            break;
        default:
            combined_query << ";";
            break;
    }
    
    // Prepare the SQL statement
    sqlite3_stmt* stmt;

    // This is important to ensure that the SQL statement is prepared correctly before executing it.
    if (sqlite3_prepare_v2(db, combined_query.c_str(), -1, &stmt, nullptr) == SQLITE_OK) {
        while (sqlite3_step(stmt) == SQLITE_ROW) {
            file << sqlite3_column_int(stmt, 0) << ","
                 << sqlite3_column_text(stmt, 1) << ",";
            // Depending on the table name, we will write different columns to the CSV file
            // This is important to ensure that we are exporting the correct data to the CSV file.
            // The columns are written in the same order as they are selected in the SQL query.
            switch (table_name) {
                case "asset_data":
                    file << sqlite3_column_text(stmt, 2) << ","
                         << sqlite3_column_text(stmt, 3) << ","
                         << sqlite3_column_text(stmt, 4) << ","
                         << sqlite3_column_text(stmt, 5) << "\n";
                    break;
                case "stock_data":
                    file << sqlite3_column_int64(stmt, 2) << ","
                         << sqlite3_column_double(stmt, 3) << ","
                         << sqlite3_column_double(stmt, 4) << ","
                         << sqlite3_column_double(stmt, 5) << ","
                         << sqlite3_column_double(stmt, 6) << ","
                         << sqlite3_column_int64(stmt, 7) << "\n";
                    break;
                case "option_data":
                    file << sqlite3_column_text(stmt, 2) << ","
                         << sqlite3_column_int64(stmt, 3) << ","
                         << sqlite3_column_double(stmt, 4) << ","
                         << sqlite3_column_double(stmt, 5) << ","
                         << sqlite3_column_double(stmt, 6) << ","
                         << sqlite3_column_double(stmt, 7) << ","
                         << sqlite3_column_double(stmt, 8) << ","
                         << sqlite3_column_text(stmt, 9) << ","
                         << sqlite3_column_int(stmt, 10) << "," 
                         << sqlite3_column_text(stmt, 11) << << "\n";
                    break;
                case constant expression:
                    /* code */
                    break;
                default:
                    std::cerr << "❌ Invalid table name!" << std::endl;
                    file.close();
                    sqlite3_finalize(stmt);
                    return;
            }
        }
        // Finalize the statement to release resources
        sqlite3_finalize(stmt);
    } else {
        std::cerr << "❌ Error exporting to CSV: " << sqlite3_errmsg(db) << std::endl;
    }
    // Close the file after writing
    // This is important to ensure that all data is written to the file and resources are released.
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

bool DataBaseClass::addAssetData(const AssetData& asset) {
    std::ostringstream oss;
    oss << "INSERT INTO asset_data (ticker, date, open_price, close_price, high_price, low_price, volume) "
        << "VALUES ('" << asset.ticker << "', '" << asset.date << "', " << asset.open_price << ", " << asset.close_price << ", "
        << asset.high_price << ", " << asset.low_price << ", " << asset.volume << ");";    
        return executeQuery(oss.str());
}
bool DataBaseClass::insertAssetData(const std::vector<AssetData>& assets) {
    for (const auto& asset : assets) {
        if (!addAssetData(asset)) {
            std::cerr << "❌ Error inserting asset data: " << asset.ticker << std::endl;
            return false;
        }
    }
    return true;
}
bool DataBaseClass::deleteAssetData(const std::string& ticker, const std::string& date) {
    std::ostringstream oss;
    oss << "DELETE FROM asset_data WHERE ticker = '" << ticker << "' AND date = '" << date << "';";
    return executeQuery(oss.str());
}
bool DataBaseClass::executeQuery(const std::string& query) {
    char* errMsg = nullptr;
    if (sqlite3_exec(db, query.c_str(), nullptr, nullptr, &errMsg) != SQLITE_OK) {
        std::cerr << "❌ SQL Error: " << errMsg << std::endl;
        sqlite3_free(errMsg);
        return false;
    }
    return true;
}

bool DataBaseClass::createTables() {
    const std::string assetTable = R"(
        CREATE TABLE IF NOT EXISTS asset_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            asset_type TEXT NOT NULL,
            name TEXT,
            exchange TEXT,
            sector TEXT,
            UNIQUE(ticker, id)
        );
    )";

    const std::string stockTable = R"(
        CREATE TABLE IF NOT EXISTS stock_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            date INTEGER,
            open_price REAL,
            close_price REAL,
            high_price REAL,
            low_price REAL,
            volume INTEGER,
            FOREIGN KEY(stock_id) REFERENCES asset_data(asset_id)
        );
    )";

    const std::string optionTable = R"(
        CREATE TABLE IF NOT EXISTS option_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            asset_name TEXT,
            date INTEGER,
            strike_price REAL,
            open_price REAL,
            close_price REAL,
            high_price REAL,
            low_price REAL,
            option_type TEXT,
            is_call INTEGER,
            expiry_date TEXT,
            FOREIGN KEY(option_id) REFERENCES asset_data(asset_id)
        );
    )";

    return executeSQL(assetTable) && executeSQL(stockTable) && executeSQL(optionTable);
}
bool DataBaseClass::executeSQL(const std::string& sql) {
    char* errMsg = nullptr;
    if (sqlite3_exec(db, sql.c_str(), nullptr, nullptr, &errMsg) != SQLITE_OK) {
        std::cerr << "❌ SQL Error: " << errMsg << std::endl;
        sqlite3_free(errMsg);
        return false;
    }
    return true;
}

std::vector<AssetData> DataBaseClass::queryAssetData(
    const std::string& ticker,
    const std::string& startDate,
    const std::string& endDate, int limit, bool ascending) const 
    {
    std::vector<AssetData> results;
    std::string query = "SELECT ticker, date, open, close, high, low, volume FROM assets WHERE ticker = ?";

    if (!startDate.empty()) query += " AND date >= ?";
    if (!endDate.empty()) query += " AND date <= ?";

    query += " ORDER BY date " + std::string(ascending ? "ASC" : "DESC") + " LIMIT ?;";

    sqlite3_stmt* stmt;
    if (sqlite3_prepare_v2(db, query.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
    std::cerr << "Error: Failed to prepare SQL statement.\n";
    return results;
    }

    int paramIndex = 1;
    sqlite3_bind_text(stmt, paramIndex++, ticker.c_str(), -1, SQLITE_STATIC);
    if (!startDate.empty()) sqlite3_bind_text(stmt, paramIndex++, startDate.c_str(), -1, SQLITE_STATIC);
    if (!endDate.empty()) sqlite3_bind_text(stmt, paramIndex++, endDate.c_str(), -1, SQLITE_STATIC);
    sqlite3_bind_int(stmt, paramIndex++, limit);

    while (sqlite3_step(stmt) == SQLITE_ROW) {
        AssetData data;
        data.id = sqlite3_column_int(stmt, 0);
        data.ticker = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 1));
        data.date = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 2));
        data.open_price = static_cast<float>(sqlite3_column_double(stmt, 3));
        data.close_price = static_cast<float>(sqlite3_column_double(stmt, 4));
        data.high_price = static_cast<float>(sqlite3_column_double(stmt, 5));
        data.low_price = static_cast<float>(sqlite3_column_double(stmt, 6));
        data.volume = sqlite3_column_int64(stmt, 7);  // ✅ Corrected from `sqlite3_column_double`
        data.strike_price = static_cast<float>(sqlite3_column_double(stmt, 8));
        data.implied_volatility = static_cast<float>(sqlite3_column_double(stmt, 9));
        data.option_type = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 10));
        data.is_call = sqlite3_column_int(stmt, 11) == 1;
        data.expiry_date = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 12));

        results.push_back(data);
    }

    sqlite3_finalize(stmt);
    return results;
}