import json
import time
import os
import platform
from enum import Enum, auto
from typing import List, Dict, Any, Optional
from datetime import datetime
from core.browser import Browser
from core.launcher import Launcher
import cdp

# Only import DisplayManager if not on Windows
if platform.system() != "Windows":
    from core.control import DisplayConfig, DisplayManager


class ScraperState(Enum):
    """States for the flight scraper state machine"""
    INITIALIZING = auto()
    NAVIGATING = auto()
    WAITING_FOR_RESULTS = auto()
    SHOW_MORE_RESULTS = auto()
    COLLECTING_DATA = auto()
    PROCESSING_DATA = auto()
    SAVING_RESULTS = auto()
    COMPLETED = auto()
    ERROR = auto()


class ExpediaFlightScraper:
    """Flight scraper for Expedia using a state machine approach"""
    
    def __init__(self, search_url: str, output_file: str = None, headless: bool = False):
        self.search_url = search_url
        self.graph_url = "https://www.expedia.com/graphql"
        self.headless = headless
        self.state = ScraperState.INITIALIZING
        self.request_ids = []
        self.response_ids = []
        self.flight_data = []
        self.processed_flights = []
        self.error_message = None
        self.browser = None
        self.tab = None
        self.launcher = None
        self.display = None
        
        # Set default output filename if none provided
        if not output_file:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.output_file = f"flight_results_{timestamp}.json"
        else:
            self.output_file = output_file
        
    def start(self):
        """Main entry point for the scraper"""
        try:
            self._setup_browser()
            self._run_state_machine()
            return self.processed_flights
        except Exception as e:
            self.state = ScraperState.ERROR
            self.error_message = str(e)
            print(f"Error: {self.error_message}")
            return []
        finally:
            self._cleanup()
            
    def _setup_browser(self):
        """Initialize browser and set up event listeners with OS-specific handling"""
        self.launcher = Launcher()
        
        # Handle display differently based on OS
        is_windows = platform.system() == "Windows"
        if is_windows:
            print("Windows detected, not using virtual display")
            # On Windows, just launch the browser directly
            if not self.launcher.launch():
                raise Exception("Failed to launch browser")
        else:
            # On non-Windows, use DisplayManager if headless mode is not enabled
            if not self.headless:
                display_config = DisplayConfig(visible=self.headless)
                self.display = DisplayManager(display_config)
                self.display.__enter__()
            
            if not self.launcher.launch():
                raise Exception("Failed to launch browser")
            
        self.browser = Browser(self.launcher.websocket_url)
        self.browser.start()
        self.tab = self.browser.new_tab()
        self.tab.execute_command(cdp.network.enable())
        self.tab.add_event_listener(cdp.network.RequestWillBeSent, self._request_handler)
        self.tab.add_event_listener(cdp.network.ResponseReceived, self._response_handler)
    

    def _request_handler(self, event: cdp.network.RequestWillBeSent):
        """Handle network requests, filtering for GraphQL flight search requests"""
        request = event.request
        if request.url == self.graph_url and request.has_post_data:
            try:
                # Parse the JSON data from the request
                json_data = json.loads(request.post_data)
                
                # Handle both dictionary and list formats
                if isinstance(json_data, dict):
                    # When JSON is a dictionary
                    if (json_data.get("operationName") == "FlightsShoppingPwaFlightSearchResultsFlightsSearch" and 
                        "query" in json_data):
                        self.request_ids.append(event.request_id)
                elif isinstance(json_data, list):
                    # When JSON is a list of operations
                    for operation in json_data:
                        if isinstance(operation, dict) and operation.get("operationName") == "FlightsShoppingPwaFlightSearchResultsFlightsSearch" and "query" in operation:
                            self.request_ids.append(event.request_id)
                            break
            except json.JSONDecodeError:
                pass  # Not a valid JSON request
            except Exception as e:
                print(f"Error processing request: {str(e)}")
            
    def _response_handler(self, event: cdp.network.ResponseReceived):
        """Handle network responses, matching to tracked GraphQL requests"""
        response = event.response
        if response.url == self.graph_url and event.request_id in self.request_ids:
            self.response_ids.append(event.request_id)
            
    def _run_state_machine(self):
        """Run the state machine until completion or error"""
        max_wait_time = 60  # Maximum time to wait for results in seconds
        start_time = time.time()
        
        while self.state != ScraperState.COMPLETED and self.state != ScraperState.ERROR:
            if time.time() - start_time > max_wait_time and self.state == ScraperState.WAITING_FOR_RESULTS:
                self.state = ScraperState.ERROR
                self.error_message = "Timed out waiting for flight results"
                break
                
            self._process_current_state()
            time.sleep(0.5)  # Small delay to prevent CPU hogging
            
    def _process_current_state(self):
        """Process the current state and transition to the next state"""
        if self.state == ScraperState.INITIALIZING:
            print("Initializing scraper...")
            self.state = ScraperState.NAVIGATING
            
        elif self.state == ScraperState.NAVIGATING:
            print("Navigating to search URL...")
            self.tab.navigate(url=self.search_url)
            self.state = ScraperState.WAITING_FOR_RESULTS
            
        elif self.state == ScraperState.WAITING_FOR_RESULTS:
            if len(self.response_ids) > 0:
                self.state = ScraperState.SHOW_MORE_RESULTS
            
        elif self.state == ScraperState.SHOW_MORE_RESULTS:
            print("Checking for 'Show More' button...")
            show_more = self.tab.query_selector('button[name="showMoreButton"]')
            if show_more:
                print("Clicking 'Show More' button...")
                show_more.click()
                time.sleep(5)  # Wait for additional results to load
            self.state = ScraperState.COLLECTING_DATA
            
        elif self.state == ScraperState.COLLECTING_DATA:
            print(f"Collecting data from {len(self.response_ids)} responses...")
            for response_id in self.response_ids:
                try:
                    results, base64 = self.tab.execute_command(cdp.network.get_response_body(response_id))
                    data = json.loads(results)
                    
                    # Extract flight listings from the response
                    listings = data.get("data", {}).get("flightsSearch", {}).get("listingResult", {}).get("listings", [])
                    
                    if listings:
                        self.flight_data.append(listings)
                except Exception as e:
                    print(f"Error processing response {response_id}: {str(e)}")
            
            self.state = ScraperState.PROCESSING_DATA
            
        elif self.state == ScraperState.PROCESSING_DATA:
            print("Processing collected flight data...")
            
            for listings in self.flight_data:
                flights = self._parse_flight_data(listings)
                self.processed_flights.extend(flights)
                
            if self.processed_flights:
                print(f"Found {len(self.processed_flights)} flight offers")
                self.state = ScraperState.SAVING_RESULTS
            else:
                print("No flight offers found.")
                self.state = ScraperState.COMPLETED
                
        elif self.state == ScraperState.SAVING_RESULTS:
            print(f"Saving results to {self.output_file}...")
            try:
                with open(self.output_file, 'w', encoding='utf-8') as f:
                    json.dump(self.processed_flights, f, indent=4)
                print(f"Results saved successfully to {self.output_file}")
                self.state = ScraperState.COMPLETED
            except Exception as e:
                print(f"Error saving results: {str(e)}")
                self.state = ScraperState.ERROR
                self.error_message = f"Failed to save results: {str(e)}"
            
    def _parse_flight_data(self, data) -> List[Dict[str, Any]]:
        """Parse the flight data from the API response with more understandable keys"""
        if not isinstance(data, list):
            print("Error: The top-level structure in the JSON file should be a list.")
            return []

        flight_offers = []

        for item in data:
            if item.get("__typename") == "FlightsStandardOffer":
                offer = {}
                offer['type'] = item['__typename']

                # --- Accessibility Info ---
                offer['heading'] = item.get('accessibilityHeading')
                offer['description'] = item.get('accessibilityMessage')

                # --- Pricing Information ---
                pricing = item.get('pricingInformation')
                if pricing:
                    offer['total_price'] = pricing.get('price', {}).get('completeText')  # Main price
                    offer['price_per_person'] = pricing.get('pricePerTraveler', {}).get('completeText')
                    offer['total_price_all_travelers'] = pricing.get('totalPriceForAllTravelers', {}).get('completeText')
                    offer['trip_type'] = pricing.get('tripType')  # e.g., "Roundtrip per traveler"

                # --- Flight Legs Information ---
                journeys = item.get('journeys')
                if journeys:
                    offer['flight_legs'] = []
                    for journey in journeys:
                        leg_data = {}
                        leg_data['cabin_class'] = journey.get('cabinClass')
                        leg_data['travel_times'] = journey.get('departureAndArrivalTime', {}).get('completeText')
                        leg_data['route'] = journey.get('departureAndArrivalLocations')
                        leg_data['layover_info'] = journey.get('layoverInformation')
                        leg_data['duration_and_stops'] = journey.get('durationAndStops')
                        leg_data['operated_by'] = journey.get('flightOperatedBy')

                        # --- Airline Information ---
                        airlines = journey.get('airlines')
                        if airlines:
                            leg_data['airlines'] = []
                            for airline in airlines:
                                airline_info = {}
                                airline_info['name'] = airline.get('text')
                                airline_url = airline.get('image', {}).get('url')
                                if airline_url:
                                    airline_info['logo'] = airline_url

                                leg_data['airlines'].append(airline_info)

                        offer['flight_legs'].append(leg_data)

                # --- Fare Options and Details ---
                details_and_fares = item.get("detailsAndFares")
                if details_and_fares:
                    content = details_and_fares.get("content")
                    if content and content.get('sections'):
                        fares_info = next((sec for sec in content['sections'] if sec.get('__typename') == 'FlightsFaresInformation'), None)
                        if fares_info and fares_info.get("fares"):
                            offer['fare_options'] = []

                            for fare in fares_info.get('fares'):
                                fare_details = {}
                                fare_details['name'] = next((fd['text'] for fd in fare.get('fareDetails', []) if fd['__typename'] == 'EGDSHeading'),None)
                                fare_details['cabin'] = next((fd['text'] for fd in fare.get('fareDetails', []) if fd['__typename'] == 'EGDSPlainText'),None)

                                # Handle multiple fare pricings and states
                                fare_heading = fare.get('heading', {})
                                fare_pricings = fare_heading.get('farePricing', [])
                                fare_details['pricing_options'] = []
                                for fare_price in fare_pricings:
                                    for fare_state in fare_price.get('states', []):
                                        price_option = {}
                                        price_option['state'] = fare_state
                                        price_lockup = fare_price.get('priceLockup', {})
                                        price_option['accessible_price'] = price_lockup.get('accessibilityPrice')
                                        price_option['display_price'] = price_lockup.get('lockupPrice')
                                        fare_details['pricing_options'].append(price_option)
                                
                                # Amenities
                                amenities = fare.get("amenities")
                                if amenities:
                                    sections = amenities.get("sections", [])
                                    fare_details["included_features"] = {}
                                    for section in sections:
                                        heading = section.get("heading")
                                        if heading:
                                            fare_details["included_features"][heading] = []
                                            items = section.get("items")
                                            if items:
                                                for item in items:
                                                    primary = item.get("primary")
                                                    
                                                    text = primary.get("text")
                                                    graphic = item.get("graphic")
                                                    
                                                    if graphic:
                                                        fare_details["included_features"][heading].append({
                                                                "feature": text,
                                                                "icon": graphic.get("description")                                                     
                                                        })
                                offer['fare_options'].append(fare_details)

                flight_offers.append(offer)

        return flight_offers
        
    def _cleanup(self):
        """Clean up resources with OS-specific handling"""
        try:
            if self.display and platform.system() != "Windows":
                self.display.__exit__(None, None, None)
            if self.browser:
                self.browser.stop()
        except Exception as e:
            print(f"Error during cleanup: {str(e)}")


