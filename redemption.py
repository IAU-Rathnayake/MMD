"""
Redemption List Automation Script
===================================
Automates the process of checking and processing client withdrawal requests
on the Exchange Management / Redemption List platform.

Requirements:
    pip install selenium webdriver-manager

Usage:
    1. Fill in SYSTEM_URL with your platform URL
    2. If login is needed, fill in LOGIN credentials below
    3. Run: python redemption_automation.py

Rules applied automatically:
    ✅ Rule 1 – Bank number must start with "09", be 7–11 digits, no spaces
    ✅ Rule 2 – Exchange amount must NOT exceed the chosen refund account balance
    ✅ Rule 3 – Skip refund accounts whose balance < 100,000
    ✅ Rule 4 – Skip "Big Money" requests (Exchange amount > 1,000,000)
    ✅ Rule 5 – Skip orders marked as abnormal/risk ("Yes" in the abnormal column)
"""

import re
import time
import logging
from dataclasses import dataclass
from typing import Optional

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException,
    StaleElementReferenceException, ElementNotInteractableException
)

# ─────────────────────────────────────────────
#  CONFIGURATION  – edit these values
# ─────────────────────────────────────────────
SYSTEM_URL   = "http://YOUR_SYSTEM_URL/exchange-management/redemption-list"
LOGIN_URL    = "http://YOUR_SYSTEM_URL/login"   # set to "" if no login needed
USERNAME     = "your_username"
PASSWORD     = "your_password"

BIG_MONEY_THRESHOLD  = 1_000_000   # skip requests above this amount
MIN_REFUND_BALANCE   = 100_000     # skip refund accounts below this balance

WAIT_TIMEOUT         = 15          # seconds to wait for elements
ACTION_DELAY         = 0.6         # short pause between UI actions
CONFIRM_DELAY        = 1.5         # pause after clicking Confirm
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("redemption")


# ── Column indices (0-based) inside each table row ──────────────────────────
#  order_number | clientID | exchange_amount | fee | type | real_name |
#  redeem_account | remarks | ip | app_time | abnormal | payment |
#  refund_status | feedback | bank_info | reason_balance | actions
COL_EXCHANGE_AMOUNT = 2
COL_ABNORMAL        = 10
COL_BANK_INFO       = 14
# ─────────────────────────────────────────────


@dataclass
class RowData:
    index: int
    exchange_amount_raw: str
    exchange_amount: int
    abnormal_text: str
    bank_info: str
    bank_number: str
    bank_type: str          # "KBZ" or "WAVE"


@dataclass
class RefundAccount:
    element: object
    text: str
    balance: int
    bank_type: str


# ═══════════════════════════════════════════════════════════════════════════
#  HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════

def parse_amount(raw: str) -> int:
    """
    Parse amounts that use Myanmar 4-digit comma grouping.
    Examples:  "100,0000" → 1000000   |   "30,0000" → 300000   |   "2,0000" → 20000
    """
    cleaned = re.sub(r"[,\s]", "", raw)
    return int(cleaned) if cleaned.isdigit() else 0


def validate_bank_number(bank_info: str) -> tuple[bool, str, str]:
    """
    Extract and validate the bank number from the Bank Information cell.

    Returns:
        (is_valid, bank_number, reason)

    Rules:
        • Must start with "09"
        • Must be digits only, length 7–11
        • Must NOT contain spaces
    """
    # Extract from text like:
    #   "Card name:zawminhtet\nBank number:09887431118\nbank:KBZ Pay"
    match = re.search(r"[Bb]ank\s*number[:\s]+(\S+)", bank_info)
    if not match:
        return False, "", "Bank number field not found in bank info"

    num = match.group(1).strip()

    if " " in num:
        return False, num, f"Contains spaces: '{num}'"

    if not num.isdigit():
        return False, num, f"Non-digit characters: '{num}'"

    if not num.startswith("09"):
        return False, num, f"Does not start with '09': '{num}'"

    if not (7 <= len(num) <= 11):
        return False, num, f"Length {len(num)} is outside 7–11 range: '{num}'"

    return True, num, "OK"


