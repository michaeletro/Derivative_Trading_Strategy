#include "../../headers/APIConnection/APIConnectionClass.h"
#include <algorithm> // For std::all_of
#include <cctype>    // For ::isdigit
#include <stdexcept> // For std::runtime_error
#include <iostream>  // For debug logs
#include <rapidjson/document.h>
#include <rapidjson/error/en.h> // For GetParseError_En

#include <curl/curl.h> // Required for libcurl functions
#include <sstream>     // Required for std::ostringstream


// Callback for CURL write function

size_t WriteCallBack(char* data, size_t size, size_t nmemb, void* userdata) {
    std::ostringstream* stream = static_cast<std::ostringstream*>(userdata);
    size_t totalSize = size * nmemb;
    if (stream) {
        stream->write(data, totalSize);
    }
    return totalSize;
}

// Constructor
APIConnection::APIConnection(const QueryData& query_data) : query(query_data) {}

// Validate Parameters
void APIConnection::validateParameters() const {
    if (query.asset_name.empty()) {
        throw std::invalid_argument("❌ Asset name cannot be empty.");
    }
    if (query.time_span != "minute" && query.time_span != "hour" && query.time_span != "day" &&
        query.time_span != "week" && query.time_span != "month") {
        throw std::invalid_argument("❌ Invalid time span.");
    }
    if (query.start_date > query.end_date) {
        throw std::invalid_argument("❌ Start date must precede or equal the end date.");
    }
    if (query.sort != "asc" && query.sort != "desc") {
        throw std::invalid_argument("❌ Sort order must be 'asc' or 'desc'.");
    }
    if (query.api_key.empty()) {
        throw std::invalid_argument("❌ API key cannot be empty.");
    }
}

// Validate Response
void APIConnection::validateResponse(const rapidjson::Document& response) const {
    if (!response.HasMember("status") || !response["status"].IsString() ||
        std::string(response["status"].GetString()) != "OK") {
        throw std::runtime_error("❌ Invalid API response: 'status' is missing or not 'OK'.");
    }
    if (!response.HasMember("results") || !response["results"].IsArray()) {
        throw std::runtime_error("❌ Invalid API response: 'results' is missing or not an array.");
    }
    if (!response.HasMember("resultsCount") || !response["resultsCount"].IsInt()) {
        throw std::runtime_error("❌ Invalid API response: 'resultsCount' is missing.");
    }
    if (response["resultsCount"].GetInt() <= 0) {
        throw std::runtime_error("⚠️ No results available.");
    }
}

// Fetch API Data
rapidjson::Document APIConnection::fetchAPIData() const {
    validateParameters(); // Validate parameters before API call
    
    std::string url = APIStringGenerator::generateURL(query);

    if (query.debug) {
        std::cout << "Generated URL: " << url << std::endl;
    }

    CURL* curl = curl_easy_init();
    if (!curl) {
        throw std::runtime_error("Failed to initialize CURL.");
    }

    std::ostringstream response_stream;
    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, WriteCallBack);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &response_stream);

    CURLcode res = curl_easy_perform(curl);
    if (res != CURLE_OK) {
        std::string error = curl_easy_strerror(res);
        curl_easy_cleanup(curl);
        throw std::runtime_error("CURL request failed: " + error);
    }

    curl_easy_cleanup(curl);

    // Parse JSON response
    rapidjson::Document doc;
    if (doc.Parse(response_stream.str().c_str()).HasParseError()) {
        throw std::runtime_error("JSON parse error: " +
                                 std::string(rapidjson::GetParseError_En(doc.GetParseError())));
    }

    // Debug raw JSON response
    if (query.debug) {
        std::cout << "Raw JSON response: " << response_stream.str() << std::endl;
    }

    // Check for errors in the API response
    if (!doc.IsObject() || !doc.HasMember("status") || doc["status"].GetString() != std::string("OK")) {
        throw std::runtime_error("API returned an error: " +
                                 std::string(doc.HasMember("error") ? doc["error"].GetString() : "Unknown error"));
    }

    // Validate JSON structure
    if (!doc.HasMember("results") || !doc["results"].IsArray()) {
        throw std::runtime_error("Invalid JSON response: Missing or malformed 'results' field.");
    }

    // Extract and return results
    if (!doc.HasMember("resultsCount") || !doc["resultsCount"].IsInt()) {
        throw std::runtime_error("Invalid JSON response: Missing or malformed 'resultsCount' field.");
    }   
    validateResponse(doc);

    return doc;
}
