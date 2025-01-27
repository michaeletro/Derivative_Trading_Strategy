# Derivative Trading Strategy Project

This project is a multi-component application for developing, testing, and analyzing derivative trading strategies. It consists of a backend written in C++ with SQLite integration and a frontend built using Python's Dash framework.

---

## Project Structure

### Directory Layout:

```
Derivative_Trading_Strategy/
├── src/
│   ├── backend/
│   │   ├── cpp_src/
│   │   │   ├── headers/       # Header files for C++ classes
│   │   │   ├── src/           # Source files for backend functionality
│   │   │   ├── bin/           # Compiled executables
│   │   │   ├── obj/           # Compiled object files
│   │   │   └── build.sh       # Shell script to build the backend
│   ├── frontend/
│   │   ├── app.py             # Entry point for Dash application
│   │   ├── assets/            # Static assets (CSS, JS, images)
│   │   ├── requirements.txt   # Python dependencies
│   │   └── README.md          # Frontend-specific documentation
├── data_files/                # Data input files and configurations
├── .gitignore                 # Ignored files for Git
└── README.md                  # Project documentation (this file)
```

---

## Backend

### Overview:
The backend is a C++ application with the following features:
- REST API server using the Crow framework.
- SQLite database for storing and querying financial data.
- Core classes for managing assets, portfolios, and financial calculations.

### Build Instructions:
1. Navigate to the `cpp_src` directory:
   ```bash
   cd src/backend/cpp_src
   ```
2. Build the project:
   ```bash
   ./build.sh
   ```
   This compiles all source files and generates an executable in the `bin/` directory.
3. Run the server:
   ```bash
   ./bin/server
   ```

### REST API Endpoints:
- `GET /api/data` - Fetch all asset data from the database.
- `POST /api/add` - Add a new asset data entry.

### Database:
SQLite is used for storing asset data. The database file `quant_data.db` resides in the backend directory.

### Example Table Schema:
```sql
CREATE TABLE asset_data (
    id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    date TEXT NOT NULL,
    open_price REAL,
    close_price REAL,
    high_price REAL,
    low_price REAL,
    volume REAL
);
```

---

## Frontend

### Overview:
The frontend is a Python Dash application for visualizing and interacting with financial data.

### Setup Instructions:
1. Create a virtual environment:
   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On Windows, use `venv\Scripts\activate`
   ```
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Run the application:
   ```bash
   python app.py
   ```

### Features:
- Interactive dashboards with visualizations of trading strategies.
- Integration with the backend for fetching and updating financial data.
- Real-time charting using Dash and Plotly.

---

## Development Notes

### Adding Backend Features:
- To add a new API endpoint, modify `server.cpp`.
- Define database interactions in `DatabaseClass.cpp`.
- Rebuild the backend after any changes.

### Adding Frontend Features:
- Modify `app.py` to add new pages or charts.
- Place static assets (CSS, JS, images) in the `assets/` directory.

### Testing:
- Backend: Use tools like Postman or cURL to test API endpoints.
- Frontend: Use a browser to interact with the Dash application.

---

## Future Enhancements

- **Advanced Analytics:** Add machine learning models for predictive trading.
- **User Authentication:** Secure API endpoints and frontend access.
- **Expanded Database:** Include additional financial data such as options and futures.
- **Deployment:** Deploy the application using Docker or a cloud platform.

---

## License
This project is licensed under the MIT License. See `LICENSE` for more details.

---

## Contact
For questions or contributions, contact:
- **Developer:** Michael Perez
- **Email:** mperez1498@gmail.com

