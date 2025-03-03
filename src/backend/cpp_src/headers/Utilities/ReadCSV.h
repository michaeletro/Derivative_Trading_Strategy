#ifndef READCSV_H
#define READCSV_H

#include <vector>
#include <string>
#include <sstream>
#include <fstream>
#include <iostream>

std::vector<std::vector<std::string>> readCSV(const std::string& filename) {
    std::vector<std::vector<std::string>> data;
    std::ifstream file(filename);

    if (!file.is_open()) {
        std::cerr << "Error: Could not open file " << filename << std::endl;
        return data;
    }

    std::string line;
    while (std::getline(file, line)) {
        std::vector<std::string> row;
        std::istringstream stream(line); // Correctly included <sstream>
        std::string value;

        while (std::getline(stream, value, ',')) {
            row.push_back(value);
        }

        data.push_back(row);
    }

    file.close();
    return data;
}
#endif 