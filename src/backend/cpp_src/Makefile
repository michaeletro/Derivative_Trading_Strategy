# Compiler and Flags
CXX = g++
CXXFLAGS = -std=c++17 -Wall -Wextra -pedantic -Iheaders -I/usr/include/rapidjson -I/usr/local/include/crow -I/usr/include/asio
LDFLAGS = -lcurl -lpthread

# Directories
SRCDIR = src
OBJDIR = obj/src
BINDIR = bin

# Source Files and Target
SOURCES = $(wildcard $(SRCDIR)/*.cpp)
OBJECTS = $(patsubst $(SRCDIR)/%.cpp, $(OBJDIR)/%.o, $(SOURCES))
TARGET = $(BINDIR)/server

# Default Rule
all: build

# Build Rule
build: $(TARGET)

$(TARGET): $(OBJECTS)
	@mkdir -p $(BINDIR)
	$(CXX) $(CXXFLAGS) -o $@ $^ $(LDFLAGS)

# Compile Rule
$(OBJDIR)/%.o: $(SRCDIR)/%.cpp
	@mkdir -p $(OBJDIR)
	$(CXX) $(CXXFLAGS) -c $< -o $@

# Clean Rule
clean:
	rm -rf $(OBJDIR) $(BINDIR)

# Phony Targets
.PHONY: all build clean