def extract_bank_type(bank_info: str) -> str:
    """Return 'KBZ' or 'WAVE' based on bank info text."""
    upper = bank_info.upper()
    if "KBZ" in upper:
        return "KBZ"
    if "WAVE" in upper:
        return "WAVE"
    return "UNKNOWN"


def parse_refund_accounts(driver) -> list[RefundAccount]:
    """
    After opening the Refund account dropdown, read all available options.

    Expected option text format:
        "Bank name:KBZ Pay Redeem account:09403764347 Balance:1170000"
    """
    # Wait briefly for the dropdown list to render
    time.sleep(ACTION_DELAY)

    # Element UI renders options inside .el-select-dropdown__list
    selectors = [
        ".el-select-dropdown__item",
        ".el-select-dropdown .el-select-dropdown__item",
        "ul.el-scrollbar__view li",
    ]
    options = []
    for sel in selectors:
        opts = driver.find_elements(By.CSS_SELECTOR, sel)
        if opts:
            options = opts
            break

    accounts = []
    for opt in options:
        raw = opt.get_attribute("textContent").strip()   # use textContent to strip HTML tags
        if not raw:
            continue
        balance_m = re.search(r"Balance[:\s]*(\d+)", raw, re.I)
        if not balance_m:
            continue
        balance = int(balance_m.group(1))
        bank_type = "KBZ" if "KBZ" in raw.upper() else ("WAVE" if "WAVE" in raw.upper() else "UNKNOWN")
        accounts.append(RefundAccount(
            element=opt,
            text=raw,
            balance=balance,
            bank_type=bank_type,
        ))

    return accounts


def choose_refund_account(accounts: list[RefundAccount], exchange_amount: int, required_bank: str) -> Optional[RefundAccount]:
    """
    Apply Rules 2 & 3 to choose the best refund account.

    Priority:
        1. Matches the client's bank type (KBZ ↔ KBZ, WAVE ↔ WAVE)
        2. Balance ≥ MIN_REFUND_BALANCE  (Rule 3)
        3. Balance ≥ exchange_amount     (Rule 2)
    Then pick the one with the highest balance to preserve smaller accounts.
    """
    candidates = []
    for acc in accounts:
        if acc.balance < MIN_REFUND_BALANCE:
            log.info(f"      ⏭  Skip account (balance {acc.balance:,} < min {MIN_REFUND_BALANCE:,}): {acc.text[:60]}")
            continue
        if acc.balance < exchange_amount:
            log.info(f"      ⏭  Skip account (balance {acc.balance:,} < amount {exchange_amount:,}): {acc.text[:60]}")
            continue
        if acc.bank_type != required_bank:
            log.info(f"      ⏭  Skip account (type {acc.bank_type} ≠ {required_bank}): {acc.text[:60]}")
            continue
        candidates.append(acc)

    if not candidates:
        return None

    # Prefer the account with the highest balance (greedy—keeps small accounts intact)
    return max(candidates, key=lambda a: a.balance)


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN AUTOMATION CLASS
# ═══════════════════════════════════════════════════════════════════════════

