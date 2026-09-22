# Compile a fixed allowlist of public UI assets into the server. No runtime
# directory traversal, source-tree serving, CDN, or frontend toolchain required.
set(DTS_DASHBOARD_DIR "${CMAKE_CURRENT_SOURCE_DIR}/src/frontend/dashboard")
foreach(asset index.html styles.css app.mjs model.mjs)
    set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${DTS_DASHBOARD_DIR}/${asset}")
endforeach()
file(READ "${DTS_DASHBOARD_DIR}/index.html" DTS_DASHBOARD_HTML)
file(READ "${DTS_DASHBOARD_DIR}/styles.css" DTS_DASHBOARD_CSS)
file(READ "${DTS_DASHBOARD_DIR}/app.mjs" DTS_DASHBOARD_APP)
file(READ "${DTS_DASHBOARD_DIR}/model.mjs" DTS_DASHBOARD_MODEL)
file(MAKE_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/generated")
configure_file("${CMAKE_CURRENT_SOURCE_DIR}/cmake/dashboard_assets.hpp.in"
    "${CMAKE_CURRENT_BINARY_DIR}/generated/dashboard_assets.hpp" @ONLY)
target_include_directories(server PRIVATE "${CMAKE_CURRENT_BINARY_DIR}/generated")
