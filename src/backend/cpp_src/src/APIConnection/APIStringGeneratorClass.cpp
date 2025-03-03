#include "../../headers/APIConnection/APIStringGeneratorClass.h"
#include <sstream>

std::string APIStringGenerator::generateURL(const QueryData& query) {
    std::ostringstream url_stream;
    
    return url_stream.str();
}

std::string APIStringGenerator::RestAPIQueryTypeGenerate(RestAPIQueryType query_type, bool debug){

    // Endpoint Map is defined in the DataStructs.h file 
    if (debug){
        std::cout << "Key for value '" << endpointMap[query_type] << "' is: " << found << std::endl;
    };
    return endpointMap.at(query_type);

}
