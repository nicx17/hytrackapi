import os
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import logging
from datetime import datetime
from bs4 import BeautifulSoup

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from dotenv import load_dotenv
import asyncio


class Config:
    """Configuration class to manage API tracking constants."""

    load_dotenv()
    REQUEST_TIMEOUT = 15


# Use a specific logger for the trackers module so it doesn't pollute uvicorn logs too much
logger = logging.getLogger("trackers")
logger.setLevel(logging.INFO)
if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setFormatter(
        logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    logger.addHandler(ch)
    logger.addHandler(ch)


# Global semaphore to limit concurrent headless Chrome instances to 2
chrome_semaphore = asyncio.Semaphore(2)


class BrowserManager:
    """Context manager for initializing and cleaning up a headless Selenium Chrome WebDriver."""

    def __init__(self):
        self.driver = None

    def __enter__(self):
        logger.info("Configuring headless Chrome WebDriver environments...")
        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")

        import platform

        arch = platform.machine()

        if arch in ["aarch64", "arm64", "armv7l"]:
            logger.info(
                f"ARM architecture ({arch}) detected. Using system ChromeDriver."
            )
            options.binary_location = "/usr/bin/chromium-browser"
            service = Service("/usr/bin/chromedriver")
        else:
            logger.debug(
                f"x86 architecture ({arch}) detected. Installing via webdriver_manager..."
            )
            service = Service(ChromeDriverManager().install())

        self.driver = webdriver.Chrome(service=service, options=options)
        logger.info("Chrome WebDriver initialized and ready for scraping.")
        return self.driver

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.driver is not None:
            logger.info("Tearing down Chrome WebDriver session.")
            self.driver.quit()


class BlueDartTracker:
    """Tracker implementation for fetching Blue Dart shipment statuses."""

    def __init__(self, waybill, session=None):
        self.waybill = waybill
        self.url = f"https://www.bluedart.com/trackdartresultthirdparty?trackFor=0&trackNo={waybill}"
        if session:
            self.session = session
        else:
            self.session = requests.Session()
            retries = Retry(
                total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504]
            )
            self.session.mount("https://", HTTPAdapter(max_retries=retries))
            self.session.mount("http://", HTTPAdapter(max_retries=retries))

    def fetch_latest_event(self, **kwargs):
        """Fetches and parses the latest tracking event from Blue Dart."""
        try:
            logger.debug("Fetching Blue Dart status: waybill=%s", self.waybill)
            response = self.session.get(
                self.url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=Config.REQUEST_TIMEOUT,
            )
            if response.status_code != 200:
                logger.warning(
                    "Blue Dart returned non-200 response: waybill=%s status_code=%s",
                    self.waybill,
                    response.status_code,
                )
                return None

            soup = BeautifulSoup(response.text, "html.parser")
            container = soup.find("div", id=f"SCAN{self.waybill}")
            if not container:
                logger.warning(
                    "No Blue Dart tracking data found: waybill=%s", self.waybill
                )
                return None

            row = container.find("table").find("tbody").find_all("tr")[0]
            cols = row.find_all("td")

            return {
                "Courier": "Blue Dart",
                "Location": cols[0].text.strip(),
                "Details": cols[1].text.strip(),
                "Date": cols[2].text.strip(),
                "Time": cols[3].text.strip(),
                "Link": self.url,
            }
        except Exception:
            logger.exception("Blue Dart fetch failed: waybill=%s", self.waybill)
            return None


class DelhiveryTracker:
    """Tracker implementation for fetching Delhivery shipment statuses using Selenium."""

    def __init__(self, waybill):
        self.waybill = waybill
        self.url = f"https://www.delhivery.com/track-v2/package/{self.waybill}"

    async def fetch_latest_event(self, driver=None):
        """Fetches and parses the latest tracking event from Delhivery via a live browser instance."""
        if not driver:
            logger.error("Delhivery tracker requires a Selenium driver instance")
            return None

        try:
            logger.debug("Fetching Delhivery status: waybill=%s", self.waybill)
            driver.get(self.url)
            wait = WebDriverWait(driver, 25)

            delivered_header_xpath = "//h2[contains(text(), 'Order Delivered')]"
            dot_xpath = "//span[contains(@class, 'animate-ping')]"
            combined_xpath = f"{delivered_header_xpath} | {dot_xpath}"

            wait.until(EC.presence_of_element_located((By.XPATH, combined_xpath)))

            if driver.find_elements(By.XPATH, delivered_header_xpath):
                return {
                    "Courier": "Delhivery",
                    "Location": "Final Destination",
                    "Details": "Delivered: Your order has been delivered",
                    "Date": datetime.now().strftime("%Y-%m-%d"),
                    "Time": datetime.now().strftime("%H:%M"),
                    "Link": self.url,
                }

            row = driver.find_element(
                By.XPATH,
                f"{dot_xpath}/ancestor::div[contains(@class, 'flex') and contains(@class, 'gap-4')][1]",
            )
            status = row.find_element(
                By.XPATH, ".//span[contains(@style, 'font-weight: 600')]"
            ).text.strip()

            try:
                desc = row.find_element(
                    By.XPATH,
                    ".//div[contains(@class, 'text-[#525B7A]') or contains(@class, 'font-[400]')]",
                ).text.strip()
            except Exception:
                desc = "Update available"

            return {
                "Courier": "Delhivery",
                "Location": "Tracking Timeline",
                "Details": f"{status}: {desc}",
                "Date": datetime.now().strftime("%Y-%m-%d"),
                "Time": datetime.now().strftime("%H:%M"),
                "Link": self.url,
            }
        except Exception:
            logger.exception("Delhivery fetch failed: waybill=%s", self.waybill)
            return None
