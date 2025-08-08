"""Worker for interacting with VietJet cargo system."""

import io
import logging
import re
import time
from queue import Queue

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from .base import BaseWorker
from .utils import clean_text_for_summary

logger = logging.getLogger(__name__)


class CargoWorker(BaseWorker):
    def __init__(self, flight_id, task_type, reporting_group, cargo_config, worker_driver, report_queue: Queue):
        super().__init__(task_id=f"cargo-{flight_id}", reporting_group=reporting_group, report_queue=report_queue)
        self.flight_id = flight_id
        self.task_type = task_type  # 'info' or 'summary'
        self.config = cargo_config
        self.driver = worker_driver

    def _get_file_text_from_url(self, url: str) -> str:  # pragma: no cover - network
        if not url or "javascript:void(0)" in url:
            return ""

        try:
            session = requests.Session()
            for cookie in self.driver.get_cookies():
                session.cookies.set(cookie['name'], cookie['value'])

            response = session.get(url, timeout=20)
            response.raise_for_status()
            content = response.content
            text = ""

            if url.endswith('.html') or b'<html' in content[:1000]:
                soup = BeautifulSoup(content, 'lxml')
                text = soup.get_text(separator='\n', strip=True)
            else:
                try:
                    pdf_reader = PdfReader(io.BytesIO(content))
                    text_parts = [page.extract_text() for page in pdf_reader.pages]
                    text = "\n".join(text_parts)
                except Exception as pdf_error:  # pragma: no cover - defensive
                    logger.error(f"[{self.name}] Failed to parse content as PDF from {url}: {pdf_error}")
                    return ""

            return clean_text_for_summary(text)
        except Exception as e:  # pragma: no cover - network
            logger.error(f"[{self.name}] Failed to download or parse file from {url}: {e}")
            return ""

    def _analyze_cargo_data(self, msg_text, loadsheet_text, manifest_text):
        bc_pattern = r'B(?P<bag_pcs>\d+)/(?P<bag_wgt>\d+)\s+C(?P<cargo_pcs>\d+)/(?P<cargo_wgt>\d+)'
        match = re.search(bc_pattern, msg_text)
        if match:
            data = match.groupdict()
            return [
                f"TÓM TẮT TẢI CHO {self.flight_id.upper()}:",
                f"- Hành lý (B): {data['bag_pcs']} kiện, {data['bag_wgt']} kg",
                f"- Hàng hóa (C): {data['cargo_pcs']} kiện, {data['cargo_wgt']} kg",
            ]
        return [f"Không tìm thấy dữ liệu tải chi tiết (B/C) cho {self.flight_id.upper()} trong tin nhắn."]

    def run(self):  # pragma: no cover - requires Selenium
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
                WebDriverWait(self.driver, 15).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, selectors['post_login_check']))
                )

            search_input = WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, selectors['search_input']))
            )
            search_input.clear()
            search_input.send_keys(self.flight_id)
            search_input.send_keys(Keys.RETURN)
            time.sleep(3)

            first_row = WebDriverWait(self.driver, 20).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, selectors['first_flight_row']))
            )

            if self.task_type == 'info':
                headers = [h.text for h in self.driver.find_elements(By.CSS_SELECTOR, selectors['flight_table_header'] + " > th")]
                cells = [c.text for c in first_row.find_elements(By.TAG_NAME, 'td')]
                info_lines = [f"Thông tin chuyến bay {self.flight_id.upper()}:"]
                info_lines.extend(f"- {h}: {c}" for h, c in zip(headers, cells) if h and c and "Msg" not in h)
                self.submit_report(info_lines, is_final=True)

            elif self.task_type == 'summary':
                first_row.click()
                time.sleep(2)
                msg_textarea = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, selectors['message_of_flight_textarea']))
                )
                msg_text = msg_textarea.get_attribute('value')

                self.driver.find_element(By.CSS_SELECTOR, selectors['uploaded_files_tab']).click()
                time.sleep(2)
                loadsheet_url = self.driver.find_element(By.CSS_SELECTOR, selectors['loadsheet_link']).get_attribute('href')
                manifest_url = self.driver.find_element(By.CSS_SELECTOR, selectors['cargo_manifest_link']).get_attribute('href')

                loadsheet_text = self._get_file_text_from_url(loadsheet_url)
                manifest_text = self._get_file_text_from_url(manifest_url)

                summary = self._analyze_cargo_data(msg_text, loadsheet_text, manifest_text)
                self.submit_report(summary, is_final=True)

        except Exception as e:  # pragma: no cover - requires Selenium
            logger.error(f"[{self.name}] Cargo worker failed: {e}", exc_info=True)
            self.submit_report(
                f"Đã xảy ra lỗi khi lấy thông tin cho {self.flight_id}.", is_final=True
            )

