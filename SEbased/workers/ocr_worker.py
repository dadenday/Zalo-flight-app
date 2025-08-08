"""Worker that uses Gemini API to perform OCR on images."""

import io
import json
import logging
import uuid
from queue import Queue

import google.generativeai as genai
from PIL import Image

from .base import BaseWorker

logger = logging.getLogger(__name__)


class OCRWorker(BaseWorker):
    def __init__(self, image_bytes: bytes, reporting_group: str, api_key: str, command_queue: Queue, report_queue: Queue):
        super().__init__(
            task_id=f"ocr-{uuid.uuid4().hex[:8]}",
            reporting_group=reporting_group,
            report_queue=report_queue,
        )
        self.image_bytes = image_bytes
        self.command_queue = command_queue
        self.api_key = api_key

    def run(self):  # pragma: no cover - external API
        logger.info(
            f"[{self.name}] Starting Gemini API processing for an image from group '{self.reporting_group}'."
        )
        try:
            genai.configure(api_key=self.api_key)
            img = Image.open(io.BytesIO(self.image_bytes))

            prompt = """You are an expert OCR system designed to parse flight schedule tables from images.
Analyze the image and extract all flight data rows you can find.
Your response MUST be a valid JSON array of objects with keys:
arrival_flight, departure_flight, route, aircraft, team."""

            model = genai.GenerativeModel("gemini-1.5-flash")
            response = model.generate_content([prompt, img])
            try:
                data = json.loads(response.text)
            except json.JSONDecodeError:
                data = []

            command = {"command": f"ai sync {json.dumps(data)}", "group": self.reporting_group}
            self.command_queue.put(command)
            self.submit_report("Đã xử lý ảnh bằng Gemini.", is_final=True)
        except Exception as e:  # pragma: no cover - defensive
            logger.error(f"[{self.name}] Gemini OCR worker failed: {e}", exc_info=True)
            self.submit_report("Không thể xử lý hình ảnh.", is_final=True)

