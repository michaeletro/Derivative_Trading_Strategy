# Numerical code is independently buildable, with no HTTP, database or broker SDK.
add_library(trading_pricing STATIC src/backend/cpp_src/pricing/pricing.cpp)
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
    set_tests_properties(pricing_tests PROPERTIES TIMEOUT 45)
endif()

# A content fingerprint works even for source archives without .git metadata.
set(pricing_fingerprints "")
foreach(path src/backend/cpp_src/core/include/dts/pricing.hpp
             src/backend/cpp_src/pricing/pricing.cpp
             src/backend/cpp_src/http/pricing_json.hpp cmake/Pricing.cmake)
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
file(MAKE_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/generated")
configure_file("${CMAKE_CURRENT_SOURCE_DIR}/cmake/pricing_build.hpp.in"
               "${CMAKE_CURRENT_BINARY_DIR}/generated/pricing_build.hpp" @ONLY)
