# run_app.py

import argparse
import logging
import re
import sys
import threading
import time
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
        self.group_scan_queue = Queue()
        
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

    # --- Background threads for group management ---

    def _group_list_loop(self):
        """Periodically scrapes the community list for group names."""
        time.sleep(5)
        logger.info("Group List worker started.")
        while self.is_running:
            try:
                with self.zalo_lock:
                    scraped = self.zalo_manager.scrape_group_list()
                if scraped:
                    new_set = set(scraped)
                    added = new_set - self.avail_list
                    if added:
                        logger.info(f"Detected {len(added)} new/renamed groups: {list(added)}")
                        for name in added:
                            self.group_scan_queue.put(name)
                    self.avail_list = new_set
                time.sleep(15 * 60)
            except Exception as e:
                logger.error(f"Error in group list loop: {e}", exc_info=True)
                time.sleep(60)

    def _group_validation_loop(self):
        """Consumes groups from queue and records their invitation link."""
        time.sleep(10)
        logger.info("Group Validation thread started.")
        while self.is_running:
            try:
                group_to_scan = self.group_scan_queue.get(timeout=5)
            except Empty:
                time.sleep(1)
                continue
            try:
                logger.info(f"[Validator] Acquiring lock for '{group_to_scan}'")
                with self.zalo_lock:
                    self.zalo_manager.close_sidebar_if_open()
                    if self.zalo_manager.open_group_via_list(group_to_scan):
                        link = self.zalo_manager.get_invitation_link()
                        if link:
                            group_id = link.rstrip('/').split('/')[-1]
                            record = self.group_db.get(group_id)
                            old_name = record['current_name'] if record else None
                            self.group_db[group_id] = {
                                "current_name": group_to_scan,
                                "invite_link": link,
                                "last_updated": datetime.now(timezone.utc).isoformat(),
                            }
                            save_db(self.group_db)
                            if old_name and old_name != group_to_scan:
                                if old_name in self.paid_list:
                                    self.paid_list.remove(old_name)
                                    self.paid_list.add(group_to_scan)
                                if old_name == self.user_settings.control_group:
                                    self.user_settings.control_group = group_to_scan
                                    save_user_settings(self.user_settings)
                        self.zalo_manager.close_sidebar_if_open()
                logger.info(f"[Validator] Finished processing '{group_to_scan}'")
            except Exception as e:
                logger.error(f"Error in group validation loop: {e}", exc_info=True)
                time.sleep(30)

    def run(self):
        try:
            self.zalo_driver, self.worker_driver = setup_drivers(self.is_gui)
        except Exception as e:
            logger.error(f"Failed to set up drivers: {e}", exc_info=True)
            self.shutdown()
            return
        self.zalo_manager = ZaloManager(
            self.zalo_driver,
            self.config.zalo_selectors,
            self.config.settings.get('zalo_action_delay', 1.5),
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
        group_list_thread = threading.Thread(target=self._group_list_loop, name="GroupListWorker", daemon=True)
        group_validator_thread = threading.Thread(target=self._group_validation_loop, name="GroupValidator", daemon=True)
        
        report_thread.start()
        proactive_thread.start()
        cache_cleanup_thread.start()
        group_list_thread.start()
        group_validator_thread.start()
        
        logger.info("Setup complete. Starting main monitoring loop...")
        while self.is_running:
            try:
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

                with self.zalo_lock:
                    stay_group = self.user_settings.stay_group
                    if stay_group:
                        self.zalo_manager.navigate_to_group(stay_group)

                    unread_groups = self.zalo_manager.get_unread_group_names()

                    for group in unread_groups:
                        if group in groups_to_monitor:
                            if group not in self.last_check_times:
                                self.last_check_times[group] = datetime.now() - timedelta(days=1)

                            new_messages = self.zalo_manager.read_new_messages(group, self.last_check_times[group])
                            if new_messages:
                                self.last_check_times[group] = new_messages[-1]['timestamp']
                                for msg in new_messages:
                                    if msg['type'] == 'text':
                                        self._handle_command(msg['content'], group)
                                    elif msg['type'] == 'image' and self.image_processing_enabled:
                                        logger.info(f"Image detected in '{group}'. Starting Gemini-powered OCR worker.")
                                        worker = OCRWorker(msg['content'], group, self.google_api_key,
                                                        self.command_queue, self.report_queue)
                                        worker.start()

                    if stay_group:
                        self.zalo_manager.navigate_to_group(stay_group)

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
