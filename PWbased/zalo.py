# zalo.py

import time
import logging
import sys
from datetime import datetime, timedelta

from playwright.sync_api import Page, Locator, expect, Error as PlaywrightError

logger = logging.getLogger(__name__)

class ZaloManager:
    """Manages all interactions with the Zalo Web Application via Playwright."""
    
    def __init__(self, page: Page, selectors: dict, action_delay: float = 1.0):
        self.page = page
        self.selectors = selectors
        self.action_delay = action_delay
        # --- FIX: Add fallback state ---
        self._last_clicked_group = None

    # --- RE-IMPLEMENTED: Encapsulated login_and_wait method ---
    def login_and_wait(self, timeout=90000) -> bool:
        """Navigates to Zalo and waits for the main chat interface to load."""
        logger.info("Navigating to Zalo and verifying login status...")
        try:
            # Only navigate if we are not on the correct URL
            if "chat.zalo.me" not in self.page.url:
                self.page.goto("https://chat.zalo.me", wait_until="load", timeout=60000)
            
            self.page.wait_for_selector(self.selectors["logged_in_sentinel"], timeout=timeout)
            logger.info("Zalo is logged in and chat interface is ready.")
            return True
        except PlaywrightError:
            logger.error(f"Zalo did not load or was not logged in within {timeout/1000} seconds.")
            return False

    def get_active_group_name(self) -> str:
        try:
            header_locator = self.page.locator(self.selectors["header_title_active_group"])
            if header_locator.is_visible(timeout=1000):
                self._active_group_from_header = header_locator.inner_text()
                return self._active_group_from_header
        except PlaywrightError:
            # --- FIX: Return last known good group on failure ---
            logger.debug("Could not find header title; returning last clicked group as fallback.")
            return self._last_clicked_group
        return None

    def _click_group(self, group_title: str) -> bool:
        logger.debug(f"Attempting to click group: '{group_title}'")
        try:
            group_locator = self.page.locator(f'div.conv-item-title__name > div.truncate:has-text("{group_title}")').first
            group_locator.click(timeout=10000)
            expect(self.page.locator(self.selectors["header_title_active_group"])).to_have_text(group_title, timeout=5000)
            
            # --- FIX: Set last clicked group on success ---
            self._last_clicked_group = group_title
            
            logger.info(f"Successfully switched to group: '{group_title}'")
            time.sleep(self.action_delay)
            return True
        except Exception as e:
            logger.error(f"Failed to switch to group '{group_title}': {e}", exc_info=False)
            return False

    def _validate_last_message(self, expected_message: str) -> bool:
        time.sleep(1.5)
        try:
            if isinstance(expected_message, list):
                expected_message = expected_message[0]
            message_contents = self.page.locator(self.selectors["message_content"]).all_inner_texts()
            for content in reversed(message_contents[-5:]):
                if expected_message in content:
                    logger.info(f"Validation successful for message: '{expected_message}'")
                    return True
            logger.warning(f"VALIDATION FAILED: Could not find recently sent message '{expected_message}'")
            return False
        except Exception as e:
            logger.error(f"An error occurred during message validation: {e}")
            return False

    def send_message(self, group_title: str, message: str) -> bool:
        if self.get_active_group_name() != group_title:
            if not self._click_group(group_title): return False
        
        for attempt in range(2):
            try:
                full_message = "\n".join(message) if isinstance(message, list) else message
                
                # Use Playwright's built-in JS evaluation to use the browser's clipboard API.
                # This is more robust than using an external library like pyperclip.
                self.page.evaluate("text => navigator.clipboard.writeText(text)", full_message)
                
                input_locator = self.page.locator(self.selectors["chat_input"])
                input_locator.focus()
                
                # Simulate paste (use 'Meta' for macOS, 'Control' for Win/Linux)
                shortcut = "Meta+V" if sys.platform == "darwin" else "Control+V"
                self.page.keyboard.press(shortcut)
                
                self.page.keyboard.press("Enter")
                
                if self._validate_last_message(full_message): return True
                logger.warning(f"Attempt {attempt + 1} failed validation. Retrying.")
            except Exception as e:
                logger.error(f"Failed to send message to '{group_title}' on attempt {attempt + 1}: {e}")
                time.sleep(1)
        return False

    def read_new_messages(self, group_title: str, last_check_time: datetime) -> list:
        new_messages = []
        if self.get_active_group_name() != group_title:
            if not self._click_group(group_title): return new_messages

        try:
            all_messages = self.page.locator(self.selectors["message_element"]).all()[-15:]
            for msg_locator in reversed(all_messages):
                try:
                    content = msg_locator.locator(self.selectors["message_content"]).inner_text()
                    time_str = msg_locator.locator(self.selectors["message_time"]).inner_text()
                    if not content: continue
                    msg_hour, msg_minute = map(int, time_str.split(':'))
                    msg_time = datetime.now().replace(hour=msg_hour, minute=msg_minute, second=0, microsecond=0)
                    if msg_time > datetime.now() + timedelta(minutes=1): msg_time -= timedelta(days=1)
                    if msg_time <= last_check_time: break
                    new_messages.append({'content': content, 'timestamp': msg_time})
                except (PlaywrightError, ValueError): continue
            
            return sorted(new_messages, key=lambda x: x['timestamp'])
        except Exception as e:
            logger.error(f"Error reading messages from '{group_title}': {e}")
            return []

    def get_unread_group_names(self) -> list:
        unread_groups = set()
        try:
            group_items = self.page.locator(self.selectors["group_item"]).all()
            for item in group_items:
                for icon_selector in self.selectors["unread_notif_icons"]:
                    if item.locator(icon_selector).count() > 0:
                        name = item.locator(self.selectors["group_title_in_item"]).inner_text()
                        if name: unread_groups.add(name)
                        break
        except PlaywrightError as e:
            logger.warning(f"Could not scrape unread group names: {e}")
        return list(unread_groups)

    def navigate_to_group(self, group_title: str):
        if self.get_active_group_name() != group_title:
            logger.info(f"Returning to fallback group: '{group_title}'")
            self._click_group(group_title)

    def scrape_all_groups(self) -> list:
        all_groups = set()
        scroll_attempts, max_scrolls = 0, 20
        try:
            scroll_container = self.page.locator(self.selectors["logged_in_sentinel"])
            while scroll_attempts < max_scrolls:
                previous_count = len(all_groups)
                current_groups = self.page.locator(self.selectors["group_title_in_item"]).all_inner_texts()
                all_groups.update([name.strip() for name in current_groups if name.strip()])
                
                scroll_container.evaluate("node => node.scrollTop = node.scrollHeight")
                time.sleep(1.5)

                if len(all_groups) == previous_count:
                    logger.info(f"Scraping complete. Found {len(all_groups)} groups.")
                    break
                scroll_attempts += 1
            return list(all_groups)
        except PlaywrightError as e:
            logger.error(f"Could not scrape group list with scrolling: {e}")
            return list(all_groups)