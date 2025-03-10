# Compiler
CXX = g++
CXXFLAGS = -std=c++17 -Wall -Wextra -O2 -Iheaders -Isrc -I/usr/include/eigen3

# Directories
SRC_DIR = src
OBJ_DIR = obj
BIN_DIR = bin
INCLUDE_DIR = headers

# Libraries
LIBS = -lsqlite3 -lcurl

# Find all .cpp files in subdirectories
SRCS = $(shell find $(SRC_DIR) -name "*.cpp")
OBJS = $(patsubst $(SRC_DIR)/%.cpp, $(OBJ_DIR)/%.o, $(SRCS))

# Executable target
TARGET = $(BIN_DIR)/server

# Default target
all: $(TARGET)

# Compile the executable
$(TARGET): $(OBJS)
	@mkdir -p $(BIN_DIR)
	$(CXX) $(CXXFLAGS) -o $@ $^ $(LIBS)

# Compile each .cpp file into an object file
$(OBJ_DIR)/%.o: $(SRC_DIR)/%.cpp
	@mkdir -p $(dir $@)
	$(CXX) $(CXXFLAGS) -I$(INCLUDE_DIR) -I$(SRC_DIR) -c $< -o $@

# Clean command
clean:
	rm -rf $(OBJ_DIR) $(BIN_DIR)

# Debug mode
debug: CXXFLAGS += -g
debug: all