def main():
    # Example search URL
    sample_url = "https://www.expedia.com/Flights-Search?leg1=from:Zhengzhou (CGO - Zhengzhou),to:Nairobi (NBO-Jomo Kenyatta Intl.),departure:3/23/2025TANYT,fromType:A,toType:A&leg2=from:Nairobi (NBO-Jomo Kenyatta Intl.),to:Zhengzhou (CGO - Zhengzhou),departure:3/30/2025TANYT,fromType:A,toType:A&mode=search&options=carrier:,cabinclass:,maxhops:1,nopenalty:N&pageId=0&passengers=adults:1,children:0,infantinlap:N&trip=roundtrip"
    
    # Create output directory if it doesn't exist
    output_dir = "flight_results"
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    
    # Generate output filename with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = os.path.join(output_dir, f"flights_{timestamp}.json")
    
    # Initialize and run the scraper
    scraper = ExpediaFlightScraper(
        search_url=sample_url, 
        output_file=output_file, 
    )
    
    results = scraper.start()
    
    # Display summary
    if results:
        print(f"\nScraping completed successfully!")
        print(f"Found {len(results)} flight offers")
        print(f"Results saved to {output_file}")
    else:
        print("\nScraping completed with no results or errors occurred.")


if __name__ == "__main__":
    main()