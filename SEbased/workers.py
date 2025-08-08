# workers.py (Add the new FlightListWorker class to the end of the file)

import threading
import uuid
import time
import re
import json
import logging
import requests
import pathlib
import io
from PIL import Image
import google.generativeai as genai
from datetime import datetime
from queue import Queue
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

# --- MODIFIED IMPORTS ---
import io
from pypdf import PdfReader # Use pypdf instead of fitz
from bs4 import BeautifulSoup   # Use BeautifulSoup for HTML

logger = logging.getLogger(__name__)

# --- Helper Functions ---
def format_time_to_minutes(time_str: str) -> int:
    """Parses time strings like 'in 25 min' or '1h 5m' into total minutes."""
    time_str = time_str.lower()
    total_minutes = 0
    try:
        if 'h' in time_str:
            parts = time_str.replace('in', '').strip().split('h')
            total_minutes += int(parts[0]) * 60
            if len(parts) > 1 and parts[1]:
                m_part = re.search(r'(\d+)', parts[1])
                if m_part: total_minutes += int(m_part.group(1))
        elif 'm' in time_str:
            m_part = re.search(r'(\d+)', time_str)
            if m_part: total_minutes = int(m_part.group(1))
        elif ':' in time_str:
            parts = time_str.split(':')
            total_minutes = int(parts[0]) * 60 + int(parts[1])
        else: # Assume it's just a number of minutes
            m_part = re.search(r'(\d+)', time_str)
            if m_part: total_minutes = int(m_part.group(1))
        return total_minutes
    except (ValueError, IndexError):
        return 0

def clean_text_for_summary(text: str) -> str:
    """Removes common boilerplate and unwanted text from downloaded files."""
    if not text: return ''
    patterns_to_remove = [
        r'Copyright [^\n]+',
        r'All rights reserved',
        r'Sign in to start your session',
        r'\s{2,}', # Collapse whitespace
    ]
    for pat in patterns_to_remove:
        text = re.sub(pat, ' ', text, flags=re.IGNORECASE)
    return text.strip()

# --- Base Worker Class ---
class BaseWorker(threading.Thread):
    def __init__(self, task_id, reporting_group, report_queue: Queue):
        super().__init__()
        self.worker_id = str(uuid.uuid4())[:8]
        self.task_id = task_id # e.g., flight code or "cargo-VJC123"
        self.reporting_group = reporting_group
        self.report_queue = report_queue
        self.stop_event = threading.Event()
        self.name = f"{self.__class__.__name__}-{self.worker_id}"

    def stop(self):
        self.stop_event.set()

    def submit_report(self, status: str, is_final=False):
        """Puts a formatted report into the shared queue."""
        report = {
            "task_id": self.task_id,
            "reporting_group": self.reporting_group,
            "status": status,
            "timestamp": datetime.now().isoformat(),
            "is_final": is_final
        }
        self.report_queue.put(report)

# --- Flight Worker ---
class FlightWorker(BaseWorker):
    def __init__(self, flight_id, reporting_group, site_config, worker_driver: WebDriver, report_queue: Queue):
        super().__init__(task_id=flight_id, reporting_group=reporting_group, report_queue=report_queue)
        self.flight_id = flight_id.upper()
        self.config = site_config
        self.driver = worker_driver
        self.expected_url = f"{self.config['base_url']}/{self.flight_id.lower()}"

    def _verify_correct_page(self):
        """Ensures the browser is on the correct flight page, correcting if necessary."""
        try:
            current_url = self.driver.current_url.lower()
            if self.flight_id.lower() not in current_url:
                logger.warning(f"[{self.name}] Page drift detected! Expected '{self.flight_id}', got '{current_url}'. Navigating back.")
                self.driver.get(self.expected_url)
                time.sleep(3) # Wait for redirect and load
            return True
        except Exception as e:
            logger.error(f"[{self.name}] Could not verify or correct page URL: {e}")
            return False

    def run(self):
        logger.info(f"[{self.name}] Starting to track {self.flight_id} on {self.config['name']}.")
        
        selectors = self.config['selectors']
        patterns = self.config['patterns']
        
        try:
            self.driver.get(self.expected_url)
            if selectors.get('cookie_accept'):
                try:
                    WebDriverWait(self.driver, 10).until(EC.element_to_be_clickable((By.CSS_SELECTOR, selectors['cookie_accept']))).click()
                except TimeoutException:
                    logger.debug(f"[{self.name}] No cookie banner found on {self.config['name']}.")
            WebDriverWait(self.driver, 25).until(EC.presence_of_element_located((By.CSS_SELECTOR, selectors['time_remaining'])))
        except Exception as e:
            logger.error(f"[{self.name}] Failed to initialize worker for {self.flight_id}. Reason: {e}")
            self.submit_report(f"Không tìm thấy dữ liệu cho {self.flight_id.upper()} trên {self.config['name']}.", is_final=True)
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
                if new_minutes <= 5 and (last_reported_minutes - new_minutes >= 1 or last_reported_minutes > 5): report_due = True
                elif 5 < new_minutes <= 30 and (last_reported_minutes - new_minutes >= 5 or last_reported_minutes > 30): report_due = True
                elif new_minutes > 30 and (last_reported_minutes - new_minutes >= 10 or last_reported_minutes == -1): report_due = True
                
                if report_due:
                    status_message = f"{self.flight_id} còn khoảng {new_minutes} phút hạ cánh."
                    last_reported_minutes = new_minutes

            elif landed_match:
                landed_minutes = format_time_to_minutes(landed_match.group(1))

                # --- NEW LOGIC ---
                # 1. Check for task expiration first. This is a terminal condition.
                if landed_minutes >= expiration_minutes:
                    logger.info(f"[{self.name}] Flight {self.flight_id} landed more than {expiration_minutes} minutes ago. Stopping task.")
                    self.submit_report(f"{self.flight_id} kết thúc theo dõi.", is_final=True)
                    break  # Exit the monitoring loop

                # 2. If not expired, check if we need to send a periodic "Landed X min ago" update.
                # This condition is met for the first "Landed" report and every minute thereafter.
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


