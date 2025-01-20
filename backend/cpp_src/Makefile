# Compiler and Flags
CXX = g++
CXXFLAGS = -Iheaders -I/usr/include/rapidjson -std=c++17 -Wall -Wextra -pedantic
LDFLAGS = -lcurl

# Directories
SRCDIR = src
INCDIR = headers
OBJDIR = obj
BINDIR = bin

# Source and Object Files
SOURCES = $(wildcard $(SRCDIR)/*.cpp) main.cpp
OBJECTS = $(patsubst $(SRCDIR)/%.cpp, $(OBJDIR)/%.o, $(SOURCES))
TARGET = $(BINDIR)/program

# Build Rules
all: $(TARGET)

$(TARGET): $(OBJECTS)
	@mkdir -p $(BINDIR)
	$(CXX) $(CXXFLAGS) -o $@ $^ $(LDFLAGS)

$(OBJDIR)/%.o: $(SRCDIR)/%.cpp
	@mkdir -p $(OBJDIR)
	$(CXX) $(CXXFLAGS) -c $< -o $@

clean:
	rm -rf $(OBJDIR) $(BINDIR)
