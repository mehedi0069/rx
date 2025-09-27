import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import telegram
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from datetime import datetime
import re
from bs4 import BeautifulSoup
import os
import asyncio
import json
import pycountry
import time
from threading import Lock
import signal
import ssl
import sys
import logging
import html

# Configure logging for service
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Brotli import handling
try:
    import brotli
except ImportError:
    try:
        import brotlicffi as brotli
    except ImportError:
        brotli = None
        logger.warning("Brotli compression not available")

# Configuration - Use environment variables
BOT_TOKEN = os.environ.get("BOT_TOKEN", "7962815200:AAFSgOWOPRaOkbAcTXw_c9s93uR20J9xVvU")
CHAT_IDS = json.loads(os.environ.get("CHAT_IDS", '["-1003074370574", "-1002760500138"]'))
TIMEOUT = (10, 30)
MAX_RETRIES = 3
MAX_BATCH_SIZE = 20
BATCH_DELAY = 3
OTP_DUPLICATE_WINDOW = 300  # 5 minutes (reduced from 10)

# IVASMS configuration - Use environment variables
LOGIN_URL = "https://www.ivasms.com/login"
SMS_LIST_URL = "https://www.ivasms.com/portal/sms/received/getsms"
SMS_NUMBERS_URL = "https://www.ivasms.com/portal/sms/received/getsms/number"
SMS_DETAILS_URL = "https://www.ivasms.com/portal/sms/received/getsms/number/sms"
EMAIL = os.environ.get("IVASMS_EMAIL", "siamrx520@gmail.com")
PASSWORD = os.environ.get("IVASMS_PASSWORD", "Siam@7492@N")
MAX_LOGIN_ATTEMPTS = 3
RETRY_DELAY = 10
SESSION_REFRESH_INTERVAL = 1800  # 30 minutes

# Flag API configuration
FLAG_API_URL = "https://siyamahmmed.shop/flag.php"

# File paths
OTP_HISTORY_FILE = "otp_history.json"
SMS_CACHE_FILE = "sms_cache.json"
file_lock = Lock()

# Configure session
session = requests.Session()
adapter = requests.adapters.HTTPAdapter(
    max_retries=MAX_RETRIES,
    pool_connections=10,
    pool_maxsize=100
)
session.mount("https://", adapter)
session.verify = False  # Disable SSL verification

COUNTRY_ALIASES = {
    "Ivory": "Côte d'Ivoire",
    "USA": "United States",
    "UK": "United Kingdom",
    "UAE": "United Arab Emirates",
    "Benin": "Benin"
}

SMS_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.ivasms.com/portal/sms/received",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Accept": "text/html, */*; q=0.01",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept-Encoding": "gzip, deflate"
}

SERVICE_PATTERNS = {
    "WhatsApp": r"(whatsapp|wa\.me|verify|wassap|whtsapp)",
    "Facebook": r"(facebook|fb\.me|fb\-|meta)",
    "Telegram": r"(telegram|t\.me|tg|telegrambot)",
    "Google": r"(google|gmail|goog|g\.co|accounts\.google)",
    "Twitter": r"(twitter|x\.com|twtr)",
    "Instagram": r"(instagram|insta|ig)",
    "Lalamove": r"(lalamove)",
    "Apple": r"(apple|icloud|appleid)"
}

# Cache for flag data
flag_cache = {}
last_flag_cache_update = 0
FLAG_CACHE_DURATION = 86400  # 24 hours

def load_flag_data():
    global flag_cache, last_flag_cache_update
    
    # Check if cache is still valid
    current_time = time.time()
    if current_time - last_flag_cache_update < FLAG_CACHE_DURATION and flag_cache:
        return flag_cache
    
    try:
        response = requests.get(FLAG_API_URL, timeout=10)
        if response.status_code == 200:
            flag_data = response.json()
            # Create a mapping from country name to emoji
            for country in flag_data:
                flag_cache[country['name'].lower()] = country['emoji']
            
            # Also add aliases
            for alias, country_name in COUNTRY_ALIASES.items():
                if country_name.lower() in flag_cache:
                    flag_cache[alias.lower()] = flag_cache[country_name.lower()]
            
            last_flag_cache_update = current_time
            logger.info(f"Loaded {len(flag_cache)} country flags from API")
            return flag_cache
        else:
            logger.error(f"Failed to fetch flag data: HTTP {response.status_code}")
            return {}
    except Exception as e:
        logger.error(f"Error loading flag data: {str(e)}")
        return {}

