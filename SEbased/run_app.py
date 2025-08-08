# run_app.py

import argparse
import logging
import re
import sys
import threading
import time
import uuid
from queue import Queue, Empty
from datetime import datetime, timedelta, timezone
from group_db import load_db, save_db

from config_loader import (
    load_main_config,
    load_user_settings,
    save_user_settings,
    load_timestamps,
    save_timestamps,
)
from drivers import setup_drivers
from zalo import ZaloManager
from worker_manager import process_report_queue
from scheduler import proactive_monitoring_loop
from workers import FlightWorker, CargoWorker, FlightListWorker, OCRWorker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(threadName)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("flight_monitor.log", mode="w"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# --- Main Application Controller ---

class AppController:
    def __init__(self, main_config, user_settings, is_gui=False, is_debug=False):
        self.config = main_config
        self.user_settings = user_settings
        self.is_gui = is_gui
        if is_debug: logging.getLogger().setLevel(logging.DEBUG)

        self.report_queue = Queue()
        self.active_workers = {}
        self.worker_lock = threading.Lock()
        
        # --- NEW: Concurrency lock and database state ---
        self.zalo_lock = threading.Lock()
        self.group_db = load_db()
        self.group_scan_queue = [] # A simple queue for which group to scan next
        
        # --- NEW: State for image processing ---
        self.command_queue = Queue()
        
        # --- NEW: Load Google API Key and configure image processing ---
        self.google_api_key = main_config.google_api_key
        if not self.google_api_key or self.google_api_key == "YOUR_API_KEY_HERE":
            logger.warning("Google API Key not found in config.json. Image processing will be disabled.")
            self.image_processing_enabled = False
        else:
            self.image_processing_enabled = True  # Enable only if we have a valid API key
        
        self.flight_subscriptions = {}
        self.cargo_cache = {}
        self.avail_list = set()
        self.paid_list = set()
        self.last_group_scrape_time = 0
        
        # --- NEW: State for proactive workflow ---
        self.proactive_flight_list = [] # Stores list of dicts from FlightListWorker
        self.proactive_last_update_time = None
        self.proactive_scrape_tracker = {} # Tracks flights sent to CargoWorker to avoid duplicates
        
        self.zalo_driver = None
        self.worker_driver = None
        self.zalo_manager = None
        
        self.last_check_times = {}
        self.last_refresh_time = time.time()
        self.is_running = True
    
    def _handle_proactive_flight_list(self, flight_list: list):
        """NEW: Processes the flight list from the proactive worker."""
        logger.info(f"Received a new proactive flight list with {len(flight_list)} items.")
        self.proactive_flight_list = flight_list
        self.proactive_last_update_time = datetime.now()
        
        current_flight_ids = {flight['flight_id'] for flight in flight_list}
        
        # --- Clean up old flights from the tracker ---
        flights_to_remove = [fid for fid in self.proactive_scrape_tracker if fid not in current_flight_ids]
        for flight_id in flights_to_remove:
            del self.proactive_scrape_tracker[flight_id]

        # --- Trigger CargoWorkers for new flights ---
        for flight in flight_list:
            flight_id = flight['flight_id']
            if flight_id not in self.proactive_scrape_tracker:
                logger.info(f"Proactive: New flight '{flight_id}' detected. Triggering CargoWorker.")
                task_id = f"cargo-{flight_id}-summary" # Proactively get summary
                
                # Check cache and active workers before starting a new one
                if task_id in self.cargo_cache or task_id in self.active_workers:
                    logger.debug(f"Proactive: Cargo task for '{flight_id}' already cached or running. Skipping.")
                    continue

                # Use a placeholder reporting group; this worker's output is for the cache only.
                worker = CargoWorker(
                    flight_id,
                    'summary',
                    "CACHE",
                    self.config.vietjet_cargo,
                    self.worker_driver,
                    self.report_queue,
                )
                with self.worker_lock:
                    self.active_workers[task_id] = worker
                worker.start()
                self.proactive_scrape_tracker[flight_id] = datetime.now() # Mark as scraped


    def _handle_command(self, message: str, group_title: str):
        content = message.lower().strip()
        control_group = self.user_settings.control_group

        # --- NEW: Control command for the feature ---
        if content.startswith("ai image") and group_title == control_group:
            parts = content.split()
            if len(parts) == 3 and parts[2] in ["on", "off"]:
                self.image_processing_enabled = (parts[2] == "on")
                status = "BẬT" if self.image_processing_enabled else "TẮT"
                response = f"Đã {status} chức năng tự động xử lý hình ảnh."
                self.report_queue.put({"reporting_group": group_title, "task_id": "cmd-img-toggle", "status": response})
            return

        # --- MODIFIED: `ai list` now reads from memory ---
        flight_list_match = re.match(r"^ai list ([a-zA-Z0-9]{3}) ([a-zA-Z]{3})$", content)
        if flight_list_match:
            airline, airport = flight_list_match.groups()
            
            # For now, we only support the proactively scraped list
            if airline.upper() == "VJC" and airport.upper() == "SGN":
                if not self.proactive_flight_list:
                    response = "Danh sách chuyến bay đến SGN đang được cập nhật, vui lòng thử lại sau giây lát."
                else:
                    response_lines = [f"Tóm tắt các chuyến bay của VJC đến SGN (Cập nhật lúc {self.proactive_last_update_time.strftime('%H:%M:%S')}):"]
                    for i, flight in enumerate(self.proactive_flight_list, 1):
                        response_lines.append(f"{i}. {flight['flight_id']} | {flight['aircraft']} | {flight['registration']}: {flight['route']}")
                    response = "\n".join(response_lines)
            else:
                response = "Lỗi: Hiện tại chỉ hỗ trợ xem danh sách tự động cho VJC đến SGN."
            
            self.report_queue.put({"reporting_group": group_title, "task_id": "cmd-list-flights", "status": response})
            return

        # --- MODIFIED: `ai cargo` now reads from cache ---
            cargo_match = re.match(r"^ai cargo ([a-zA-Z0-9]+)", content)
            if cargo_match:
                flight_id = cargo_match.group(1).upper().strip()
                task_id_summary = f"cargo-{flight_id}-summary"
                
                with self.worker_lock:
                    cache_hit = self.cargo_cache.get(task_id_summary)

                cache_expiry_hours = getattr(self.config, 'cargo_cache_expiry_hours', 6)
                if cache_hit and (datetime.now() - cache_hit[1] < timedelta(hours=cache_expiry_hours)):
                    report_data, timestamp = cache_hit
                    full_report = f"[BÁO CÁO TỪ BỘ NHỚ ĐỆM (lúc {timestamp.strftime('%H:%M:%S')})]\n" + ("\n".join(report_data) if isinstance(report_data, list) else report_data)
                    self.report_queue.put({"reporting_group": group_title, "task_id": task_id_summary, "status": full_report})
                else:
                    response = f"Không tìm thấy dữ liệu hàng hoá cho chuyến bay {flight_id}. Chuyến bay có thể chưa có trong danh sách đến hoặc dữ liệu đã cũ."
                    self.report_queue.put({"reporting_group": group_title, "task_id": f"cache-miss-{flight_id}", "status": response})
                return        # Original flight checking command
        check_match = re.match(r"^ai check ([a-zA-Z0-9]+)", content)
        if check_match:
            flight_id = check_match.group(1).upper()
            self.flight_subscriptions.setdefault(flight_id, set()).add(group_title)
            
            if flight_id in self.active_workers:
                self.report_queue.put({"reporting_group": group_title, "task_id": f"confirm-{flight_id}", 
                                     "status": f"Đã thêm nhóm này vào danh sách theo dõi cho {flight_id}."})
                return
            
            site_config = self.config.sites[0] if self.config.sites else None
            if not site_config:
                self.report_queue.put({"reporting_group": group_title, "task_id": f"error-{flight_id}", 
                                     "status": "Lỗi: Không có trang web nào được cấu hình."})
                return

            self.report_queue.put({"reporting_group": group_title, "task_id": f"confirm-{flight_id}", 
                                 "status": f"Đã chấp nhận. Bắt đầu theo dõi {flight_id}..."})
            worker = FlightWorker(flight_id, group_title, site_config, self.worker_driver, self.report_queue)
            with self.worker_lock: self.active_workers[flight_id] = worker
            worker.start()
            return

        cargo_match = re.match(r"^ai cargo ([a-zA-Z0-9]+)(?:\s+--(\S+))?$", content)
        if cargo_match:
            flight_id, task_type = cargo_match.group(1).strip(), 'info' if cargo_match.group(2) and 'info' in cargo_match.group(2) else 'summary'
            task_id = f"cargo-{flight_id}-{task_type}"
            
            cache_expiry_hours = getattr(self.config, 'cargo_cache_expiry_hours', 6)
            if task_id in self.cargo_cache and (datetime.now() - self.cargo_cache[task_id][1] < timedelta(hours=cache_expiry_hours)):
                report_data, _ = self.cargo_cache[task_id]
                full_report = "[BÁO CÁO TỪ BỘ NHỚ ĐỆM]\n" + ("\n".join(report_data) if isinstance(report_data, list) else report_data)
                self.report_queue.put({"reporting_group": group_title, "task_id": task_id, "status": full_report})
                return

            if task_id in self.active_workers:
                self.report_queue.put({"reporting_group": group_title, "task_id": f"confirm-{task_id}", "status": f"Tác vụ hàng hóa cho {flight_id} đang được xử lý."})
                return
            
            self.report_queue.put({"reporting_group": group_title, "task_id": f"confirm-{task_id}", "status": f"Đang lấy thông tin '{task_type}' cho chuyến bay {flight_id.upper()}..."})
            worker = CargoWorker(
                flight_id,
                task_type,
                group_title,
                self.config.vietjet_cargo,
                self.worker_driver,
                self.report_queue,
            )
            with self.worker_lock: self.active_workers[task_id] = worker
            worker.start()
            return

        if content == "ai stop" and group_title == control_group:
             logger.info("Global stop command received. Shutting down...")
             self.is_running = False

    def _cache_cleanup_loop(self):
        """Periodically cleans expired items from the cargo cache based on their creation timestamp."""
        cleanup_interval_minutes = 30
        logger.info(f"Cache cleaner started. Will run every {cleanup_interval_minutes} minutes.")
        
        while self.is_running:
            try:
                # Use a more resilient sleep that can be interrupted by the shutdown flag
                for _ in range(cleanup_interval_minutes * 60):
                    if not self.is_running:
                        logger.info("Cache cleaner shutting down.")
                        return
                    time.sleep(1)

                cache_expiry_hours = getattr(self.config, 'cargo_cache_expiry_hours', 6)
                logger.debug(f"CacheCleaner: Running cleanup for items older than {cache_expiry_hours} hours.")
                
                stale_keys = []
                
                # We use the lock here to safely read from the cache while other threads might be writing.
                with self.worker_lock:
                    # Iterate over a copy of items to allow for safe key collection
                    for task_id, (report_data, timestamp) in list(self.cargo_cache.items()):
                        if datetime.now() - timestamp > timedelta(hours=cache_expiry_hours):
                            stale_keys.append(task_id)
                
                # Perform the deletion outside the initial read lock if needed,
                # but locking for the whole operation is safer.
                if stale_keys:
                    with self.worker_lock:
                        logger.info(f"CacheCleaner: Found {len(stale_keys)} stale items to remove.")
                        for key in stale_keys:
                            # Verify key still exists before deleting in case of a race condition
                            if key in self.cargo_cache:
                                del self.cargo_cache[key]
                                logger.debug(f"CacheCleaner: Removed '{key}' from cache.")
                else:
                    logger.debug("CacheCleaner: No stale items found.")

            except Exception as e:
                logger.error(f"Error in cache cleanup loop: {e}")
                time.sleep(60)

    # --- NEW: Group identity and validation logic ---
    def _calculate_similarity(self, set1, set2):
        """Calculates the Jaccard similarity between two sets."""
        if not set1 and not set2:
            return 1.0
        if not set1 or not set2:
            return 0.0
        intersection = len(set1.intersection(set2))
        union = len(set1.union(set2))
        return intersection / union

    def find_renamed_group(self, old_name: str) -> str | None:
        """Tries to find a renamed group by comparing member lists."""
        logger.warning(f"Attempting to find potentially renamed group for '{old_name}'...")
        
        # Find the old group's data in our DB
        old_group_id = None
        for gid, data in self.group_db.items():
            if data['current_name'] == old_name:
                old_group_id = gid
                break
        
        if not old_group_id:
            logger.error(f"No database entry found for '{old_name}'. Cannot validate rename.")
            return None
            
        last_known_members = set(self.group_db[old_group_id]['last_known_members'])
        if not last_known_members:
            logger.error(f"No members recorded for '{old_name}'. Cannot validate rename.")
            return None

        # Compare against all current groups in the DB
        best_match_name = None
        highest_similarity = 0.75 # Must be at least 75% similar

        for gid, data in self.group_db.items():
            # Skip comparing a group to itself
            if gid == old_group_id:
                continue

            current_members = set(data['last_known_members'])
            similarity = self._calculate_similarity(last_known_members, current_members)
            
            if similarity > highest_similarity:
                highest_similarity = similarity
                best_match_name = data['current_name']
        
        if best_match_name:
            logger.info(f"Found a likely match for '{old_name}'! New name: '{best_match_name}' with {highest_similarity:.2f} similarity.")
            # Update the DB to merge the old record into the new one
            self.group_db[old_group_id]['current_name'] = best_match_name
            save_db(self.group_db)
            return best_match_name
            
        logger.warning(f"Could not find a suitable rename match for '{old_name}'.")
        return None

    # --- NEW: Background thread for scanning group members ---
    def _group_scanner_loop(self):
        """Periodically scrapes members from groups to keep the database updated."""
        time.sleep(10) # Initial delay
        logger.info("Group Member Scanner thread started.")
        
        while self.is_running:
            try:
                # If the scan queue is empty, populate it with all known groups
                if not self.group_scan_queue:
                    # Sort for consistent, alphabetical order
                    self.group_scan_queue = sorted(list(self.avail_list))

                if not self.group_scan_queue:
                    # Still no groups, wait a bit
                    time.sleep(60)
                    continue

                # Get the next group to scan
                group_to_scan = self.group_scan_queue.pop(0)

                logger.info(f"[Scanner] Acquiring lock to scan group: '{group_to_scan}'")
                with self.zalo_lock:
                    self.zalo_manager.close_sidebar_if_open() # Cleanup first
                    if self.zalo_manager._click_group(group_to_scan):
                        if self.zalo_manager.click_group_members_icon():
                            members = self.zalo_manager.scrape_group_members()
                            if members:
                                # Find if this group exists in DB or create a new entry
                                group_id = None
                                for gid, data in self.group_db.items():
                                    if data['current_name'] == group_to_scan:
                                        group_id = gid
                                        break
                                
                                if not group_id: # New group, create ID
                                    group_id = str(uuid.uuid4())
                                
                                # Update DB record
                                self.group_db[group_id] = {
                                    "current_name": group_to_scan,
                                    "last_known_members": members,
                                    "last_updated": datetime.now(timezone.utc).isoformat()
                                }
                                save_db(self.group_db)
                        self.zalo_manager.close_sidebar_if_open()
                logger.info(f"[Scanner] Released lock after scanning.")

                # Wait for the configured interval before next scan
                time.sleep(180) # 3 minutes between scans

            except Exception as e:
                logger.error(f"Error in group scanner loop: {e}", exc_info=True)
                time.sleep(60) # Wait longer on error

    def run(self):
        try:
            self.zalo_driver, self.worker_driver = setup_drivers(self.is_gui)
        except Exception as e:
            logger.error(f"Failed to set up drivers: {e}", exc_info=True)
            self.shutdown()
            return
        self.zalo_manager = ZaloManager(
            self.zalo_driver, self.config.settings.get('zalo_action_delay', 1.5)
        )
        if not self.zalo_manager.login_and_wait(): self.shutdown()
        
        self.last_check_times = load_timestamps([self.user_settings.control_group])
        
        report_thread = threading.Thread(
            target=process_report_queue,
            args=(self,),
            name="ReportProcessor",
            daemon=True,
        )
        proactive_thread = threading.Thread(
            target=proactive_monitoring_loop,
            args=(self,),
            name="ProactiveScheduler",
            daemon=True,
        )
        cache_cleanup_thread = threading.Thread(target=self._cache_cleanup_loop, name="CacheCleaner", daemon=True)
        # --- NEW: Start the group scanner thread ---
        group_scanner_thread = threading.Thread(target=self._group_scanner_loop, name="GroupScanner", daemon=True)
        
        report_thread.start()
        proactive_thread.start()
        cache_cleanup_thread.start()
        group_scanner_thread.start()
        
        logger.info("Setup complete. Starting main monitoring loop...")
        while self.is_running:
            try:
                # --- This block now feeds the scanner ---
                if time.time() - self.last_group_scrape_time > 15 * 60:
                    logger.info("Scraping Zalo sidebar to update available groups list...")
                    with self.zalo_lock:
                         scraped_groups = self.zalo_manager.scrape_all_groups()
                    if scraped_groups:
                        self.avail_list = set(scraped_groups)
                    self.last_group_scrape_time = time.time()
                
                if time.time() - self.last_refresh_time > 4 * 3600:
                    logger.info("Performing periodic 4-hour refresh of Zalo page.")
                    self.zalo_manager.driver.refresh()
                    time.sleep(10)
                    self.last_refresh_time = time.time()
                
                groups_to_monitor = self.paid_list.union({self.user_settings.control_group})
                
                # --- NEW: Process the internal command queue first ---
                try:
                    queued_command = self.command_queue.get_nowait()
                    logger.info(f"Processing queued command: '{queued_command['command']}' for group '{queued_command['group']}'")
                    self._handle_command(queued_command['command'], queued_command['group'])
                except Empty:
                    pass # No queued commands, proceed as normal

                # --- MODIFIED: Main loop now uses the Zalo lock and rename validation ---
                with self.zalo_lock:
                    unread_groups = self.zalo_manager.get_unread_group_names()
                    
                    # Validate that our monitored groups still exist
                    current_paid_list = self.paid_list.copy()
                    for group_name in current_paid_list:
                        if group_name not in self.avail_list:
                            new_name = self.find_renamed_group(group_name)
                            if new_name:
                                self.paid_list.remove(group_name)
                                self.paid_list.add(new_name)
                                logger.warning(f"Updated paid list: Replaced '{group_name}' with '{new_name}'.")

                    if self.user_settings.control_group not in self.avail_list:
                        new_name = self.find_renamed_group(self.user_settings.control_group)
                        if new_name:
                            self.user_settings.control_group = new_name
                            save_user_settings(self.user_settings)
                            logger.warning(
                                f"Updated control group name to '{new_name}'."
                            )

                    # Process commands from unread groups
                    for group in unread_groups:
                        if group in groups_to_monitor:
                            if group not in self.last_check_times:
                                self.last_check_times[group] = datetime.now() - timedelta(days=1)
                            
                            new_messages = self.zalo_manager.read_new_messages(group, self.last_check_times[group])
                            if new_messages:
                                self.last_check_times[group] = new_messages[-1]['timestamp']
                                for msg in new_messages:
                                    # If it's a text message, handle as a command
                                    if msg['type'] == 'text':
                                        self._handle_command(msg['content'], group)
                                    # If it's an image and the feature is on, start the OCR worker
                                    elif msg['type'] == 'image' and self.image_processing_enabled:
                                        logger.info(f"Image detected in '{group}'. Starting Gemini-powered OCR worker.")
                                        worker = OCRWorker(msg['content'], group, self.google_api_key, 
                                                        self.command_queue, self.report_queue)
                                        # This worker is short-lived, no need to track in active_workers
                                        worker.start()
                    
                    stay_group = self.user_settings.stay_group
                    if stay_group: self.zalo_manager.navigate_to_group(stay_group)

                time.sleep(self.config.settings.get('manager_loop_delay', 5))

            except KeyboardInterrupt: self.is_running = False
            except Exception as e:
                logger.error(f"An error occurred in the main loop: {e}", exc_info=True)
                time.sleep(10)
        self.shutdown()

    def shutdown(self):
        logger.info("Shutting down application...")
        self.is_running = False
        with self.worker_lock:
            for worker in self.active_workers.values():
                worker.stop()
                worker.join(timeout=5)
        save_timestamps(self.last_check_times)
        if self.zalo_driver: self.zalo_driver.quit()
        if self.worker_driver: self.worker_driver.quit()
        logger.info("Shutdown complete.")
        sys.exit(0)

# --- MODIFIED: Simplified initial setup ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Zalo Flight & Cargo Monitoring Bot")
    parser.add_argument('--gui', action='store_true', help="Run in GUI mode.")
    parser.add_argument('--debug', action='store_true', help="Enable detailed debug logging.")
    args = parser.parse_args()
    
    main_config, user_settings = load_main_config(), load_user_settings()
    if not user_settings.control_group:
        print("\n--- [Manager] INITIAL SETUP ---")
        cg = input("Enter EXACT title for Control Group: ")
        sg = input("Enter EXACT title for Stay/Fallback Group: ")
        user_settings.control_group = cg
        user_settings.monitoring_groups = []  # Set to empty as it's no longer used
        user_settings.stay_group = sg
        save_user_settings(user_settings)
        print("Settings saved.")

    app = AppController(main_config, user_settings, is_gui=args.gui, is_debug=args.debug)
    app.run()