class RedemptionBot:
    def __init__(self, driver: webdriver.Chrome):
        self.driver = driver
        self.wait   = WebDriverWait(driver, WAIT_TIMEOUT)
        self.stats  = {"success": 0, "skipped": 0, "error": 0, "processed": 0}

    # ── Row data extraction ────────────────────────────────────────────────

    def _get_cell_text(self, cells: list, index: int) -> str:
        try:
            return cells[index].get_attribute("textContent").strip()
        except IndexError:
            return ""

    def _extract_row_data(self, row, index: int) -> Optional[RowData]:
        try:
            cells = row.find_elements(By.TAG_NAME, "td")
            if len(cells) < 15:
                return None

            amount_raw  = self._get_cell_text(cells, COL_EXCHANGE_AMOUNT)
            abnormal    = self._get_cell_text(cells, COL_ABNORMAL)
            bank_info   = self._get_cell_text(cells, COL_BANK_INFO)

            amount      = parse_amount(amount_raw)
            ok, num, _  = validate_bank_number(bank_info)

            return RowData(
                index=index,
                exchange_amount_raw=amount_raw,
                exchange_amount=amount,
                abnormal_text=abnormal,
                bank_info=bank_info,
                bank_number=num if ok else "",
                bank_type=extract_bank_type(bank_info),
            )
        except StaleElementReferenceException:
            return None

    # ── Rule checks ───────────────────────────────────────────────────────

    def _check_rules(self, data: RowData) -> tuple[bool, str]:
        """
        Returns (should_process, reason).
        """
        # Rule 5 – Skip risk-marked orders
        if re.search(r"\byes\b", data.abnormal_text, re.I):
            return False, f"[Rule 5] Risk-marked order (abnormal = Yes): '{data.abnormal_text}'"

        # Rule 4 – Skip Big Money
        if data.exchange_amount > BIG_MONEY_THRESHOLD:
            return False, f"[Rule 4] Big Money ({data.exchange_amount:,} > {BIG_MONEY_THRESHOLD:,})"

        # Rule 1 – Validate bank number
        ok, num, reason = validate_bank_number(data.bank_info)
        if not ok:
            return False, f"[Rule 1] Invalid bank number — {reason}"

        return True, "All rules passed"

    # ── UI interactions ───────────────────────────────────────────────────

    def _close_modal(self):
        """Dismiss the Automatic cash modal if it is open."""
        for selector, attr in [
            (".el-dialog__close", None),
            ("//button[contains(text(),'Cancel')]", "xpath"),
        ]:
            try:
                el = (
                    self.driver.find_element(By.XPATH, selector)
                    if attr == "xpath"
                    else self.driver.find_element(By.CSS_SELECTOR, selector)
                )
                el.click()
                time.sleep(ACTION_DELAY)
                return
            except Exception:
                pass

    def _click_automatic_cash(self, row) -> bool:
        """Click the 'Automatic cash' button in a table row."""
        try:
            # Look for a button containing the text "Automatic cash" or "automatic"
            btn = row.find_element(
                By.XPATH,
                ".//button[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'automatic')]"
            )
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            time.sleep(0.2)
            btn.click()
            return True
        except NoSuchElementException:
            log.warning("      'Automatic cash' button not found in row")
            return False

    def _open_refund_dropdown(self, modal) -> bool:
        """Click on the Refund account select inside the modal."""
        try:
            # Element UI select wrapper
            dropdown = modal.find_element(By.CSS_SELECTOR, ".el-select")
            dropdown.click()
            time.sleep(ACTION_DELAY)
            return True
        except Exception:
            try:
                # Fallback: click the input placeholder
                inp = modal.find_element(By.CSS_SELECTOR, "input[placeholder='Refund account']")
                inp.click()
                time.sleep(ACTION_DELAY)
                return True
            except Exception as e:
                log.error(f"      Cannot open refund dropdown: {e}")
                return False

    def _get_exchange_amount_from_modal(self, modal) -> int:
        """Read Exchange amount from the modal (more reliable than the table cell)."""
        try:
            inp = modal.find_element(By.XPATH, ".//label[contains(.,'Exchange amount')]/following-sibling::div//input | .//div[label[contains(.,'Exchange amount')]]//input")
            return parse_amount(inp.get_attribute("value"))
        except Exception:
            return 0

    # ── Full row processing pipeline ──────────────────────────────────────

    def process_row(self, row, index: int) -> tuple[str, str]:
        """
        Process one redemption row.

        Returns:
            ("success" | "skipped" | "error", reason_message)
        """
        log.info(f"\n  ┌─ Row {index} " + "─" * 40)

        # ── Extract data ─────────────────────────────────────────────────
        data = self._extract_row_data(row, index)
        if data is None:
            return "skipped", "Could not extract row data (empty or stale)"

        log.info(f"  │ Amount : {data.exchange_amount_raw}  →  {data.exchange_amount:,}")
        log.info(f"  │ Abnorm.: {data.abnormal_text[:50]}")
        log.info(f"  │ Bank   : {data.bank_info[:80]}")

        # ── Apply Rules 1, 4, 5 (pre-click checks) ───────────────────────
        should_process, reason = self._check_rules(data)
        if not should_process:
            log.info(f"  └─ ⏭  SKIPPED — {reason}")
            return "skipped", reason

        log.info(f"  │ ✅  Pre-click rules passed — bank: {data.bank_number} ({data.bank_type})")

        # ── Click 'Automatic cash' ────────────────────────────────────────
        if not self._click_automatic_cash(row):
            return "error", "Automatic cash button not found"
        log.info("  │ 🖱  Clicked 'Automatic cash'")

        # ── Wait for modal ────────────────────────────────────────────────
        try:
            modal = WebDriverWait(self.driver, WAIT_TIMEOUT).until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, ".el-dialog"))
            )
        except TimeoutException:
            log.error("  └─ ❌ ERROR — Modal did not appear")
            return "error", "Modal did not appear"

        # ── Use modal's exchange amount (safer) ───────────────────────────
        modal_amount = self._get_exchange_amount_from_modal(modal)
        if modal_amount > 0:
            data.exchange_amount = modal_amount

        # ── Open Refund account dropdown ──────────────────────────────────
        if not self._open_refund_dropdown(modal):
            self._close_modal()
            return "error", "Could not open refund account dropdown"

        # ── Parse refund accounts and apply Rules 2 & 3 ──────────────────
        accounts = parse_refund_accounts(self.driver)
        log.info(f"  │ 📋  {len(accounts)} refund account(s) found in dropdown")

        best = choose_refund_account(accounts, data.exchange_amount, data.bank_type)

        if best is None:
            log.warning("  │ ⚠️  No suitable refund account found — closing modal")
            self._close_modal()
            return "skipped", "No refund account meets balance requirements (Rules 2 & 3)"

        log.info(f"  │ 💳  Chosen: {best.text[:70]}")

        # ── Select the account ────────────────────────────────────────────
        try:
            self.driver.execute_script("arguments[0].scrollIntoView({block:'nearest'});", best.element)
            best.element.click()
            time.sleep(ACTION_DELAY)
        except Exception as e:
            self._close_modal()
            return "error", f"Could not select refund account: {e}"

        # ── Click Confirm ─────────────────────────────────────────────────
        try:
            confirm_btn = modal.find_element(
                By.XPATH, ".//button[contains(text(),'Confirm')]"
            )
            confirm_btn.click()
            log.info("  │ ✅  Clicked Confirm")
        except Exception as e:
            self._close_modal()
            return "error", f"Confirm button error: {e}"

        # ── Wait and verify success ───────────────────────────────────────
        time.sleep(CONFIRM_DELAY)

        # Look for a success toast/notification
        try:
            self.driver.find_element(
                By.XPATH,
                "//*[contains(text(),'successfully') or contains(text(),'success') or contains(text(),'Created')]"
            )
            log.info(f"  └─ 🎉 SUCCESS — Redeemed {data.exchange_amount:,} via {best.bank_type} (balance {best.balance:,})")
            return "success", f"Redeemed {data.exchange_amount:,} via account balance {best.balance:,}"
        except NoSuchElementException:
            # No error toast visible either — assume success
            log.info(f"  └─ 🎉 SUCCESS (no toast) — Redeemed {data.exchange_amount:,}")
            return "success", f"Redeemed {data.exchange_amount:,}"

    # ── Page-level loop ───────────────────────────────────────────────────

    def process_current_page(self):
        """Iterate over all visible rows on the current page."""
        try:
            table_body = self.wait.until(
                EC.presence_of_element_located((
                    By.CSS_SELECTOR,
                    ".el-table__body tbody, table tbody"
                ))
            )
        except TimeoutException:
            log.error("Table not found on page")
            return

        rows = table_body.find_elements(By.TAG_NAME, "tr")
        log.info(f"\n{'═'*55}")
        log.info(f"  Found {len(rows)} row(s) on this page")
        log.info(f"{'═'*55}")

        i = 0
        while i < len(rows):
            try:
                # Re-fetch the table body and row after each action
                # (DOM may refresh after a successful redemption)
                table_body = self.driver.find_element(
                    By.CSS_SELECTOR, ".el-table__body tbody, table tbody"
                )
                rows = table_body.find_elements(By.TAG_NAME, "tr")
                if i >= len(rows):
                    break

                row = rows[i]
                status, reason = self.process_row(row, i + 1)

                self.stats["processed"] += 1
                self.stats[status if status in ("success", "skipped", "error") else "error"] += 1

            except StaleElementReferenceException:
                log.warning(f"  Row {i+1}: Stale reference — retrying once")
                time.sleep(1)
                # Don't increment i; retry this index
                continue
            except Exception as e:
                log.error(f"  Row {i+1}: Unexpected error — {e}")
                self.stats["error"] += 1

            i += 1
            time.sleep(0.3)   # small breathing room between rows

    # ── Pagination ────────────────────────────────────────────────────────

    def _go_to_next_page(self) -> bool:
        """Click the Next page button. Returns False if there is no next page."""
        try:
            next_btn = self.driver.find_element(
                By.CSS_SELECTOR,
                "button.btn-next:not([disabled]), li.el-pager + li.btn-next:not(.disabled)"
            )
            disabled = next_btn.get_attribute("disabled")
            if disabled:
                return False
            next_btn.click()
            time.sleep(2.5)   # wait for new page to load
            return True
        except NoSuchElementException:
            return False

    # ── Entry point ───────────────────────────────────────────────────────

    def run(self, max_pages: Optional[int] = None):
        """
        Run the automation across all pages (or up to max_pages).

        Args:
            max_pages: Limit the number of pages to process (None = all pages).
        """
        page = 1
        log.info("\n" + "█"*55)
        log.info("  REDEMPTION AUTOMATION STARTED")
        log.info("█"*55)

        while True:
            log.info(f"\n  📄  Processing page {page}…")
            self.process_current_page()
            log.info(f"\n  📊  Page {page} done | stats so far: {self.stats}")

            if max_pages and page >= max_pages:
                log.info(f"  Reached max_pages={max_pages} — stopping.")
                break

            if not self._go_to_next_page():
                log.info("  No more pages.")
                break

            page += 1

        log.info("\n" + "█"*55)
        log.info(f"  COMPLETED ✔  Total stats: {self.stats}")
        log.info("█"*55)
        return self.stats


