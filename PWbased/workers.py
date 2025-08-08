# workers.py

import threading
import uuid
import time
import re
import logging
from datetime import datetime
from queue import Queue

from playwright.sync_api import BrowserContext, Page, Error as PlaywrightError
import requests
import fitz  # PyMuPDF

logger = logging.getLogger(__name__)

# --- Helper Functions (format_time_to_minutes, clean_text_for_summary) ---
# (These functions are unchanged and can be copied from the original files)
def format_time_to_minutes(time_str: str) -> int:
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
    if not text: return ''
    patterns_to_remove = [
        r'Copyright [^\n]+', r'All rights reserved', r'Sign in to start your session', r'\s{2,}',
    ]
    for pat in patterns_to_remove: text = re.sub(pat, ' ', text, flags=re.IGNORECASE)
    return text.strip()

class BaseWorker(threading.Thread):
    def __init__(self, task_id, reporting_group, report_queue: Queue):
        super().__init__()
        self.worker_id = str(uuid.uuid4())[:8]
        self.task_id = task_id
        self.reporting_group = reporting_group
        self.report_queue = report_queue
        self.stop_event = threading.Event()
        self.name = f"{self.__class__.__name__}-{self.worker_id}"

    def stop(self): self.stop_event.set()

    def submit_report(self, status, is_final=False):
        self.report_queue.put({
            "task_id": self.task_id, "reporting_group": self.reporting_group, "status": status,
            "timestamp": datetime.now().isoformat(), "is_final": is_final
        })

class FlightWorker(BaseWorker):
    # --- FIX: Accept BrowserContext, not Browser ---
    def __init__(self, flight_id, reporting_group, site_config, browser_context: BrowserContext, report_queue: Queue):
        super().__init__(task_id=flight_id, reporting_group=reporting_group, report_queue=report_queue)
        self.flight_id = flight_id.upper()
        self.config = site_config
        self.context = browser_context # Store the context
        self.expected_url = f"{self.config['base_url']}/{self.flight_id.lower()}"

    # --- RE-IMPLEMENTED: Dedicated page drift verification method ---
    def _verify_correct_page(self, page: Page):
        if self.flight_id.lower() not in page.url.lower():
            logger.warning(f"[{self.name}] Page drift detected! Correcting URL for {self.flight_id}.")
            page.goto(self.expected_url, wait_until="domcontentloaded")
            return False
        return True

    def run(self):
        logger.info(f"[{self.name}] Starting to track {self.flight_id} on {self.config['name']}.")
        page = self.context.new_page()
        try:
            selectors, patterns = self.config['selectors'], self.config['patterns']
            page.goto(self.expected_url, wait_until="domcontentloaded", timeout=30000)

            # --- FIX: Handle cookie consent banner ---
            cookie_locator = page.locator(selectors['cookie_accept'])
            try:
                cookie_locator.click(timeout=7000)
                logger.info(f"[{self.name}] Clicked cookie accept banner.")
            except PlaywrightError:
                logger.debug(f"[{self.name}] Cookie banner not found or not clickable.")
            
            status_locator = page.locator(selectors['time_remaining'])
            status_locator.wait_for(state="visible", timeout=25000)
            
            last_reported_minutes = -1
            expiration_minutes = self.config['settings'].get('flight_landed_expiration_minutes', 5)

            while not self.stop_event.is_set():
                self._verify_correct_page(page)
                status_locator.wait_for(state="visible", timeout=15000)

                current_status_text = status_locator.inner_text()
                arriving_match = re.search(patterns['arriving_in'], current_status_text, re.IGNORECASE)
                landed_match = re.search(patterns['landed_ago'], current_status_text, re.IGNORECASE)
                report_due, status_message = False, ""

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
                    
                    # --- THIS IS THE CORRECTED LOGIC BLOCK ---
                    if landed_minutes >= expiration_minutes:
                        logger.info(f"[{self.name}] Flight {self.flight_id} landed more than {expiration_minutes} minutes ago. Stopping task.")
                        # RE-ADDED: The missing final report to the user.
                        self.submit_report(f"{self.flight_id} kết thúc theo dõi.", is_final=True)
                        break # Exit the loop
                        
                    if landed_minutes != last_reported_minutes:
                        status_message = f"{self.flight_id} đã đáp {landed_minutes} phút trước."
                        report_due, last_reported_minutes = True, landed_minutes
                
                if report_due and status_message: self.submit_report(status_message)
                
                # Use a more responsive sleep that checks the stop event
                for _ in range(self.config['settings'].get('worker_loop_delay', 20)):
                    if self.stop_event.is_set(): break
                    time.sleep(1)

        except Exception as e:
            # --- FIX: Add site name to error message ---
            logger.error(f"[{self.name}] Worker failed for {self.flight_id}: {e}", exc_info=True)
            error_message = f"Không tìm thấy dữ liệu cho {self.flight_id.upper()} trên {self.config['name']}."
            self.submit_report(error_message, is_final=True)
        finally:
            logger.info(f"[{self.name}] Worker finished. Closing page.")
            page.close()

