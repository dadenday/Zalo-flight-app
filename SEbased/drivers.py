"""Browser driver setup utilities."""

from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.options import Options as ChromeOptions
from webdriver_manager.chrome import ChromeDriverManager


def get_chrome_options(profile_dir: str, is_gui: bool = False) -> ChromeOptions:
    """Configure Chrome options for a specific profile."""

    options = ChromeOptions()
    options.add_argument(f"user-data-dir={profile_dir}")
    if not is_gui:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,800")
    options.add_experimental_option(
        "prefs", {"profile.default_content_setting_values.notifications": 2}
    )
    return options


def setup_drivers(is_gui: bool = False):
    """Create Zalo and worker Chrome drivers."""

    zalo_options = get_chrome_options("zalo_profile", is_gui)
    zalo_driver = webdriver.Chrome(
        service=ChromeService(ChromeDriverManager().install()), options=zalo_options
    )

    worker_options = get_chrome_options("worker_profile", is_gui)
    worker_driver = webdriver.Chrome(
        service=ChromeService(ChromeDriverManager().install()), options=worker_options
    )
    return zalo_driver, worker_driver