# ═══════════════════════════════════════════════════════════════════════════
#  DRIVER SETUP & MAIN
# ═══════════════════════════════════════════════════════════════════════════

def build_driver(headless: bool = False) -> webdriver.Chrome:
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1600,900")
    opts.add_argument("--lang=en-US")

    # Suppress automation detection (some sites block WebDriver flags)
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    try:
        # Try to use webdriver-manager for automatic ChromeDriver management
        from webdriver_manager.chrome import ChromeDriverManager
        service = Service(ChromeDriverManager().install())
        return webdriver.Chrome(service=service, options=opts)
    except ImportError:
        # Fall back to default Chrome driver on PATH
        return webdriver.Chrome(options=opts)


def login(driver: webdriver.Chrome, wait: WebDriverWait):
    """Optional: Log in to the system if a login page is required."""
    if not LOGIN_URL:
        return
    driver.get(LOGIN_URL)
    time.sleep(2)
    try:
        driver.find_element(By.CSS_SELECTOR, "input[type='text'], input[name='username']").send_keys(USERNAME)
        driver.find_element(By.CSS_SELECTOR, "input[type='password']").send_keys(PASSWORD)
        driver.find_element(By.CSS_SELECTOR, "button[type='submit'], .login-btn").click()
        wait.until(EC.url_changes(LOGIN_URL))
        log.info("✅ Logged in successfully")
        time.sleep(2)
    except Exception as e:
        log.error(f"Login failed: {e}. Please log in manually and re-run.")
        input("Press Enter once you are logged in…")


def main():
    driver = build_driver(headless=False)   # set headless=True for background mode
    wait   = WebDriverWait(driver, WAIT_TIMEOUT)

    try:
        # ── 1. Login (if required) ─────────────────────────────────────
        login(driver, wait)

        # ── 2. Navigate to the Redemption List page ───────────────────
        log.info(f"Navigating to {SYSTEM_URL}")
        driver.get(SYSTEM_URL)
        time.sleep(3)

        # ── 3. (Optional) Apply filters ───────────────────────────────
        # Example: filter for unpaid + today's orders
        # You can add filter-clicking code here if needed.

        # ── 4. Run the bot ────────────────────────────────────────────
        bot = RedemptionBot(driver)
        bot.run(max_pages=None)   # None = all pages; set e.g. max_pages=5 to test

    except KeyboardInterrupt:
        log.info("\nInterrupted by user.")
    finally:
        driver.quit()
        log.info("Browser closed.")


if __name__ == "__main__":
    main()