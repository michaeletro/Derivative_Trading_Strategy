# Compatibility entry point; the executable's sources/dependencies live in CMake.
ROOT := $(abspath ../../..)
BUILD_DIR ?= $(ROOT)/build-server
CMAKE_ARGS ?=
.PHONY: all debug clean
all:
	cmake -S "$(ROOT)" -B "$(BUILD_DIR)" -DDTS_BUILD_SERVER=ON $(CMAKE_ARGS)
	cmake --build "$(BUILD_DIR)" --target server --parallel 2
	mkdir -p bin
	cmake -E copy_if_different "$(BUILD_DIR)/server" bin/server
debug:
	$(MAKE) BUILD_DIR="$(ROOT)/build-server-debug" CMAKE_ARGS="-DCMAKE_BUILD_TYPE=Debug $(CMAKE_ARGS)" all
clean:
	@if test -f "$(BUILD_DIR)/CMakeCache.txt"; then cmake --build "$(BUILD_DIR)" --target clean; fi