# --- Cargo Worker ---
class CargoWorker(BaseWorker):
    def __init__(self, flight_id, task_type, reporting_group, cargo_config, worker_driver: WebDriver, report_queue: Queue):
        super().__init__(task_id=f"cargo-{flight_id}", reporting_group=reporting_group, report_queue=report_queue)
        self.flight_id = flight_id
        self.task_type = task_type # 'info' or 'summary'
        self.config = cargo_config
        self.driver = worker_driver

    def _get_file_text_from_url(self, url: str) -> str:
        """Downloads a file (PDF/HTML) using the browser's session cookies and extracts text."""
        if not url or "javascript:void(0)" in url: return ""
        
        try:
            session = requests.Session()
            for cookie in self.driver.get_cookies():
                session.cookies.set(cookie['name'], cookie['value'])
            
            response = session.get(url, timeout=20)
            response.raise_for_status()
            content = response.content
            text = ""

            # Check if the content is likely HTML first
            if url.endswith('.html') or b'<html' in content[:1000]:
                # Use BeautifulSoup to parse HTML and get text
                soup = BeautifulSoup(content, 'lxml')
                text = soup.get_text(separator='\n', strip=True)
            else:
                # Otherwise, assume it's a PDF and use pypdf
                try:
                    pdf_reader = PdfReader(io.BytesIO(content))
                    text_parts = [page.extract_text() for page in pdf_reader.pages]
                    text = "\n".join(text_parts)
                except Exception as pdf_error:
                    logger.error(f"[{self.name}] Failed to parse content as PDF from {url}: {pdf_error}")
                    return "" # Return empty if PDF parsing fails
            
            return clean_text_for_summary(text)
        except Exception as e:
            logger.error(f"[{self.name}] Failed to download or parse file from {url}: {e}")
            return ""

    def _analyze_cargo_data(self, msg_text, loadsheet_text, manifest_text):
        """Extracts and summarizes cargo and baggage data."""
        bc_pattern = r'B(?P<bag_pcs>\d+)/(?P<bag_wgt>\d+)\s+C(?P<cargo_pcs>\d+)/(?P<cargo_wgt>\d+)'
        match = re.search(bc_pattern, msg_text)
        if match:
            data = match.groupdict()
            return [
                f"TÓM TẮT TẢI CHO {self.flight_id.upper()}:",
                f"- Hành lý (B): {data['bag_pcs']} kiện, {data['bag_wgt']} kg",
                f"- Hàng hóa (C): {data['cargo_pcs']} kiện, {data['cargo_wgt']} kg"
            ]
        return [f"Không tìm thấy dữ liệu tải chi tiết (B/C) cho {self.flight_id.upper()} trong tin nhắn."]

    def run(self):
        logger.info(f"[{self.name}] Starting task '{self.task_type}' for cargo flight {self.flight_id}.")
        
        selectors = self.config['selectors']
        creds = self.config['credentials']
        
        try:
            self.driver.get(self.config['login_url'])
            time.sleep(2)
            if selectors.get('post_login_check') and self.driver.find_elements(By.CSS_SELECTOR, selectors['post_login_check']):
                 logger.info(f"[{self.name}] Already logged into cargo system.")
            else:
                logger.info(f"[{self.name}] Logging into cargo system...")
                self.driver.find_element(By.CSS_SELECTOR, selectors['username_field']).send_keys(creds['username'])
                self.driver.find_element(By.CSS_SELECTOR, selectors['password_field']).send_keys(creds['password'])
                self.driver.find_element(By.CSS_SELECTOR, selectors['login_button']).click()
                WebDriverWait(self.driver, 15).until(EC.presence_of_element_located((By.CSS_SELECTOR, selectors['post_login_check'])))
            
            search_input = WebDriverWait(self.driver, 15).until(EC.presence_of_element_located((By.CSS_SELECTOR, selectors['search_input'])))
            search_input.clear()
            search_input.send_keys(self.flight_id)
            search_input.send_keys(Keys.RETURN)
            time.sleep(3)

            first_row = WebDriverWait(self.driver, 20).until(EC.element_to_be_clickable((By.CSS_SELECTOR, selectors['first_flight_row'])))
            
            if self.task_type == 'info':
                headers = [h.text for h in self.driver.find_elements(By.CSS_SELECTOR, selectors['flight_table_header'] + " > th")]
                cells = [c.text for c in first_row.find_elements(By.TAG_NAME, 'td')]
                info_lines = [f"Thông tin chuyến bay {self.flight_id.upper()}:"]
                info_lines.extend(f"- {h}: {c}" for h, c in zip(headers, cells) if h and c and "Msg" not in h)
                self.submit_report(info_lines, is_final=True)
            
            elif self.task_type == 'summary':
                first_row.click()
                time.sleep(2)
                
                msg_textarea = WebDriverWait(self.driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, selectors['message_of_flight_textarea'])))
                msg_text = msg_textarea.get_attribute('value')

                self.driver.find_element(By.CSS_SELECTOR, selectors['uploaded_files_tab']).click()
                time.sleep(2)
                loadsheet_url = self.driver.find_element(By.CSS_SELECTOR, selectors['loadsheet_link']).get_attribute('href')
                manifest_url = self.driver.find_element(By.CSS_SELECTOR, selectors['cargo_manifest_link']).get_attribute('href')
                
                loadsheet_text = self._get_file_text_from_url(loadsheet_url)
                manifest_text = self._get_file_text_from_url(manifest_url)
                
                summary = self._analyze_cargo_data(msg_text, loadsheet_text, manifest_text)
                self.submit_report(summary, is_final=True)

        except Exception as e:
            logger.error(f"[{self.name}] Cargo worker failed: {e}", exc_info=True)
            self.submit_report(f"Đã xảy ra lỗi khi lấy thông tin cho {self.flight_id}.", is_final=True)
            
