# zalo.py

import time
import logging
import requests
import pyperclip
from datetime import datetime, timedelta
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    ElementNotInteractableException
)

logger = logging.getLogger(__name__)

class ZaloManager:
    """Manages all interactions with the Zalo Web Application via Selenium."""
    
# --- MODIFIED: __init__ now accepts selectors ---
    def __init__(self, driver: WebDriver, selectors: dict, action_delay: float = 1.0):
        self.driver = driver
        self.selectors = selectors # Store the selectors
        self.action_delay = action_delay
        self._last_clicked_group = None
        self._active_group_from_header = None

    # ... (login_and_wait, get_active_group_name, _click_group methods remain the same) ...
    def login_and_wait(self, timeout=20):
        """Navigates to Zalo and waits for the main chat interface to load."""
        logger.info("Navigating to Zalo and verifying login status...")
        self.driver.get("https://chat.zalo.me")
        try:
            WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, self.selectors["logged_in_sentinel"]))
            )
            logger.info("Zalo is logged in and chat interface is ready.")
            return True
        except TimeoutException:
            logger.error(f"Zalo did not load or was not logged in within {timeout} seconds.")
            return False

    def get_active_group_name(self):
        """
        Gets the name of the currently displayed group from the header.
        This is the most reliable way to know which chat is active.
        """
        try:
            header_element = self.driver.find_element(By.CSS_SELECTOR, self.selectors["header_title_active_group"])
            self._active_group_from_header = header_element.text.strip()
            return self._active_group_from_header
        except NoSuchElementException:
            logger.debug("Could not find header title to determine active group.")
            return self._last_clicked_group # Fallback to the last group we intentionally clicked
        except Exception as e:
            logger.warning(f"Error getting active group name: {e}")
            return self._last_clicked_group

    def _click_group(self, group_title: str):
        """Finds a group by its title in the conversation list and clicks it."""
        logger.debug(f"Attempting to click group: '{group_title}'")
        try:
            # Using XPath to find an element by its exact text content
            group_xpath = f"//*[normalize-space(text())='{group_title}']/ancestor::div[contains(@class, 'conv-item')][1]"
            group_element = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, group_xpath))
            )
            group_element.click()
            self._last_clicked_group = group_title # Track the group we clicked
            time.sleep(self.action_delay)
            # Verify the header updated to the correct group
            WebDriverWait(self.driver, 5).until(
                EC.text_to_be_present_in_element((By.CSS_SELECTOR, self.selectors["header_title_active_group"]), group_title)
            )
            logger.info(f"Successfully switched to group: '{group_title}'")
            return True
        except TimeoutException:
            logger.warning(f"Failed to switch to group '{group_title}'. It might already be active or not exist.")
            # Check if we are already there
            if self.get_active_group_name() == group_title:
                return True
            return False
        except Exception as e:
            logger.error(f"An unexpected error occurred while clicking group '{group_title}': {e}")
            return False

    def _validate_last_message(self, expected_message: str) -> bool:
        """
        Checks the last 5 messages in the current chat to confirm the expected_message was sent.
        """
        time.sleep(1.5) # Wait for UI to update after sending
        try:
            # If the message was a list, we only validate the first line for simplicity
            if isinstance(expected_message, list):
                expected_message = expected_message[0]

            last_messages = self.driver.find_elements(By.CSS_SELECTOR, self.selectors["message_element"])[-5:]
            for msg_element in reversed(last_messages):
                try:
                    content = msg_element.find_element(By.CSS_SELECTOR, self.selectors["message_content"]).text.strip()
                    # We check if the expected message is *contained* in the actual message.
                    # This handles cases where Zalo might add formatting or truncate long messages.
                    if expected_message in content:
                        logger.info(f"Validation successful for message: '{expected_message}'")
                        return True
                except NoSuchElementException:
                    continue # Not a standard message element
            
            logger.warning(f"VALIDATION FAILED: Could not find recently sent message '{expected_message}'")
            return False
        except Exception as e:
            logger.error(f"An error occurred during message validation: {e}")
            return False

