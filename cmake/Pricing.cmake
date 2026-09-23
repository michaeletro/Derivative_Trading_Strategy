# Numerical code is independently buildable, with no HTTP, database or broker SDK.
add_library(trading_pricing STATIC src/backend/cpp_src/pricing/pricing.cpp src/backend/cpp_src/pricing/greeks.cpp src/backend/cpp_src/pricing/sde.cpp src/backend/cpp_src/pricing/hedging.cpp)
configure_executable(trading_pricing)
if(NOT MSVC)
    target_compile_options(trading_pricing PRIVATE -fno-fast-math)
endif()
add_executable(pricing_demo apps/pricing_demo.cpp)
configure_executable(pricing_demo)
target_link_libraries(pricing_demo PRIVATE trading_pricing)
if(BUILD_TESTING)
    add_executable(pricing_tests tests/pricing_tests.cpp)
    configure_executable(pricing_tests)
    target_link_libraries(pricing_tests PRIVATE trading_pricing)
    add_test(NAME pricing_tests COMMAND pricing_tests)
    add_executable(greeks_tests tests/greeks_tests.cpp)
    configure_executable(greeks_tests)
    target_link_libraries(greeks_tests PRIVATE trading_pricing)
    add_test(NAME greeks_tests COMMAND greeks_tests)
    add_executable(sde_tests tests/sde/sde_tests.cpp)
    configure_executable(sde_tests)
    target_link_libraries(sde_tests PRIVATE trading_pricing)
    add_test(NAME sde_tests COMMAND sde_tests)
    add_executable(hedging_tests tests/hedging/hedging_tests.cpp)
    configure_executable(hedging_tests)
    target_link_libraries(hedging_tests PRIVATE trading_pricing)
    add_test(NAME hedging_tests COMMAND hedging_tests)
    set_tests_properties(pricing_tests greeks_tests hedging_tests PROPERTIES TIMEOUT 45)
endif()

# A content fingerprint works even for source archives without .git metadata.
set(pricing_fingerprints "")
foreach(path src/backend/cpp_src/core/include/dts/pricing.hpp
             src/backend/cpp_src/pricing/pricing.cpp
             src/backend/cpp_src/pricing/greeks.cpp
             src/backend/cpp_src/pricing/simulation_detail.hpp
             src/backend/cpp_src/core/include/dts/greeks.hpp
             src/backend/cpp_src/http/pricing_json.hpp
             src/backend/cpp_src/http/greeks_json.hpp cmake/Pricing.cmake)
    set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/${path}")
    file(SHA256 "${CMAKE_CURRENT_SOURCE_DIR}/${path}" digest)
    string(APPEND pricing_fingerprints "${path}:${digest}\n")
endforeach()
string(SHA256 pricing_fingerprint "${pricing_fingerprints}")
set(pricing_revision "unavailable")
set(pricing_dirty "unknown")
find_package(Git QUIET)
if(GIT_FOUND AND EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/.git")
    execute_process(COMMAND "${GIT_EXECUTABLE}" rev-parse HEAD
        WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}" OUTPUT_VARIABLE revision
        OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET)
    if(revision MATCHES "^[0-9a-f]+$")
        set(pricing_revision "${revision}")
        execute_process(COMMAND "${GIT_EXECUTABLE}" status --porcelain --untracked-files=normal
            WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}" OUTPUT_VARIABLE dirty
            OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET)
        set(pricing_dirty "false")
        if(NOT dirty STREQUAL "")
            set(pricing_dirty "true")
        endif()
    endif()
endif()
set(research_fingerprints "")
foreach(path src/backend/cpp_src/core/include/dts/replay.hpp
             src/backend/cpp_src/research/replay.cpp src/backend/cpp_src/research/sha256.cpp
             src/backend/cpp_src/storage/src/research_store.inc
             src/backend/cpp_src/storage/include/dts/research_schema.hpp
             src/backend/cpp_src/http/research_json.hpp cmake/Pricing.cmake)
    set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/${path}")
    file(SHA256 "${CMAKE_CURRENT_SOURCE_DIR}/${path}" digest)
    string(APPEND research_fingerprints "${path}:${digest}\n")
endforeach()
string(SHA256 research_fingerprint "${research_fingerprints}")
set(sde_fingerprints "")
foreach(path src/backend/cpp_src/core/include/dts/sde.hpp src/backend/cpp_src/pricing/sde.cpp
             src/backend/cpp_src/pricing/simulation_detail.hpp src/backend/cpp_src/pricing/pricing.cpp
             src/backend/cpp_src/http/sde_json.hpp src/backend/cpp_src/http/experiments_json.hpp
             src/backend/cpp_src/storage/src/numerical_store.inc src/backend/cpp_src/storage/include/dts/numerical_schema.hpp cmake/Pricing.cmake)
    set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/${path}")
    file(SHA256 "${CMAKE_CURRENT_SOURCE_DIR}/${path}" digest)
    string(APPEND sde_fingerprints "${path}:${digest}\n")
endforeach()
string(SHA256 sde_fingerprint "${sde_fingerprints}")
set(hedging_fingerprints "")
foreach(path src/backend/cpp_src/core/include/dts/hedging.hpp src/backend/cpp_src/pricing/hedging.cpp
             src/backend/cpp_src/pricing/simulation_detail.hpp src/backend/cpp_src/pricing/pricing.cpp
             src/backend/cpp_src/http/hedging_json.hpp src/backend/cpp_src/http/experiments_json.hpp
             src/backend/cpp_src/storage/include/dts/hedging_schema.hpp cmake/Pricing.cmake)
    set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/${path}")
    file(SHA256 "${CMAKE_CURRENT_SOURCE_DIR}/${path}" digest)
    string(APPEND hedging_fingerprints "${path}:${digest}\n")
endforeach()
string(SHA256 hedging_fingerprint "${hedging_fingerprints}")
file(MAKE_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/generated")
configure_file("${CMAKE_CURRENT_SOURCE_DIR}/cmake/pricing_build.hpp.in"
               "${CMAKE_CURRENT_BINARY_DIR}/generated/pricing_build.hpp" @ONLY)
