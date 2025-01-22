Derivative Trading Strategy Framework

This project is a high-performance trading strategy framework, designed with a Python-based frontend for data visualization and a C++ backend for handling data processing and API interactions. It includes modules for financial asset analysis, portfolio management, and real-time data visualization.

Derivative_Trading_Strategy/
├── backend/
│   ├── cpp_src/
│   │   ├── src/
│   │   │   ├── APIConnectionClass.cpp
│   │   │   ├── APIStringGeneratorClass.cpp
│   │   │   ├── AssetClass.cpp
│   │   │   ├── OptionClass.cpp
│   │   │   ├── PortfolioClass.cpp
│   │   │   ├── StockClass.cpp
│   │   │   ├── TimeSeriesClass.cpp
│   │   │   ├── TimeConversion.cpp
│   │   │   └── main.cpp
│   │   ├── headers/
│   │   │   ├── APIConnectionClass.h
│   │   │   ├── APIStringGeneratorClass.h
│   │   │   ├── AssetClass.h
│   │   │   ├── OptionClass.h
│   │   │   ├── PortfolioClass.h
│   │   │   ├── StockClass.h
│   │   │   ├── TimeSeriesClass.h
│   │   │   └── TimeConversion.h
│   │   ├── Makefile
│   │   └── obj/
│   └── bin/
│       └── program (generated after build)
├── frontend/
│   ├── app.py (Dash app for live visualization)
│   ├── utils.py (Helper functions for data handling)
│   └── data/
│       └── example.csv (Sample data file for testing)
└── README.md

Backend (C++)

High-Performance Data Processing: Processes financial time-series data efficiently using C++.

API Interaction: Fetches data from financial APIs using libcurl.
Asset and Portfolio Management: Classes for managing stocks, options, and portfolios.
Real-Time Integration: Communicates with the frontend for real-time updates.
Unix Timestamp Conversion: Includes utilities for converting Unix timestamps to human-readable formats.

Frontend (Python)

Interactive Visualization: Built with Dash, providing a live-updating bar chart and CSV upload feature.
Data Integration: Periodically reads data from the backend and updates the charts dynamically.

Setup

Requirements
Backend
C++17 or higher
libcurl library
Makefile (for building the C++ source code)
Frontend
Python 3.7 or higher
Python libraries:
dash
pandas
plotly
Installation

Clone the repository:

bash
Copy
Edit
git clone https://github.com/your-repo/Derivative_Trading_Strategy.git
cd Derivative_Trading_Strategy
Backend Setup:

Navigate to backend/cpp_src and build the C++ project:
bash
Copy
Edit
cd backend/cpp_src
./build.sh
Ensure the compiled binary is located in backend/bin/program.
Frontend Setup:

Install Python dependencies:
bash
Copy
Edit
pip install dash pandas plotly
Start the Dash app:
bash
Copy
Edit
python frontend/app.py

Usage
Run the Backend: Execute the compiled binary in backend/bin/:

bash
Copy
Edit
./program

Run the Frontend: Start the Dash app:

bash
Copy
Edit
python frontend/app.py

Live Updates:

Upload a CSV file via the frontend to see it visualized in real-time.
Backend and frontend communicate seamlessly for periodic updates.
Performance Comparison
The backend leverages C++ for high-performance data handling, outperforming traditional Python-based processing by approximately 10-20x (depending on the complexity of operations).

Future Improvements

Integrate a database for persistent storage.
Add more visualization options (e.g., line charts, scatter plots).
Enable multi-user functionality.
Enhance API support for more financial data providers.

Contributors

Michael Perez - Development Lead
License
This project is licensed under the MIT License.

