cmake_minimum_required(VERSION 3.16)
project(DerivativeTradingCore VERSION 0.1.0 LANGUAGES CXX)

# The legacy Crow server retains its existing Makefile. This target deliberately
# has no broker SDK, networking, database, or frontend dependency.
add_library(trading_core INTERFACE)
target_compile_features(trading_core INTERFACE cxx_std_17)
target_include_directories(trading_core INTERFACE
    ${CMAKE_CURRENT_SOURCE_DIR}/src/backend/cpp_src/core/include)

function(configure_executable target)
    target_link_libraries(${target} PRIVATE trading_core)
    set_target_properties(${target} PROPERTIES CXX_EXTENSIONS OFF)
    if(MSVC)
        target_compile_options(${target} PRIVATE /W4 /WX)
    else()
        target_compile_options(${target} PRIVATE -Wall -Wextra -Wpedantic -Werror)
    endif()
endfunction()

add_executable(broker_demo apps/broker_demo.cpp)
configure_executable(broker_demo)

include(CTest)
if(BUILD_TESTING)
    add_executable(core_tests tests/core_tests.cpp)
    configure_executable(core_tests)
    add_test(NAME core_tests COMMAND core_tests)
endif()
