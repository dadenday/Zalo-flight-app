"""Utilities for worker orchestration."""

import logging
from datetime import datetime
from queue import Empty

logger = logging.getLogger(__name__)


def process_report_queue(controller) -> None:
    """Continuously processes reports produced by workers.

    The controller object is expected to expose attributes used in the original
    ``AppController._process_report_queue`` method.
    """

    while controller.is_running:
        try:
            report = controller.report_queue.get(timeout=1)
            task_id = report["task_id"]
            status = report["status"]
            is_final = report.get("is_final", False)

            if task_id == "proactive-flight-list":
                controller._handle_proactive_flight_list(status)
                continue

            if report["reporting_group"] == "CACHE":
                if is_final and task_id.startswith("cargo-"):
                    logger.info(
                        "Proactive: Caching final report for task '%s'.", task_id
                    )
                    controller.cargo_cache[task_id] = (status, datetime.now())
            elif task_id in controller.flight_subscriptions:
                subscribers = controller.flight_subscriptions.get(task_id, set())
                for group in subscribers:
                    with controller.zalo_lock:
                        controller.zalo_manager.send_message(group, status)
            else:
                with controller.zalo_lock:
                    controller.zalo_manager.send_message(
                        report["reporting_group"], status
                    )

            if is_final:
                with controller.worker_lock:
                    if task_id in controller.flight_subscriptions:
                        del controller.flight_subscriptions[task_id]
                    if task_id in controller.active_workers:
                        del controller.active_workers[task_id]
        except Empty:
            continue
        except Exception as e:  # pragma: no cover - defensive
            logger.error("Error processing report queue: %s", e)