# --- START: Replacement block for the send_message function ---

    def send_message(self, group_title: str, message: str):
        """
        Switches to a group, sends a message using the clipboard, and validates.
        This replaces the Shift+Enter simulation with a more reliable copy-paste method.
        """
        if self.get_active_group_name() != group_title:
            if not self._click_group(group_title):
                return False
        
        for attempt in range(2):
            try:
                full_message = "\n".join(message) if isinstance(message, list) else message
                
                # Step 1: Copy the full, formatted message to the system clipboard
                pyperclip.copy(full_message)
                
                chat_input = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, self.selectors["chat_input"]))
                )
                
                # Step 2: Simulate a "Paste" command (Ctrl+V for Win/Linux)
                chat_input.send_keys(Keys.CONTROL, 'v')
                
                # Step 3: Simulate "Enter" to send the pasted content
                chat_input.send_keys(Keys.RETURN)
                
                # Step 4: Use the existing validation logic
                if self._validate_last_message(full_message):
                    logger.info(f"Successfully sent and validated message to '{group_title}'")
                    return True
                else:
                    logger.warning(f"Attempt {attempt + 1} failed validation. Retrying if possible.")
                    # Clear input for retry, just in case
                    WebDriverWait(self.driver, 3).until(EC.presence_of_element_located((By.CSS_SELECTOR, self.selectors["chat_input"]))).clear()

            except Exception as e:
                logger.error(f"Failed to send message to '{group_title}' on attempt {attempt + 1}: {e}")
                time.sleep(1)

        logger.error(f"Failed to send message to '{group_title}' after multiple attempts.")
        return False

# --- END: Replacement block for the send_message function ---
    
    def _get_image_from_element(self, msg_element) -> bytes | None:
        """
        Finds an image element within a message, gets its source URL,
        and downloads the image content.
        """
        try:
            # Find the main image display element within the message bubble
            img_container = msg_element.find_element(By.CSS_SELECTOR, "div.msg-image")
            img_tag = img_container.find_element(By.TAG_NAME, "img")
            
            img_url = img_tag.get_attribute('src')
            if not img_url:
                return None

            # Zalo uses blobs for recent images, but direct URLs for older ones.
            # We can use requests to download it directly.
            # We don't need the session for these public/blob URLs.
            response = requests.get(img_url, timeout=20)
            response.raise_for_status()
            logger.info(f"Successfully downloaded image from URL: {img_url[:50]}...")
            return response.content
        except NoSuchElementException:
            return None # This message element is not an image
        except Exception as e:
            logger.error(f"Failed to download image: {e}")
            return None

    def read_new_messages(self, group_title: str, last_check_time: datetime):
        """
        MODIFIED: Now reads both text messages and detects new images.
        """
        new_messages = []
        if self.get_active_group_name() != group_title:
            if not self._click_group(group_title):
                return new_messages

        try:
            message_elements = self.driver.find_elements(By.CSS_SELECTOR, self.selectors["message_element"])[-15:]
            
            for msg_element in reversed(message_elements):
                try:
                    time_elem = msg_element.find_element(By.CSS_SELECTOR, self.selectors["message_time"])
                    time_str = time_elem.text.strip()
                    
                    msg_hour, msg_minute = map(int, time_str.split(':'))
                    msg_time = datetime.now().replace(hour=msg_hour, minute=msg_minute, second=0, microsecond=0)
                    
                    if msg_time > datetime.now() + timedelta(minutes=1):
                         msg_time -= timedelta(days=1)
                    
                    if msg_time <= last_check_time:
                        break
                    
                    # Try to get text content first
                    try:
                        content_elem = msg_element.find_element(By.CSS_SELECTOR, self.selectors["message_content"])
                        content = content_elem.text.strip()
                        if content:
                            new_messages.append({'type': 'text', 'content': content, 'timestamp': msg_time})
                            continue # Move to next message
                    except NoSuchElementException:
                        pass # Not a text message, could be an image

                    # If no text, try to get an image
                    image_bytes = self._get_image_from_element(msg_element)
                    if image_bytes:
                        new_messages.append({'type': 'image', 'content': image_bytes, 'timestamp': msg_time})

                except (NoSuchElementException, ValueError) as e:
                    logger.debug(f"Could not parse a message element: {e}")
                    continue
            
            return sorted(new_messages, key=lambda x: x['timestamp'])

        except Exception as e:
            logger.error(f"Error reading messages from '{group_title}': {e}")
            return []

    def get_unread_group_names(self):
        """
        Scans the conversation list for groups with unread notification icons.
        This is the primary method for detecting new commands.
        """
        unread_groups = set()
        try:
            # Find all visible conversation items
            msg_items = self.driver.find_elements(By.CSS_SELECTOR, self.selectors["group_item"])
            
            for item in msg_items:
                # Check if any of the known notification icon selectors exist within this item
                for notif_css in self.selectors["unread_notif_icons"]:
                    try:
                        # Use find_element, which raises an exception if not found
                        item.find_element(By.CSS_SELECTOR, notif_css)
                        
                        # If found, get the group name from this item
                        name_elem = item.find_element(By.CSS_SELECTOR, self.selectors["group_title_in_item"])
                        name = name_elem.text.strip()
                        if name:
                            unread_groups.add(name)
                        break # Move to the next group item
                    except NoSuchElementException:
                        continue # This icon wasn't found, try the next one
                        
        except Exception as e:
            logger.warning(f"Could not scrape unread group names: {e}")
            
        logger.debug(f"Detected unread groups via icons: {list(unread_groups)}")
        return list(unread_groups)

    def navigate_to_group(self, group_title: str):
        """
        Navigates to a specific group without reading or sending messages.
        Used to reset the view to the stay/fallback group.
        """
        if self.get_active_group_name() != group_title:
            logger.info(f"Returning to fallback group: '{group_title}'")
            self._click_group(group_title)
            
