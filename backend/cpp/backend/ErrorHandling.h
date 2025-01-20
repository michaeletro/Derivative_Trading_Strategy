#ifndef ERROR_HANDLING_H
#define ERROR_HANDLING_H

#include <stdexcept>
#include <string>

class APIError : public std::runtime_error {
public:
    APIError(const std::string& message) : std::runtime_error("API Error: " + message) {}
};

class DataError : public std::runtime_error {
public:
    DataError(const std::string& message) : std::runtime_error("Data Error: " + message) {}
};

class PortfolioError : public std::runtime_error {
public:
    PortfolioError(const std::string& message) : std::runtime_error("Portfolio Error: " + message) {}
};

#endif