# --- NEW: Worker for scraping flight lists ---
class FlightListWorker(BaseWorker):
    def __init__(self, airline_code, airport_code, reporting_group, site_config, worker_driver: WebDriver, report_queue: Queue):
        super().__init__(task_id=f"list-{airline_code}-{airport_code}", reporting_group=reporting_group, report_queue=report_queue)
        self.airline_code = airline_code.upper()
        self.airport_code = airport_code.upper()
        self.config = site_config
        self.driver = worker_driver

    def run(self):
        logger.info(f"[{self.name}] Starting flight list scrape for {self.airline_code} arriving at {self.airport_code}.")
        try:
            self.driver.get("https://www.flightradar24.com")
            
            search_bar = WebDriverWait(self.driver, 15).until(EC.presence_of_element_located((By.CSS_SELECTOR, "input#search-input")))
            search_bar.clear()
            search_bar.send_keys(self.airline_code)
            
            airline_xpath = f"//div[text()='Airlines']/following-sibling::div/a[contains(., '{self.airline_code}')]"
            airline_link = WebDriverWait(self.driver, 10).until(EC.element_to_be_clickable((By.XPATH, airline_xpath)))
            airline_link.click()
            
            live_flights_xpath = f"//a[contains(., 'Live {self.airline_code} flights')]"
            live_flights_link = WebDriverWait(self.driver, 10).until(EC.element_to_be_clickable((By.XPATH, live_flights_xpath)))
            live_flights_link.click()

            table_body_selector = "table#table-flighs-list > tbody"
            WebDriverWait(self.driver, 20).until(EC.presence_of_element_located((By.CSS_SELECTOR, table_body_selector)))
            
            rows = self.driver.find_elements(By.CSS_SELECTOR, f"{table_body_selector} > tr")
            logger.info(f"[{self.name}] Found {len(rows)} total flights for {self.airline_code}. Filtering for arrivals at {self.airport_code}.")
            
            arriving_flights = []
            for row in rows:
                try:
                    cols = row.find_elements(By.TAG_NAME, "td")
                    route = cols[3].text
                    
                    if f"- {self.airport_code}" in route:
                        flight_num = cols[1].text.strip()
                        # --- MODIFICATION: Store structured data, not a formatted string ---
                        if flight_num: # Ensure we have a flight number
                            arriving_flights.append({
                                "flight_id": flight_num,
                                "aircraft": cols[2].text.strip(),
                                "registration": cols[4].text.strip(),
                                "route": route.replace('\n', ' ').strip()
                            })
                except IndexError:
                    continue

            # --- MODIFICATION: Submit the raw list, not a formatted message ---
            self.submit_report(arriving_flights, is_final=True)

        except Exception as e:
            logger.error(f"[{self.name}] Flight list worker failed: {e}", exc_info=True)
            # Submit an empty list on failure
            self.submit_report([], is_final=True)