# In zalo.py, replace the existing scrape_all_groups function with this one:
    def scrape_all_groups(self):
        """
        Scrapes all group titles from the conversation sidebar, scrolling down to find all of them.
        """
        all_groups = set()
        scroll_attempts = 0
        max_scrolls = 20 # Safety break to prevent infinite loops

        try:
            # Find the scrollable container for the conversation list
            scroll_container_selector = "div#conversationListId"
            scroll_container = self.driver.find_element(By.CSS_SELECTOR, scroll_container_selector)
            
            while scroll_attempts < max_scrolls:
                previous_group_count = len(all_groups)

                # Scrape all currently visible groups
                group_elements = self.driver.find_elements(By.CSS_SELECTOR, self.selectors["group_title_in_item"])
                for elem in group_elements:
                    try:
                        name = elem.text.strip()
                        if name:
                            all_groups.add(name)
                    except Exception:
                        continue # Ignore stale elements

                # Scroll the container to the bottom
                self.driver.execute_script("arguments[0].scrollTop = arguments[0].scrollHeight", scroll_container)
                
                # Wait for any new groups to load
                time.sleep(1.5)

                # If the number of groups hasn't changed after scrolling and waiting, we're at the bottom
                if len(all_groups) == previous_group_count:
                    logger.info(f"Scraping complete. Found {len(all_groups)} unique groups after {scroll_attempts + 1} scrolls.")
                    break
                
                scroll_attempts += 1
            
            if scroll_attempts >= max_scrolls:
                logger.warning(f"Reached max scrolls ({max_scrolls}). Returning {len(all_groups)} found groups.")

            return list(all_groups)

        except Exception as e:
            logger.error(f"Could not scrape group list with scrolling: {e}")
            # Return whatever was found, even if the process failed midway
            return list(all_groups)

    def click_group_members_icon(self) -> bool:
        """Clicks the icon in the header that opens the group member sidebar."""
        try:
            # This selector targets the clickable area showing member count
            member_icon_selector = "div.subtitle__groupmember__content"
            wait = WebDriverWait(self.driver, 10)
            member_icon = wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, member_icon_selector))
            )
            member_icon.click()
            # Wait for the sidebar to become visible
            wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, "div.chat-box-member")))
            logger.info("Successfully opened group member sidebar.")
            return True
        except TimeoutException:
            logger.warning("Could not find or click the group member icon.")
            return False

    def scrape_group_members(self) -> list[str]:
        """Scrapes all member names from the visible member sidebar. Assumes sidebar is already open."""
        members = []
        try:
            wait = WebDriverWait(self.driver, 15)
            # This selector targets each member item in the list
            member_elements_selector = "div[data-id='div_MemList_MemItem']"
            member_elements = wait.until(
                EC.presence_of_all_elements_located((By.CSS_SELECTOR, member_elements_selector))
            )
            
            for elem in member_elements:
                # The member's full name is reliably in the 'title' attribute
                name = elem.get_attribute('title')
                if name:
                    members.append(name.strip())
            
            logger.info(f"Scraped {len(members)} members.")
            return members
        except TimeoutException:
            logger.error("Timed out waiting for member list to appear in sidebar.")
            return []

    def close_sidebar_if_open(self):
        """Closes the right sidebar by sending the ESCAPE key to the chat input."""
        try:
            # Check if a sidebar is likely open
            if self.driver.find_elements(By.CSS_SELECTOR, "div.chat-box-member"):
                chat_input = self.driver.find_element(By.CSS_SELECTOR, self.selectors["chat_input"])
                chat_input.send_keys(Keys.ESCAPE)
                logger.info("Closed sidebar using ESC key.")
                time.sleep(0.5)
        except (NoSuchElementException, ElementNotInteractableException):
            # No sidebar was open or input wasn't available, which is fine.
            pass