def get_country_emoji(country_name):
    if not country_name or country_name == "Unknown":
        return "🌍"
    
    # Load flag data if not already loaded
    if not flag_cache:
        load_flag_data()
    
    # Try exact match
    country_name_lower = country_name.lower()
    if country_name_lower in flag_cache:
        return flag_cache[country_name_lower]
    
    # Try with aliases
    country_name = COUNTRY_ALIASES.get(country_name, country_name)
    country_name_lower = country_name.lower()
    if country_name_lower in flag_cache:
        return flag_cache[country_name_lower]
    
    # Try fuzzy matching
    for cached_name, emoji in flag_cache.items():
        if country_name_lower in cached_name or cached_name in country_name_lower:
            return emoji
    
    # Fallback to pycountry if API didn't work
    try:
        country_name = COUNTRY_ALIASES.get(country_name, country_name)
        countries = pycountry.countries.search_fuzzy(country_name)
        if countries:
            # Generate flag emoji from country code
            country_code = countries[0].alpha_2
            code_points = [ord(c.upper()) - ord('A') + 0x1F1E6 for c in country_code]
            return chr(code_points[0]) + chr(code_points[1])
        return "🌍"
    except Exception:
        return "🌍"

def load_sms_cache():
    with file_lock:
        try:
            if os.path.exists(SMS_CACHE_FILE):
                with open(SMS_CACHE_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            return {}
        except Exception as e:
            logger.error(f"Error loading SMS cache: {str(e)}")
            return {}

def save_sms_cache(cache):
    with file_lock:
        try:
            with open(SMS_CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(cache, f, indent=4)
        except Exception as e:
            logger.error(f"Error saving SMS cache: {str(e)}")

def load_otp_history():
    with file_lock:
        try:
            if os.path.exists(OTP_HISTORY_FILE):
                with open(OTP_HISTORY_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            return {}
        except Exception as e:
            logger.error(f"Error loading OTP history: {str(e)}")
            return {}

def save_otp_history(history):
    with file_lock:
        try:
            with open(OTP_HISTORY_FILE, 'w', encoding='utf-8') as f:
                json.dump(history, f, indent=4)
        except Exception as e:
            logger.error(f"Error saving OTP history: {str(e)}")

def check_and_save_otp(number, otp, message_id):
    history = load_otp_history()
    current_time = datetime.now().isoformat()
    
    # Check if we've seen this exact OTP for this number recently
    if number not in history:
        history[number] = [{"otp": otp, "message_id": message_id, "timestamp": current_time}]
        save_otp_history(history)
        return True
    
    # Check if the same OTP was sent to this number recently
    for entry in history[number]:
        if entry["otp"] == otp:
            entry_time = datetime.fromisoformat(entry["timestamp"])
            if (datetime.now() - entry_time).total_seconds() < OTP_DUPLICATE_WINDOW:
                return False  # Duplicate OTP within time window
    
    # Different OTP for same number - allow it
    history[number].append({"otp": otp, "message_id": message_id, "timestamp": current_time})
    
    # Clean up old entries (keep only last 10 entries per number)
    if len(history[number]) > 10:
        history[number] = history[number][-10:]
    
    save_otp_history(history)
    return True

def format_otp_with_spaces(otp):
    if otp == "N/A":
        return "N/A"
    otp_clean = otp.replace(" ", "")
    return " ".join(otp_clean)

async def get_csrf_token():
    try:
        response = session.get(LOGIN_URL, timeout=TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        csrf_input = soup.find('input', {'name': '_token'})
        if csrf_input is None:
            logger.error("CSRF token input not found")
            return None
        csrf_token = csrf_input.get('value')
        if not csrf_token:
            logger.error("Empty CSRF token value")
            return None
        return csrf_token
    except Exception as e:
        logger.error(f"CSRF token error: {str(e)}")
        return None

async def login(attempt=1):
    if attempt > MAX_LOGIN_ATTEMPTS:
        logger.error("Max login attempts reached")
        return False
    
    try:
        csrf_token = await get_csrf_token()
        if not csrf_token:
            logger.warning(f"Login attempt {attempt}: No CSRF token")
            await asyncio.sleep(RETRY_DELAY)
            return await login(attempt + 1)
        
        login_data = {
            "_token": csrf_token,
            "email": EMAIL,
            "password": PASSWORD
        }
        login_response = session.post(LOGIN_URL, data=login_data, timeout=TIMEOUT)
        login_response.raise_for_status()
        
        if "dashboard" in login_response.url or login_response.status_code == 200:
            logger.info("Login successful")
            return True
        else:
            logger.warning(f"Login attempt {attempt} failed - Unexpected response")
            await asyncio.sleep(RETRY_DELAY)
            return await login(attempt + 1)
    except Exception as e:
        logger.error(f"Login error: {str(e)}")
        await asyncio.sleep(RETRY_DELAY)
        return await login(attempt + 1)

async def refresh_session(last_login_time):
    current_time = time.time()
    if current_time - last_login_time >= SESSION_REFRESH_INTERVAL:
        logger.info("Refreshing session...")
        if await login():
            return True, current_time
        else:
            return False, last_login_time
    return True, last_login_time

async def fetch_sms():
    try:
        csrf_token = await get_csrf_token()
        if not csrf_token:
            if await login():
                csrf_token = await get_csrf_token()
            if not csrf_token:
                logger.error("Failed to get CSRF token after login")
                return []
        
        headers = SMS_HEADERS.copy()
        headers["X-CSRF-TOKEN"] = csrf_token
        payload = f"_token={csrf_token}&from=&to="
        response = session.post(SMS_LIST_URL, headers=headers, data=payload, timeout=TIMEOUT)
        response.raise_for_status()
        
        content_encoding = response.headers.get('Content-Encoding', '')
        response_text = response.content
        
        # Handle Brotli decompression
        if content_encoding == 'br' and brotli:
            try:
                response_text = brotli.decompress(response_text).decode('utf-8')
            except Exception as e:
                logger.error(f"Brotli decompression error: {str(e)}")
                response_text = response_text.decode('utf-8', errors='replace')
        else:
            response_text = response_text.decode('utf-8', errors='replace')
        
        soup = BeautifulSoup(response_text, 'html.parser')
        items = soup.find_all('div', class_='item')
        sms_list = []
        sms_cache = load_sms_cache()
        
        for item in items:
            try:
                range_name = item.find('div', class_='col-sm-4')
                range_name = range_name.text.strip() if range_name else "Unknown"
                count = item.find('p', string=re.compile(r'^\d+$'))
                count = count.text if count else "0"
                number = range_name.split()[-1] if range_name else "Unknown"
                
                numbers = await fetch_numbers(range_name, csrf_token)
                
                for num in numbers:
                    try:
                        sms_details = await fetch_sms_details(num, range_name, csrf_token)
                        message_id = f"{num}_{sms_details.get('message', '')[:50]}"
                        if message_id in sms_cache:
                            continue
                        
                        country_name = extract_country(range_name)
                        country_emoji = get_country_emoji(country_name)
                        sms_entry = {
                            "range": range_name,
                            "count": count,
                            "country": country_name,
                            "country_emoji": country_emoji,
                            "service": sms_details.get('service', 'Unknown'),
                            "number": num,
                            "otp": extract_otp(sms_details.get('message', '')),
                            "full_message": sms_details.get('message', 'No message available'),
                            "message_id": message_id
                        }
                        sms_list.append(sms_entry)
                        sms_cache[message_id] = {"timestamp": datetime.now().isoformat()}
                        save_sms_cache(sms_cache)
                    except Exception as e:
                        logger.error(f"Error processing SMS details: {str(e)}")
                        continue
            except Exception as e:
                logger.error(f"Error processing SMS item: {str(e)}")
                continue
        
        return sms_list
    except Exception as e:
        logger.error(f"Error in fetch_sms: {str(e)}")
        return []

async def fetch_numbers(range_name, csrf_token):
    try:
        headers = SMS_HEADERS.copy()
        headers["X-CSRF-TOKEN"] = csrf_token
        payload = f"_token={csrf_token}&start=&end=&range={range_name}"
        response = session.post(SMS_NUMBERS_URL, headers=headers, data=payload, timeout=TIMEOUT)
        response.raise_for_status()
        
        content_encoding = response.headers.get('Content-Encoding', '')
        response_text = response.content
        
        if content_encoding == 'br' and brotli:
            try:
                response_text = brotli.decompress(response_text).decode('utf-8')
            except Exception:
                response_text = response_text.decode('utf-8', errors='replace')
        else:
            response_text = response_text.decode('utf-8', errors='replace')
        
        soup = BeautifulSoup(response_text, 'html.parser')
        number_divs = soup.find_all('div', class_='col-sm-4')
        numbers = [div.text.strip() for div in number_divs if div.text.strip()]
        return numbers
    except Exception as e:
        logger.error(f"Error fetching numbers: {str(e)}")
        return []

async def fetch_sms_details(number, range_name, csrf_token):
    try:
        headers = SMS_HEADERS.copy()
        headers["X-CSRF-TOKEN"] = csrf_token
        payload = f"_token={csrf_token}&start=&end=&Number={number}&Range={range_name}"
        response = session.post(SMS_DETAILS_URL, headers=headers, data=payload, timeout=TIMEOUT)
        response.raise_for_status()
        
        content_encoding = response.headers.get('Content-Encoding', '')
        response_text = response.content
        
        if content_encoding == 'br' and brotli:
            try:
                response_text = brotli.decompress(response_text).decode('utf-8')
            except Exception:
                response_text = response_text.decode('utf-8', errors='replace')
        else:
            response_text = response_text.decode('utf-8', errors='replace')
        
        soup = BeautifulSoup(response_text, 'html.parser')
        message_divs = soup.select('div.col-9.col-sm-6 p.mb-0.pb-0')
        messages = [div.text.strip() for div in message_divs] if message_divs else ["No message found"]
        service_div = soup.find('div', class_='col-sm-4')
        service = service_div.text.strip().replace('CLI', '') if service_div else "Unknown"
        
        sms_details = []
        for message in messages:
            service_from_message = extract_service(message)
            if service_from_message != "Unknown":
                service = service_from_message
            sms_details.append({"message": message, "service": service})
        
        return sms_details[0] if sms_details else {"message": "No message found", "service": "Unknown"}
    except Exception as e:
        logger.error(f"Error fetching SMS details: {str(e)}")
        return {"message": "No message found", "service": "Unknown"}

def extract_country(range_name):
    country = range_name.split()[0].capitalize() if range_name and len(range_name.split()) > 1 else "Unknown"
    return country

def extract_service(message):
    for service, pattern in SERVICE_PATTERNS.items():
        if re.search(pattern, message, re.IGNORECASE):
            return service
    return "Unknown"

def extract_otp(text):
    # More robust OTP detection
    patterns = [
        r'\b\d{4,6}\b',  # Standard 4-6 digit codes
        r'\b\d{3}\s\d{3}\b',  # 3+3 digit codes
        r'verification code: (\w+)',
        r'code: (\d+)',
        r'\b[A-Z0-9]{4,8}\b',  # Alphanumeric codes
        r'\d{4,6} is your',  # Common patterns
        r'is:? (\d{4,6})',
        r'use (\d{4,6})',
        r'\b[a-zA-Z0-9]{4,8}\b'  # Alphanumeric codes
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(0)
    
    # If no OTP found, return "N/A"
    return "N/A"

async def send_sms_to_telegram(bot, sms):
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        country_emoji = sms['country_emoji']
        country = sms['country']
        service = sms['service']
        number = sms['number']
        full_message = sms['full_message']
        
        # Format OTP or use "N/A"
        if sms['otp'] == "N/A":
            formatted_otp = "N/A"
        else:
            formatted_otp = format_otp_with_spaces(sms['otp'])

        # Escape HTML special characters
        country = html.escape(country)
        service = html.escape(service)
        formatted_otp = html.escape(formatted_otp)
        number = html.escape(number)
        full_message = html.escape(full_message)

        # Format message with HTML (removed inline buttons)
        message = (
            f"{country_emoji} <b>{country} {service} SMS Received...</b>\n\n"
            f"🔑 <b>OTP :</b> <code>{formatted_otp}</code>\n\n"
            f"🕒 <b>Time :</b> <code>{timestamp}</code>\n"
            f"⚙️ <b>Service :</b> <code>{service}</code>\n"
            f"🌐 <b>Country :</b> <code>{country}</code>\n"
            f"☎️ <b>Phone :</b> <code>{number}</code>\n\n"
            f"<pre>{full_message}</pre>"
        )
        
        # Send to all chat IDs
        for chat_id in CHAT_IDS:
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    parse_mode="HTML"
                )
            except telegram.error.RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
                await bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    parse_mode="HTML"
                )
            except telegram.error.BadRequest as e:
                if "chat not found" in str(e).lower():
                    logger.warning(f"Chat not found: {chat_id}")
                else:
                    logger.error(f"Telegram BadRequest error: {str(e)}")
            except Exception as e:
                logger.error(f"Error sending to {chat_id}: {str(e)}")
    except Exception as e:
        logger.error(f"Error formatting message: {str(e)}")

async def send_start_alert(bot):
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        message = (
            "🤖 <b>Bot Started Successfully</b>\n\n"
            f"🕒 <b>Time:</b> <code>{timestamp}</code>\n"
            "ℹ️ <b>Status:</b> <code>Monitoring for SMS messages</code>"
        )
        
        for chat_id in CHAT_IDS:
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    parse_mode="HTML"
                )
            except telegram.error.RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
                await bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    parse_mode="HTML"
                )
            except telegram.error.BadRequest as e:
                if "chat not found" in str(e).lower():
                    logger.warning(f"Chat not found: {chat_id}")
                else:
                    logger.error(f"Telegram BadRequest error: {str(e)}")
            except Exception as e:
                logger.error(f"Error sending start alert to {chat_id}: {str(e)}")
    except Exception as e:
        logger.error(f"Error sending start alert: {str(e)}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command"""
    try:
        await update.message.reply_text("✅ Bot is running! Monitoring for SMS updates.")
    except Exception as e:
        logger.error(f"Error handling /start command: {str(e)}")

async def main():
    logger.info("Initializing SMS Bot Service...")
    
    # Service control event
    stop_event = asyncio.Event()
    
    # Signal handling for graceful shutdown
    loop = asyncio.get_running_loop()
    def signal_handler():
        logger.info("\nShutting down gracefully...")
        stop_event.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)
    
    try:
        # Initialize bot
        application = Application.builder().token(BOT_TOKEN).build()
        application.add_handler(CommandHandler("start", start))
        
        await application.initialize()
        await application.start()
        await application.updater.start_polling()
        
        logger.info("Sending startup notification...")
        await send_start_alert(application.bot)
        
        # Pre-load flag data
        load_flag_data()
        
        # Main service loop
        last_login_time = time.time()
        if await login():
            logger.info("Login successful, starting monitoring loop...")
            while not stop_event.is_set():
                try:
                    # Refresh session if needed
                    success, last_login_time = await refresh_session(last_login_time)
                    if not success:
                        logger.warning("Session refresh failed, will retry...")
                        await asyncio.sleep(10)
                        continue
                    
                    # Refresh flag data every 24 hours
                    current_time = time.time()
                    if current_time - last_flag_cache_update >= FLAG_CACHE_DURATION:
                        load_flag_data()
                    
                    # Fetch and process SMS
                    sms_list = await fetch_sms()
                    if sms_list:
                        logger.info(f"Processing {len(sms_list)} new SMS messages")
                        
                        # Process in batches
                        for i in range(0, len(sms_list), MAX_BATCH_SIZE):
                            batch = sms_list[i:i+MAX_BATCH_SIZE]
                            for sms in batch:
                                # Skip if no message content
                                if sms['full_message'] == "No message found":
                                    continue
                                
                                # For messages with OTP, check for duplicates
                                if sms['otp'] != "N/A":
                                    if check_and_save_otp(sms['number'], sms['otp'], sms['message_id']):
                                        logger.info(f"Sending SMS with OTP for {sms['number']}: {sms['otp']}")
                                        await send_sms_to_telegram(application.bot, sms)
                                    else:
                                        logger.info(f"Duplicate OTP skipped for {sms['number']}")
                                else:
                                    # For messages without OTP, send directly
                                    logger.info(f"Sending SMS without OTP for {sms['number']}")
                                    await send_sms_to_telegram(application.bot, sms)
                            
                            # Batch delay
                            if i + MAX_BATCH_SIZE < len(sms_list):
                                logger.debug(f"Processed batch, waiting {BATCH_DELAY} seconds...")
                                await asyncio.sleep(BATCH_DELAY)
                    else:
                        logger.debug("No new SMS messages found")
                    
                    await asyncio.sleep(5)
                except Exception as e:
                    logger.error(f"Error in main loop: {str(e)}", exc_info=True)
                    await asyncio.sleep(10)
        else:
            logger.error("Initial login failed - service cannot start")
            return
    except Exception as e:
        logger.critical(f"Fatal error during startup: {str(e)}", exc_info=True)
    finally:
        logger.info("Shutting down service...")
        try:
            await application.updater.stop()
            await application.stop()
            await application.shutdown()
        except Exception as e:
            logger.error(f"Error during shutdown: {str(e)}")
        logger.info("Service stopped")

if __name__ == "__main__":
    try:
        logger.info("Starting SMS Bot Service")
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Service stopped by user")
    except Exception as e:
        logger.critical(f"Service crashed: {str(e)}", exc_info=True)
        sys.exit(1)