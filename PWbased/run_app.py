# run_app.py

import argparse
import logging
import os
import re
import sys
import threading
import time
from queue import Queue, Empty
from datetime import datetime, timedelta

from playwright.sync_api import sync_playwright, Playwright, BrowserContext, Error as PlaywrightError

from config_loader import load_main_config, load_user_settings, save_user_settings, load_timestamps, save_timestamps
from zalo import ZaloManager
from workers import FlightWorker, CargoWorker, FlightListWorker

# --- Logging setup is unchanged ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(threadName)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('flight_monitor.log', mode='w'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class AppController:
    # --- __init__ is unchanged from the previous corrected version ---
    def __init__(self, main_config, user_settings, is_gui=False, is_debug=False):
        self.config = main_config
        self.user_settings = user_settings
        self.is_gui = is_gui
        if is_debug: logging.getLogger().setLevel(logging.DEBUG)

        self.playwright: Playwright = None
        self.zalo_context: BrowserContext = None
        self.worker_context: BrowserContext = None # Renamed for clarity
        self.zalo_manager = None
        
        self.report_queue = Queue()
        self.active_workers = {}
        self.worker_lock = threading.Lock()
        
        self.flight_subscriptions = {}
        self.cargo_cache = {}
        
        self.proactive_flight_list = []
        self.proactive_last_update_time = None
        self.proactive_scrape_tracker = {}
        self.paid_list = set(self.user_settings.get('monitoring_groups', []))
        
        # --- RE-ADDED: State for the missing feature ---
        self.avail_list = set()
        self.last_group_scrape_time = 0
        
        self.last_check_times = {}
        self.last_refresh_time = time.time()
        self.is_running = True

    def _setup_browsers(self, playwright: Playwright):
        logger.info("Setting up two isolated Playwright browser environments...")
        try:
            # --- Zalo Context ---
            self.zalo_context = playwright.chromium.launch_persistent_context(
                user_data_dir=os.path.abspath('zalo_profile_pw'),
                headless=not self.is_gui,
                args=["--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu"]
            )
            zalo_page = self.zalo_context.pages[0] if self.zalo_context.pages else self.zalo_context.new_page()
            
            # --- FIX: Use config-driven delay and encapsulated login method ---
            action_delay = self.config['settings'].get('manager_zalo_action_delay', 1.5)
            self.zalo_manager = ZaloManager(zalo_page, self.config['zalo_selectors'], action_delay)
            if not self.zalo_manager.login_and_wait():
                return False # Explicit failure handling
            logger.info("Zalo context is ready.")
            
            # Profile 2: Worker persistent context for deeper isolation
            self.worker_context = playwright.chromium.launch_persistent_context(
                user_data_dir=os.path.abspath('worker_profile_pw'),
                headless=not self.is_gui,
                args=["--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu"]
            )
            logger.info("Worker context is ready.")
            return True
        except PlaywrightError as e:
            logger.error(f"Failed to set up Playwright browsers: {e}")
            return False

    # --- RE-ADDED: Logic to handle proactive flight list ---
    def _handle_proactive_flight_list(self, flight_list: list):
        logger.info(f"Received a new proactive flight list with {len(flight_list)} items.")
        self.proactive_flight_list = flight_list
        self.proactive_last_update_time = datetime.now()
        
        current_flight_ids = {flight['flight_id'] for flight in flight_list}
        
        flights_to_remove = [fid for fid in self.proactive_scrape_tracker if fid not in current_flight_ids]
        for flight_id in flights_to_remove:
            del self.proactive_scrape_tracker[flight_id]

        for flight in flight_list:
            flight_id = flight['flight_id']
            if flight_id not in self.proactive_scrape_tracker:
                logger.info(f"Proactive: New flight '{flight_id}' detected. Triggering CargoWorker for caching.")
                task_id = f"cargo-{flight_id}-summary"
                
                if task_id in self.cargo_cache or task_id in self.active_workers:
                    logger.debug(f"Proactive: Cargo task for '{flight_id}' already cached or running. Skipping.")
                    continue

                # Use "CACHE" group to signal silent caching
                worker = CargoWorker(flight_id, 'summary', "CACHE", self.config['vietjet_cargo'], self.worker_context, self.report_queue)
                with self.worker_lock:
                    self.active_workers[task_id] = worker
                worker.start()
                self.proactive_scrape_tracker[flight_id] = datetime.now()

    # --- MODIFIED: Restored full logic for report processing ---
    def _process_report_queue(self):
        while self.is_running:
            try:
                report = self.report_queue.get(timeout=1)
                task_id, status, is_final = report['task_id'], report['status'], report.get('is_final', False)
                
                if task_id == "proactive-flight-list":
                    self._handle_proactive_flight_list(status)
                    continue

                if report['reporting_group'] == "CACHE":
                    if is_final and task_id.startswith("cargo-"):
                        logger.info(f"Proactive: Caching final report for task '{task_id}'.")
                        self.cargo_cache[task_id] = (status, datetime.now())
                elif task_id in self.flight_subscriptions:
                    subscribers = self.flight_subscriptions.get(task_id, set())
                    for group in subscribers: self.zalo_manager.send_message(group, status)
                else:
                    self.zalo_manager.send_message(report['reporting_group'], status)

                if is_final:
                    with self.worker_lock:
                        if task_id in self.flight_subscriptions: del self.flight_subscriptions[task_id]
                        if task_id in self.active_workers:
                            del self.active_workers[task_id]
            except Empty: continue
            except Exception as e: logger.error(f"Error processing report queue: {e}")

    # --- RE-ADDED: Proactive monitoring loop ---
    def _proactive_monitoring_loop(self):
        time.sleep(30) 
        while self.is_running:
            try:
                logger.info("ProactiveScheduler: Triggering 'ai list VJC SGN' workflow.")
                task_id = "proactive-flight-list"
                worker = FlightListWorker("VJC", "SGN", "PROACTIVE", self.config['sites'][0], self.worker_context, self.report_queue)
                worker.task_id = task_id
                self.active_workers[task_id] = worker
                worker.start()
                time.sleep(15 * 60) # Run every 15 minutes
            except Exception as e:
                logger.error(f"Error in proactive monitoring loop: {e}")
                time.sleep(60)

    # --- RE-ADDED: Cache cleanup loop ---
    def _cache_cleanup_loop(self):
        while self.is_running:
            time.sleep(30 * 60) # Run every 30 minutes
            try:
                cache_expiry_hours = self.config['settings'].get('cargo_cache_expiry_hours', 6)
                expiry_delta = timedelta(hours=cache_expiry_hours)
                stale_keys = []
                with self.worker_lock:
                    for task_id, (_, timestamp) in self.cargo_cache.items():
                        if datetime.now() - timestamp > expiry_delta:
                            stale_keys.append(task_id)
                    if stale_keys:
                        logger.info(f"CacheCleaner: Removing {len(stale_keys)} stale items.")
                        for key in stale_keys:
                            if key in self.cargo_cache:
                                del self.cargo_cache[key]
            except Exception as e:
                logger.error(f"Error in cache cleanup loop: {e}")

    def _handle_command(self, message: str, group_title: str):
        content = message.lower().strip()
        control_group = self.user_settings['control_group']

        flight_list_match = re.match(r"^ai list ([a-zA-Z0-9]{3}) ([a-zA-Z]{3})$", content)
        if flight_list_match:
            airline, airport = flight_list_match.groups()
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

        check_match = re.match(r"^ai check ([a-zA-Z0-9]+)", content)
        if check_match:
            flight_id = check_match.group(1).upper()
            self.flight_subscriptions.setdefault(flight_id, set()).add(group_title)
            
            if flight_id in self.active_workers:
                self.report_queue.put({"reporting_group": group_title, "task_id": f"confirm-{flight_id}", 
                                     "status": f"Đã thêm nhóm này vào danh sách theo dõi cho {flight_id}."})
                return
            
            # --- FIX: Add site presence check ---
            site_config = self.config['sites'][0] if self.config['sites'] else None
            if not site_config:
                self.report_queue.put({"reporting_group": group_title, "task_id": f"error-{flight_id}", 
                                     "status": "Lỗi: Không có trang web nào được cấu hình để theo dõi."})
                return

            self.report_queue.put({"reporting_group": group_title, "task_id": f"confirm-{flight_id}", 
                                 "status": f"Đã chấp nhận. Bắt đầu theo dõi {flight_id}..."})
            worker = FlightWorker(flight_id, group_title, site_config, self.worker_context, self.report_queue)
            with self.worker_lock: self.active_workers[flight_id] = worker
            worker.start()
            return

        # --- RE-IMPLEMENTED: "Cache-only" ai cargo command path ---
        cargo_summary_match = re.match(r"^ai cargo ([a-zA-Z0-9]+)$", content)
        if cargo_summary_match:
            flight_id = cargo_summary_match.group(1).upper().strip()
            task_id_summary = f"cargo-{flight_id}-summary"
            
            with self.worker_lock:
                cache_hit = self.cargo_cache.get(task_id_summary)

            cache_expiry_hours = self.config['settings'].get('cargo_cache_expiry_hours', 6)
            if cache_hit and (datetime.now() - cache_hit[1] < timedelta(hours=cache_expiry_hours)):
                report_data, timestamp = cache_hit
                full_report = f"[BÁO CÁO TỪ BỘ NHỚ ĐỆM (lúc {timestamp.strftime('%H:%M:%S')})]\n" + ("\n".join(report_data) if isinstance(report_data, list) else report_data)
                self.report_queue.put({"reporting_group": group_title, "task_id": task_id_summary, "status": full_report})
            else:
                # This is the special "cache-only" response
                response = f"Không tìm thấy dữ liệu hàng hoá cho chuyến bay {flight_id}. Chuyến bay có thể chưa có trong danh sách đến hoặc dữ liệu đã cũ."
                self.report_queue.put({"reporting_group": group_title, "task_id": f"cache-miss-{flight_id}", "status": response})
            return

        # --- FIX: On-demand cargo command with correct cache handling ---
        cargo_match_full = re.match(r"^ai cargo ([a-zA-Z0-9]+)\s+--(\S+)", content)
        if cargo_match_full:
            flight_id, task_type = cargo_match_full.group(1).upper().strip(), cargo_match_full.group(2)
            task_id = f"cargo-{flight_id}-{task_type}"
            
            with self.worker_lock:
                cache_hit = self.cargo_cache.get(task_id)
            
            cache_expiry_hours = self.config['settings'].get('cargo_cache_expiry_hours', 6)
            if cache_hit and (datetime.now() - cache_hit[1] < timedelta(hours=cache_expiry_hours)):
                report_data, timestamp = cache_hit
                full_report = f"[BÁO CÁO TỪ BỘ NHỚ ĐỆM (lúc {timestamp.strftime('%H:%M:%S')})]\n" + ("\n".join(report_data) if isinstance(report_data, list) else report_data)
                self.report_queue.put({"reporting_group": group_title, "task_id": task_id, "status": full_report})
                return # <-- THE CRITICAL BUG FIX

            if task_id in self.active_workers:
                self.report_queue.put({"reporting_group": group_title, "task_id": f"confirm-{task_id}", "status": f"Tác vụ hàng hóa cho {flight_id} đang được xử lý."})
            else:
                self.report_queue.put({"reporting_group": group_title, "task_id": f"confirm-{task_id}", "status": f"Đang lấy thông tin '{task_type}' cho chuyến bay {flight_id.upper()}..."})
                worker = CargoWorker(flight_id, task_type, group_title, self.config['vietjet_cargo'], self.worker_context, self.report_queue)
                with self.worker_lock: self.active_workers[task_id] = worker
                worker.start()
            return

        if content == "ai stop" and group_title == control_group:
            logger.info("Global stop command received.")
            self.is_running = False

    def run(self):
        with sync_playwright() as p:
            self.playwright = p
            if not self._setup_browsers(p): self.shutdown(); return
            # Note: zalo_manager is now created in _setup_browsers
            
            all_groups = self.paid_list.union({self.user_settings['control_group']})
            self.last_check_times = load_timestamps(all_groups)
            
            # --- Start ALL background threads ---
            report_thread = threading.Thread(target=self._process_report_queue, name="ReportProcessor", daemon=True)
            proactive_thread = threading.Thread(target=self._proactive_monitoring_loop, name="ProactiveScheduler", daemon=True)
            cache_cleanup_thread = threading.Thread(target=self._cache_cleanup_loop, name="CacheCleaner", daemon=True)
            report_thread.start()
            proactive_thread.start()
            cache_cleanup_thread.start()
            
            logger.info("Setup complete. Starting main monitoring loop...")
            
            while self.is_running:
                try:
                    # --- RE-ADDED: Periodic scraping of all Zalo groups ---
                    scrape_interval_minutes = 15
                    if time.time() - self.last_group_scrape_time > scrape_interval_minutes * 60:
                        logger.info(f"Scraping Zalo sidebar for available groups (every {scrape_interval_minutes} mins).")
                        scraped_groups = self.zalo_manager.scrape_all_groups()
                        if scraped_groups:
                            self.avail_list = set(scraped_groups)
                        self.last_group_scrape_time = time.time()

                    # --- FIX: Use config-driven refresh interval ---
                    refresh_interval_mins = self.config['settings'].get('page_refresh_minutes', 240) # Default 4h
                    if time.time() - self.last_refresh_time > refresh_interval_mins * 60:
                        logger.info(f"Performing periodic {refresh_interval_mins}-minute refresh of Zalo page.")
                        self.zalo_manager.page.reload(wait_until="load")
                        self.last_refresh_time = time.time()
                    
                    groups_to_monitor = self.paid_list.union({self.user_settings['control_group']})
                    unread_groups = self.zalo_manager.get_unread_group_names()
                    
                    for group in unread_groups:
                        if group in groups_to_monitor:
                            if group not in self.last_check_times: 
                                self.last_check_times[group] = datetime.now() - timedelta(days=1)
                            new_messages = self.zalo_manager.read_new_messages(group, self.last_check_times[group])
                            if new_messages:
                                self.last_check_times[group] = new_messages[-1]['timestamp']
                                for msg in new_messages: self._handle_command(msg['content'], group)
                    
                    stay_group = self.user_settings.get('stay_group')
                    if stay_group: self.zalo_manager.navigate_to_group(stay_group)
                    time.sleep(self.config['settings'].get('manager_loop_delay', 5))

                except KeyboardInterrupt: self.is_running = False
                except Exception as e:
                    logger.error(f"An error occurred in the main loop: {e}", exc_info=True)
                    time.sleep(10)
            self.shutdown()

    def shutdown(self):
        logger.info("Shutting down application...")
        self.is_running = False
        with self.worker_lock:
            active_worker_list = list(self.active_workers.values())
            logger.info(f"Stopping {len(active_worker_list)} active workers...")
            for worker in active_worker_list:
                worker.stop()
        for worker in active_worker_list:
             worker.join(timeout=10)

        save_timestamps(self.last_check_times)
        
        # Close both browser contexts
        if self.worker_context: self.worker_context.close()
        if self.zalo_context: self.zalo_context.close()
        
        logger.info("Shutdown complete.")
        sys.exit(0)

# --- MODIFIED: Initial setup aligned with original Selenium version ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Zalo Flight & Cargo Monitoring Bot")
    parser.add_argument('--gui', action='store_true', help="Run in GUI mode (headed).")
    parser.add_argument('--debug', action='store_true', help="Enable detailed debug logging.")
    args = parser.parse_args()

    main_config, user_settings = load_main_config(), load_user_settings()
    if not user_settings.get('control_group'):
        print("\n--- [Manager] INITIAL SETUP ---")
        cg = input("Enter EXACT title for Control Group: ")
        # Monitoring groups are managed dynamically, so we don't ask for them.
        sg = input("Enter EXACT title for Stay/Fallback Group: ")
        user_settings['control_group'] = cg
        user_settings['monitoring_groups'] = [] # Set to empty as it's no longer used for setup
        user_settings['stay_group'] = sg
        save_user_settings(user_settings)
        print("Settings saved. You can add monitoring groups to 'settings.json' manually if needed.")

    app = AppController(main_config, user_settings, is_gui=args.gui, is_debug=args.debug)
    app.run()