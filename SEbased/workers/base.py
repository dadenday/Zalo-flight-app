"""Base worker class shared by all worker implementations."""

import threading
import uuid
from datetime import datetime
from queue import Queue


class BaseWorker(threading.Thread):
    def __init__(self, task_id, reporting_group, report_queue: Queue):
        super().__init__()
        self.worker_id = str(uuid.uuid4())[:8]
        self.task_id = task_id  # e.g., flight code or "cargo-VJC123"
        self.reporting_group = reporting_group
        self.report_queue = report_queue
        self.stop_event = threading.Event()
        self.name = f"{self.__class__.__name__}-{self.worker_id}"

    def stop(self) -> None:
        self.stop_event.set()

    def submit_report(self, status: str, is_final: bool = False) -> None:
        """Place a formatted report into the shared queue."""

        report = {
            "task_id": self.task_id,
            "reporting_group": self.reporting_group,
            "status": status,
            "timestamp": datetime.now().isoformat(),
            "is_final": is_final,
        }
        self.report_queue.put(report)