# --- NEW: The Gemini-powered OCR Worker ---
class OCRWorker(BaseWorker):
    def __init__(self, image_bytes: bytes, reporting_group: str, api_key: str, command_queue: Queue, report_queue: Queue):
        super().__init__(task_id=f"ocr-{uuid.uuid4().hex[:8]}", reporting_group=reporting_group, report_queue=report_queue)
        self.image_bytes = image_bytes
        self.command_queue = command_queue
        self.api_key = api_key

    def run(self):
        logger.info(f"[{self.name}] Starting Gemini API processing for an image from group '{self.reporting_group}'.")
        try:
            genai.configure(api_key=self.api_key)

            # Prepare the image for the API
            img = Image.open(io.BytesIO(self.image_bytes))
            
            # --- The Magic Prompt ---
            # We instruct the model on exactly what to do and how to format the output.
            prompt = """
            You are an expert OCR system designed to parse flight schedule tables from images.
            Analyze the image and extract all flight data rows you can find.
            
            Your response MUST be a valid JSON array of objects.
            Each object in the array should represent one flight row and must have the following keys:
            - "arrival_flight": The primary flight number. If there are two (e.g., VJ123/VJ456), this is the first one.
            - "departure_flight": The second flight number if it exists, otherwise this should be null.
            - "route": The flight route (e.g., "HAN-SGN").
            - "aircraft": The aircraft code (e.g., "VN-A630").
            - "team": The text in the final column, representing the assigned team.

            Example of a valid response for an image with two flight rows:
            [
              {
                "arrival_flight": "VJ1605",
                "departure_flight": "VJ392",
                "route": "CXR-SGN-PXU",
                "aircraft": "VN-A644",
                "team": "THẮNG 3"
              },
              {
                "arrival_flight": "VJ190",
                "departure_flight": null,
                "route": "SGN-HAN",
                "aircraft": "VN-A647",
                "team": "Nam 3+phong 3"
              }
            ]

            If you cannot find any valid flight rows, return an empty array [].
            """

            # Select the model and generate the content
            model = genai.GenerativeModel('gemini-1.5-flash-latest')
            response = model.generate_content([prompt, img])
            
            # Clean up the response to extract the JSON
            cleaned_response = response.text.strip().replace("```json", "").replace("```", "")
            
            # Parse the JSON response
            flights = json.loads(cleaned_response)

            if not flights:
                logger.warning(f"[{self.name}] Gemini processed the image but found no valid flight data.")
                return

            # --- Workflow Logic (same as before, but now with clean data) ---
            if len(flights) == 1:
                flight = flights[0]
                arrival = flight.get('arrival_flight')
                departure = flight.get('departure_flight')
                
                if not arrival: return # Skip if the core flight number is missing

                self.submit_report(f"Phát hiện 1 chuyến bay: {arrival}. Tự động kích hoạt theo dõi.")
                
                self.command_queue.put({'command': f'ai check {arrival}', 'group': self.reporting_group})
                self.command_queue.put({'command': f'ai cargo {arrival} --summary', 'group': self.reporting_group})
                if departure:
                    self.command_queue.put({'command': f'ai cargo {departure} --summary', 'group': self.reporting_group})
            else:
                report_lines = ["Phát hiện nhiều chuyến bay. Vui lòng chọn đội để xử lý:"]
                for flight in flights:
                    # Use .get() to avoid errors if a key is missing
                    arrival_flight = flight.get('arrival_flight', 'N/A')
                    team = flight.get('team', 'N/A')
                    report_lines.append(f"- Chuyến bay: {arrival_flight}, Đội: {team}")
                
                self.submit_report("\n".join(report_lines))

        except json.JSONDecodeError:
             logger.error(f"[{self.name}] Gemini API did not return valid JSON. Response:\n{response.text}")
             self.submit_report("Lỗi: Không thể phân tích dữ liệu từ hình ảnh.")
        except Exception as e:
            logger.error(f"[{self.name}] Gemini worker failed: {e}", exc_info=True)
            self.submit_report("Đã xảy ra lỗi khi xử lý hình ảnh qua API.")
        finally:
            logger.info(f"[{self.name}] Gemini processing finished.")