class FlightListWorker(BaseWorker):
    # --- FIX: Accept BrowserContext ---
    def __init__(self, airline_code, airport_code, reporting_group, site_config, browser_context: BrowserContext, report_queue: Queue):
        super().__init__(task_id=f"list-{airline_code}-{airport_code}", reporting_group=reporting_group, report_queue=report_queue)
        self.airline_code, self.airport_code = airline_code.upper(), airport_code.upper()
        self.config, self.context = site_config, browser_context

    def run(self):
        logger.info(f"[{self.name}] Starting flight list scrape for {self.airline_code} to {self.airport_code}.")
        page = self.context.new_page()
        try:
            # --- RE-IMPLEMENTED: Generalized, config-driven search workflow ---
            selectors = self.config['flight_list_selectors']
            page.goto(self.config['base_url'], timeout=30000)
            
            page.locator(selectors['search_box']).fill(self.airline_code)
            
            airline_xpath = selectors['airline_result_link_xpath'].format(airline_code=self.airline_code)
            page.locator(f"xpath={airline_xpath}").click()
            
            live_flights_xpath = selectors['live_flights_link_xpath'].format(airline_code=self.airline_code)
            page.locator(f"xpath={live_flights_xpath}").click()

            page.wait_for_selector(selectors['data_table_body'], timeout=20000)
            rows = page.locator(selectors['data_table_rows']).all()
            
            arriving_flights = []
            for row_locator in rows:
                try:
                    route = row_locator.locator(selectors['route_in_row']).inner_text()
                    if f" {self.airport_code}" in route:
                        flight_num = row_locator.locator(selectors['flight_number_in_row']).inner_text().strip()
                        if flight_num:
                            arriving_flights.append({
                                "flight_id": flight_num,
                                "aircraft": row_locator.locator(selectors['aircraft_in_row']).inner_text().strip(),
                                "registration": row_locator.locator(selectors['registration_in_row']).inner_text().strip(),
                                "route": route.replace('\n', ' ').strip()
                            })
                except (PlaywrightError, IndexError): continue

            self.submit_report(arriving_flights, is_final=True)

        except Exception as e:
            logger.error(f"[{self.name}] Flight list worker failed: {e}", exc_info=True)
            self.submit_report([], is_final=True)
        finally:
            page.close()

