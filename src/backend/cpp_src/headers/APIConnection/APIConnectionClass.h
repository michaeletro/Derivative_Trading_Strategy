#ifndef API_CONNECTION_H
#define API_CONNECTION_H

#include <string>
#include <map>
#include <memory>
#include <stdexcept>
#include <rapidjson/document.h>
#include <vector>

#include "APIStringGeneratorClass.h"


class APIConnection {
    protected:
        // Data Structure Pertaining to a Query and Asset Data
        QueryData query;
        
        // Helper Methods
        virtual void validateParameters() const;
        virtual void validateResponse(const rapidjson::Document& response) const;

    public:
        // Constructors and Destructor
        APIConnection(const QueryData& query_data);
        virtual ~APIConnection() = default;

        // Deleted functions to prevent copying
        APIConnection(const APIConnection&) = delete;
        APIConnection& operator=(const APIConnection&) = delete;

        // Move semantics
        APIConnection(APIConnection&&) noexcept = default;
        APIConnection& operator=(APIConnection&&) noexcept = default;

        // API Interaction
        rapidjson::Document fetchAPIData() const;
    };

#endif // API_CONNECTION_H
