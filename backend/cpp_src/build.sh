#!/bin/bash

# Build Script
echo "Building the project..."
make
if [ $? -eq 0 ]; then
    echo "Build successful!"
else
    echo "Build failed."
    exit 1
fi
