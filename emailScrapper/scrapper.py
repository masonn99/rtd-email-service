import requests
from bs4 import BeautifulSoup
import time
import json
from typing import Dict, List
from urllib.parse import urljoin

class EmbassyScraper:
    def __init__(self):
        self.base_url = "https://travel.state.gov"
        self.main_page = "/content/travel/en/consularnotification/ConsularNotificationandAccess.html"
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        self.embassy_data = {}

    def get_country_links(self) -> List[Dict]:
        """Get country links from the sidebar menu."""
        try:
            response = requests.get(urljoin(self.base_url, self.main_page), headers=self.headers)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Find all links in the country list menu
            country_links = soup.select("div.tsg-rwd-side-menu-frame a")
            countries = []
            
            for link in country_links:
                if not link.get('href'):
                    continue
                    
                country = {
                    'name': link.text.strip(),
                    'url': urljoin(self.base_url, link['href'])
                }
                countries.append(country)
                print(f"Found country: {country['name']}")
                
            return countries

        except Exception as e:
            print(f"Error getting country links: {e}")
            return []

    def extract_embassy_emails(self, soup: BeautifulSoup) -> Dict:
        """Extract email information from a country page."""
        try:
            emails = []
            
            # Find the contact information section
            contact_div = soup.find("div", class_="tsg-rwd-consular-notifications-fram-for-info")
            if not contact_div:
                return {"error": "Contact section not found"}

            # Find all mailto links
            email_links = contact_div.find_all("a", href=lambda href: href and "mailto:" in href)
            
            for link in email_links:
                email = link["href"].replace("mailto:", "").strip()
                if email and email not in emails:
                    emails.append(email)
            
            return {"emails": emails} if emails else {"error": "No embassy emails found"}

        except Exception as e:
            return {"error": str(e)}

    def scrape_country(self, country: Dict) -> None:
        """Scrape email information for a single country."""
        try:
            print(f"\nProcessing {country['name']}...")
            response = requests.get(country['url'], headers=self.headers)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            result = self.extract_embassy_emails(soup)
            
            self.embassy_data[country['name']] = result
            print(f"Results for {country['name']}: {result}")
            
            time.sleep(2)  # Respectful delay
            
        except Exception as e:
            self.embassy_data[country['name']] = {"error": str(e)}
            print(f"Error processing {country['name']}: {e}")

    def scrape_all_countries(self):
        """Scrape email information for all countries."""
        countries = self.get_country_links()
        print(f"\nFound {len(countries)} countries to process")
        
        for country in countries:
            self.scrape_country(country)

        # Save to JSON
        with open('embassy_emails.json', 'w', encoding='utf-8') as f:
            json.dump(self.embassy_data, f, indent=2, ensure_ascii=False)
        
        return self.embassy_data


scraper = EmbassyScraper()
print("Starting embassy email scraper...")
data = scraper.scrape_all_countries()
print(f"\nScraped {len(data)} countries successfully!")
print("\nData has been saved to embassy_emails.json")