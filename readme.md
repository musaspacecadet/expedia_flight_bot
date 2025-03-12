# Expedia Flight Scraper

A robust web scraper for extracting flight data from Expedia search results using a state machine approach.

## Features

- **State Machine Architecture**: Clean, predictable flow for stable operation
- **Cross-Platform Compatibility**: Works on both Windows and Linux/macOS systems
- **Auto-Save Results**: Automatically saves data as JSON with timestamped filenames
- **Understandable Data Format**: Uses human-readable keys for easier data analysis
- **Robust Error Handling**: Gracefully manages timeouts and errors

## Requirements

- Python 3.7+
- `cdp` library
- Browser automation libraries (`core.browser`, `core.launcher`)

## Usage

```python
from expedia_scraper import ExpediaFlightScraper

# Create a scraper instance
scraper = ExpediaFlightScraper(
    search_url="https://www.expedia.com/Flights-Search?...",  # Your search URL
    output_file="results/my_flights.json",  # Optional custom output path
    headless=False  # Set to True for headless operation
)

# Run the scraper and get results
flight_data = scraper.start()

# Data is also saved to the specified output file
```

## Output Structure

The scraper extracts comprehensive flight data including:

- Pricing information (total price, price per person)
- Flight leg details (departure/arrival times, routes, layovers)
- Airline information
- Fare options and included features

## States

The scraper uses these states to manage the flow:

1. `INITIALIZING`: Setting up the environment
2. `NAVIGATING`: Navigating to the search URL
3. `WAITING_FOR_RESULTS`: Waiting for search results to load
4. `SHOW_MORE_RESULTS`: Expanding search results (if available)
5. `COLLECTING_DATA`: Gathering raw data from responses
6. `PROCESSING_DATA`: Parsing and structuring the data
7. `SAVING_RESULTS`: Writing data to disk
8. `COMPLETED`: Successful completion
9. `ERROR`: Error handling

## Customization

You can extend the scraper by:

- Adding new states to the `ScraperState` enum
- Implementing additional parsing logic in `_parse_flight_data`
- Modifying timeout values in `_run_state_machine`

