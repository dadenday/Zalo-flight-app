"""Worker for monitoring individual flights."""

import logging
import re
import time
from queue import Queue

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException, TimeoutException

from .base import BaseWorker
from .utils import format_time_to_minutes

logger = logging.getLogger(__name__)


class FlightWorker(BaseWorker):
    def __init__(self, flight_id, reporting_group, site_config, worker_driver, report_queue: Queue):
        super().__init__(task_id=flight_id, reporting_group=reporting_group, report_queue=report_queue)
        self.flight_id = flight_id.upper()
        self.config = site_config
        self.driver = worker_driver
        self.expected_url = f"{self.config['base_url']}/{self.flight_id.lower()}"

    def _verify_correct_page(self):
        try:
            current_url = self.driver.current_url.lower()
            if self.flight_id.lower() not in current_url:
                logger.warning(
                    f"[{self.name}] Page drift detected! Expected '{self.flight_id}', got '{current_url}'. Navigating back."
                )
                self.driver.get(self.expected_url)
                time.sleep(3)
            return True
        except Exception as e:
            logger.error(f"[{self.name}] Could not verify or correct page URL: {e}")
            return False

    def run(self):  # pragma: no cover - requires Selenium
        logger.info(f"[{self.name}] Starting to track {self.flight_id} on {self.config['name']}.")

        selectors = self.config['selectors']
        patterns = self.config['patterns']

        try:
            self.driver.get(self.expected_url)
            if selectors.get('cookie_accept'):
                try:
                    WebDriverWait(self.driver, 10).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, selectors['cookie_accept']))
                    ).click()
                except TimeoutException:
                    logger.debug(f"[{self.name}] No cookie banner found on {self.config['name']}.")
            WebDriverWait(self.driver, 25).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, selectors['time_remaining']))
            )
        except Exception as e:
            logger.error(f"[{self.name}] Failed to initialize worker for {self.flight_id}. Reason: {e}")
            self.submit_report(
                f"Không tìm thấy dữ liệu cho {self.flight_id.upper()} trên {self.config['name']}.",
                is_final=True,
            )
            return

        last_reported_minutes = -1
        expiration_minutes = self.config.get('flight_landed_expiration_minutes', 5)

        while not self.stop_event.is_set():
            if not self._verify_correct_page():
                time.sleep(15)
                continue

            try:
                status_element = self.driver.find_element(By.CSS_SELECTOR, selectors['time_remaining'])
                current_status_text = status_element.text.strip()
            except NoSuchElementException:
                logger.warning(f"[{self.name}] Status element not found for {self.flight_id}. Retrying.")
                time.sleep(10)
                continue

            arriving_match = re.search(patterns['arriving_in'], current_status_text, re.IGNORECASE)
            landed_match = re.search(patterns['landed_ago'], current_status_text, re.IGNORECASE)

            report_due = False
            status_message = ""

            if arriving_match:
                new_minutes = format_time_to_minutes(arriving_match.group(1))
                if new_minutes <= 5 and (last_reported_minutes - new_minutes >= 1 or last_reported_minutes > 5):
                    report_due = True
                elif 5 < new_minutes <= 30 and (last_reported_minutes - new_minutes >= 5 or last_reported_minutes > 30):
                    report_due = True
                elif new_minutes > 30 and (last_reported_minutes - new_minutes >= 10 or last_reported_minutes == -1):
                    report_due = True

                if report_due:
                    status_message = f"{self.flight_id} còn khoảng {new_minutes} phút hạ cánh."
                    last_reported_minutes = new_minutes

            elif landed_match:
                landed_minutes = format_time_to_minutes(landed_match.group(1))
                if landed_minutes >= expiration_minutes:
                    logger.info(
                        f"[{self.name}] Flight {self.flight_id} landed more than {expiration_minutes} minutes ago. Stopping task."
                    )
                    self.submit_report(f"{self.flight_id} kết thúc theo dõi.", is_final=True)
                    break
                if landed_minutes != last_reported_minutes:
                    status_message = f"{self.flight_id} đã đáp {landed_minutes} phút trước."
                    report_due = True
                    last_reported_minutes = landed_minutes
            else:
                logger.warning(f"[{self.name}] Could not parse status text: '{current_status_text}'")

            if report_due and status_message:
                self.submit_report(status_message)

            time.sleep(self.config.get('worker_loop_delay', 20))

        logger.info(f"[{self.name}] Worker for {self.flight_id} has finished.")