# --- RE-IMPLEMENTED: CargoWorker using Playwright ---
class CargoWorker(BaseWorker):
    # --- FIX: Accept BrowserContext ---
    def __init__(self, flight_id, task_type, reporting_group, cargo_config, browser_context: BrowserContext, report_queue: Queue):
        super().__init__(task_id=f"cargo-{flight_id}-{task_type}", reporting_group=reporting_group, report_queue=report_queue)
        self.flight_id, self.task_type, self.config, self.context = flight_id, task_type, cargo_config, browser_context

    def _get_file_text_from_url(self, page: Page, url: str) -> str:
        if not url or "javascript:void(0)" in url: return ""
        try:
            # Use browser context's cookies for the requests session
            cookies = page.context.cookies()
            s = requests.Session()
            for cookie in cookies:
                s.cookies.set(cookie['name'], cookie['value'], domain=cookie['domain'])
            
            response = s.get(url, timeout=20)
            response.raise_for_status()
            content = response.content

            if url.endswith('.html') or b'<html' in content[:1000]:
                text = fitz.open("html", content).get_text()
            else:
                text = fitz.open("pdf", content).get_text()
            # --- FIX: Add missing text cleaning step ---
            return clean_text_for_summary(text)
        except Exception as e:
            logger.error(f"[{self.name}] Failed to download or parse file from {url}: {e}")
            return ""

    def _analyze_cargo_data(self, msg_text, loadsheet_text, manifest_text):
        bc_pattern = r'B(?P<bag_pcs>\d+)/(?P<bag_wgt>\d+)\s+C(?P<cargo_pcs>\d+)/(?P<cargo_wgt>\d+)'
        match = re.search(bc_pattern, msg_text)
        if match:
            data = match.groupdict()
            return [f"TÓM TẮT TẢI CHO {self.flight_id.upper()}:",
                    f"- Hành lý (B): {data['bag_pcs']} kiện, {data['bag_wgt']} kg",
                    f"- Hàng hóa (C): {data['cargo_pcs']} kiện, {data['cargo_wgt']} kg"]
        return [f"Không tìm thấy dữ liệu tải chi tiết (B/C) cho {self.flight_id.upper()}."]

    def run(self):
        logger.info(f"[{self.name}] Starting browser-based task '{self.task_type}' for cargo flight {self.flight_id}.")
        page = self.context.new_page()
        try:
            selectors, creds = self.config['selectors'], self.config['credentials']
            page.goto(self.config['login_url'], timeout=30000)
            
            if not page.locator(selectors['post_login_check']).is_visible():
                logger.info(f"[{self.name}] Logging into cargo system...")
                page.locator(selectors['username_field']).fill(creds['username'])
                page.locator(selectors['password_field']).fill(creds['password'])
                page.locator(selectors['login_button']).click()
            page.wait_for_selector(selectors['post_login_check'], timeout=15000)
            
            search_input = page.locator(selectors['search_input'])
            search_input.fill(self.flight_id)
            search_input.press('Enter')
            
            first_row = page.locator(selectors['first_flight_row'])
            first_row.wait_for(state="visible", timeout=20000)
            
            if self.task_type == 'info':
                headers = page.locator(selectors['flight_table_header'] + " > th").all_inner_texts()
                cells = first_row.locator('td').all_inner_texts()
                info_lines = [f"Thông tin chuyến bay {self.flight_id.upper()}:"]
                info_lines.extend(f"- {h}: {c}" for h, c in zip(headers, cells) if h and c and "Msg" not in h)
                self.submit_report(info_lines, is_final=True)
            elif self.task_type == 'summary':
                first_row.click()
                msg_text = page.locator(selectors['message_of_flight_textarea']).input_value()
                
                page.locator(selectors['uploaded_files_tab']).click()
                loadsheet_url = page.locator(selectors['loadsheet_link']).get_attribute('href')
                manifest_url = page.locator(selectors['cargo_manifest_link']).get_attribute('href')
                
                base_url = self.config['login_url'].split('/')[0] + '//' + self.config['login_url'].split('/')[2]
                loadsheet_text = self._get_file_text_from_url(page, base_url + loadsheet_url)
                manifest_text = self._get_file_text_from_url(page, base_url + manifest_url)
                
                summary = self._analyze_cargo_data(msg_text, loadsheet_text, manifest_text)
                self.submit_report(summary, is_final=True)
        except Exception as e:
            logger.error(f"[{self.name}] Cargo worker failed: {e}", exc_info=True)
            self.submit_report(f"Đã xảy ra lỗi khi lấy thông tin cho {self.flight_id}.", is_final=True)
        finally:
            page.close()