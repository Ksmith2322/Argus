"""ATT BGW320 Wi-Fi Control -- enable/disable radios via browser automation.

Usage:
    python ops/wifi_control.py enable
    python ops/wifi_control.py disable

Task Scheduler:
    C:\\Argus\\.venv\\Scripts\\python.exe C:\\Argus\\repo\\ops\\wifi_control.py enable
    C:\\Argus\\.venv\\Scripts\\python.exe C:\\Argus\\repo\\ops\\wifi_control.py disable
"""
import sys
import time
from datetime import datetime
from pathlib import Path

LOG_FILE = Path("C:/Argus/repo/ops/logs/wifi_control.log")
GATEWAY_URL = "https://192.168.1.254/cgi-bin/wconfig.ha"
PASSWORD = "3<>0#%88%3"


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts}  {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("enable", "disable"):
        print("Usage: python wifi_control.py [enable|disable]")
        sys.exit(1)

    action = sys.argv[1]
    wifi_value = "on" if action == "enable" else "off"
    log(f"=== Wi-Fi {action} (radios -> '{wifi_value}') ===")

    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.support.ui import WebDriverWait, Select
    from selenium.webdriver.support import expected_conditions as EC

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--ignore-certificate-errors")
    opts.add_argument("--allow-insecure-localhost")
    # Enable cookies (default, but be explicit)
    opts.add_argument("--enable-features=SameSiteByDefaultCookies")

    driver = None
    try:
        log("Starting Chrome...")
        driver = webdriver.Chrome(options=opts)
        driver.set_page_load_timeout(30)

        # Step 1: Load login page (double-load needed for cookie establishment)
        log("Loading gateway (cookie handshake)...")
        driver.get(GATEWAY_URL)
        time.sleep(3)
        # Second load with cookie now set
        driver.get(GATEWAY_URL)
        time.sleep(3)

        # Force body visible in case anti-clickjack didn't self-remove
        driver.execute_script("""
            document.body.style.display = 'block';
            var ac = document.getElementById('antiClickjack');
            if (ac) ac.parentNode.removeChild(ac);
        """)
        time.sleep(1)

        page = driver.page_source
        log(f"Page loaded ({len(page)} bytes)")

        # Step 2: Login if needed
        if "Access Code" in page or "password" in page.lower():
            log("Login required...")

            # Wait for password field
            try:
                pw_field = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.NAME, "password"))
                )
            except Exception:
                # Try by ID
                pw_field = driver.find_element(By.ID, "password")

            pw_field.clear()
            pw_field.send_keys(PASSWORD)
            log("Password entered")

            # Find and click Continue
            try:
                btn = driver.find_element(By.CSS_SELECTOR, "input[value='Continue']")
            except Exception:
                btn = driver.find_element(By.NAME, "Continue")
            btn.click()
            log("Continue clicked")

            time.sleep(5)
            page = driver.page_source

            if "wl80211on" in page or "Wi-Fi Operation" in page:
                log("Login SUCCESS -- on config page")
            elif "Access Code" in page:
                log("Login FAILED -- bad password?")
                driver.save_screenshot("C:/Argus/repo/ops/logs/wifi_login_fail.png")
                sys.exit(1)
            else:
                # Try navigating to config
                driver.get(GATEWAY_URL)
                time.sleep(3)
                page = driver.page_source
                if "wl80211on" not in page and "Wi-Fi Operation" not in page:
                    log("ERROR: Cannot reach config after login")
                    sys.exit(1)
                log("Login SUCCESS (redirect)")

        elif "wl80211on" in page or "Wi-Fi Operation" in page:
            log("Already authenticated")
        else:
            log("ERROR: Unexpected page state")
            driver.save_screenshot("C:/Argus/repo/ops/logs/wifi_unexpected.png")
            sys.exit(1)

        # Step 3: Toggle 2.4GHz
        log(f"Setting 2.4GHz -> {wifi_value}...")
        try:
            sel24 = Select(driver.find_element(By.ID, "radioon"))
            current = sel24.first_selected_option.get_attribute("value")
            log(f"  2.4GHz current: {current}")
            if current != wifi_value:
                sel24.select_by_value(wifi_value)
                time.sleep(5)  # auto-submits
                log(f"  2.4GHz -> {wifi_value} DONE")
                # Page reloads after submit, re-navigate
                driver.get(GATEWAY_URL)
                time.sleep(3)
            else:
                log(f"  2.4GHz already {wifi_value}")
        except Exception as e:
            log(f"  2.4GHz error: {e}")

        # Step 4: Toggle 5GHz
        log(f"Setting 5GHz -> {wifi_value}...")
        try:
            sel5 = Select(driver.find_element(By.ID, "radioon_5"))
            current5 = sel5.first_selected_option.get_attribute("value")
            log(f"  5GHz current: {current5}")
            if current5 != wifi_value:
                sel5.select_by_value(wifi_value)
                time.sleep(5)
                log(f"  5GHz -> {wifi_value} DONE")
            else:
                log(f"  5GHz already {wifi_value}")
        except Exception as e:
            log(f"  5GHz error: {e}")

        log(f"=== Wi-Fi {action} COMPLETE ===")

    except Exception as e:
        log(f"ERROR: {e}")
        if driver:
            driver.save_screenshot("C:/Argus/repo/ops/logs/wifi_error.png")
        sys.exit(1)
    finally:
        if driver:
            driver.quit()


if __name__ == "__main__":
    main()
