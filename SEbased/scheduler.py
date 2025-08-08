"""Scheduled proactive task utilities."""

import logging
import time

from workers import FlightListWorker

logger = logging.getLogger(__name__)


def proactive_monitoring_loop(controller) -> None:
    """Main loop for scheduled proactive tasks."""

    time.sleep(30)
    while controller.is_running:
        try:
            logger.info("ProactiveScheduler: Triggering 'ai list VJC SGN' workflow.")
            task_id = "proactive-flight-list"
            worker = FlightListWorker(
                "VJC",
                "SGN",
                "PROACTIVE",
                controller.config.sites[0],
                controller.worker_driver,
                controller.report_queue,
            )
            worker.task_id = task_id
            controller.active_workers[task_id] = worker
            worker.start()
            time.sleep(15 * 60)
        except Exception as e:  # pragma: no cover - defensive
            logger.error("Error in proactive monitoring loop: %s", e)
            time.sleep(60)

