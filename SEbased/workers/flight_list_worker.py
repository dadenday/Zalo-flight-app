"""Worker that scrapes live flight lists for an airline."""

import logging
import time
from queue import Queue

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from .base import BaseWorker

logger = logging.getLogger(__name__)


class FlightListWorker(BaseWorker):
    def __init__(self, airline_code, airport_code, reporting_group, site_config, worker_driver, report_queue: Queue):
        super().__init__(
            task_id=f"list-{airline_code}-{airport_code}",
            reporting_group=reporting_group,
            report_queue=report_queue,
        )
        self.airline_code = airline_code.upper()
        self.airport_code = airport_code.upper()
        self.config = site_config
        self.driver = worker_driver

    def run(self):  # pragma: no cover - requires Selenium
        logger.info(
            f"[{self.name}] Starting flight list scrape for {self.airline_code} arriving at {self.airport_code}."
        )
        try:
            self.driver.get("https://www.flightradar24.com")

            search_bar = WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input#search-input"))
            )
            search_bar.clear()
            search_bar.send_keys(self.airline_code)

            airline_xpath = f"//div[text()='Airlines']/following-sibling::div/a[contains(., '{self.airline_code}')]"
            airline_link = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, airline_xpath))
            )
            airline_link.click()

            live_flights_xpath = f"//a[contains(., 'Live {self.airline_code} flights')]"
            live_flights_link = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, live_flights_xpath))
            )
            live_flights_link.click()

            table_body_selector = "table#table-flighs-list > tbody"
            WebDriverWait(self.driver, 20).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, table_body_selector))
            )

            rows = self.driver.find_elements(By.CSS_SELECTOR, f"{table_body_selector} > tr")
            logger.info(
                f"[{self.name}] Found {len(rows)} total flights for {self.airline_code}. Filtering for arrivals at {self.airport_code}."
            )

            arriving_flights = []
            for row in rows:
                try:
                    cols = row.find_elements(By.TAG_NAME, "td")
                    route = cols[3].text
                    if f"- {self.airport_code}" in route:
                        flight_num = cols[1].text.strip()
                        if flight_num:
                            arriving_flights.append(
                                {
                                    "flight_id": flight_num,
                                    "aircraft": cols[2].text.strip(),
                                    "registration": cols[4].text.strip(),
                                    "route": route.replace('\n', ' ').strip(),
                                }
                            )
                except IndexError:
                    continue

            self.submit_report(arriving_flights, is_final=True)

        except Exception as e:  # pragma: no cover - requires Selenium
            logger.error(f"[{self.name}] Flight list worker failed: {e}", exc_info=True)
            self.submit_report([], is_final=True)

