import requests
from bs4 import BeautifulSoup
import logging
import re
from flask import current_app

def get_logger():
    """Get Flask logger if available, otherwise use module logger"""
    try:
        return current_app.logger
    except:
        logger = logging.getLogger('container_lookup_service')
        logger.setLevel(logging.INFO)
        return logger

def clean_vessel_name(vessel_name):
    """Clean and normalize vessel name to single line"""
    if not vessel_name:
        return None
    
    # Convert to string and strip
    vessel_name = str(vessel_name).strip()
    
    # Remove multiple spaces, newlines, tabs
    vessel_name = re.sub(r'\s+', ' ', vessel_name)
    
    # Remove common unwanted text
    unwanted_patterns = [
        r'vessel\s*:?\s*',
        r'name\s*:?\s*',
        r'departed\s+by\s+',
        r'<[^>]+>',  # Remove any HTML tags
    ]
    
    for pattern in unwanted_patterns:
        vessel_name = re.sub(pattern, '', vessel_name, flags=re.IGNORECASE)
    
    # Final strip and uppercase
    vessel_name = vessel_name.strip().upper()
    
    # Validate length (vessel names are typically 3-50 characters)
    if len(vessel_name) < 3 or len(vessel_name) > 100:
        return None
    
    return vessel_name

def lookup_container_vessel(container_number):
    """
    Lookup vessel information for a container number using multiple services
    Returns: dict with vessel_name, voyage, eta, status, etc.
    """
    logger = get_logger()
    
    # Clean container number
    container_number = container_number.strip().upper()
    logger.info(f"Starting vessel lookup for container: {container_number}")
    
    # Try multiple services
    result = None
    
    # Method 1: SeaRates API (they have container tracking)
    logger.info(f"Trying SeaRates lookup for {container_number}...")
    result = _lookup_searates(container_number)
    if result:
        logger.info(f"SeaRates found vessel: {result.get('vessel_name')}")
        return result
    
    # Method 2: Container Tracking API (containerstrack.com)
    logger.info(f"Trying ContainersTrack lookup for {container_number}...")
    result = _lookup_containerstrack(container_number)
    if result:
        logger.info(f"ContainersTrack found vessel: {result.get('vessel_name')}")
        return result
    
    # Method 3: Track-Trace API
    logger.info(f"Trying Track-Trace lookup for {container_number}...")
    result = _lookup_tracktrace(container_number)
    if result:
        logger.info(f"Track-Trace found vessel: {result.get('vessel_name')}")
        return result
    
    # Method 4: FindTEU
    logger.info(f"Trying FindTEU lookup for {container_number}...")
    result = _lookup_findteu(container_number)
    if result:
        logger.info(f"FindTEU found vessel: {result.get('vessel_name')}")
        return result
    
    logger.warning(f"No vessel found for container {container_number} from any service")
    return None

def _lookup_searates(container_number):
    """Lookup using SeaRates tracking"""
    logger = get_logger()
    try:
        url = f"https://www.searates.com/container/tracking/?number={container_number}"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Try multiple strategies to find vessel name
            vessel_name = None
            
            # Strategy 1: div with vessel-name class
            vessel_elem = soup.find('div', class_='vessel-name')
            if vessel_elem:
                vessel_name = vessel_elem.get_text().strip()
            
            # Strategy 2: Text containing "Vessel:"
            if not vessel_name:
                vessel_text = soup.find(text=re.compile('Vessel:', re.I))
                if vessel_text:
                    parent = vessel_text.parent
                    if parent:
                        # Get next element after "Vessel:"
                        next_elem = parent.find_next(['span', 'div', 'td', 'strong'])
                        if next_elem:
                            vessel_name = next_elem.get_text().strip()
            
            # Strategy 3: Look for table row with Vessel
            if not vessel_name:
                rows = soup.find_all(['tr', 'div'], class_=re.compile('row|item'))
                for row in rows:
                    if 'vessel' in row.get_text().lower():
                        # Try to extract the value
                        values = row.find_all(['td', 'span', 'div'])
                        for val in values:
                            text = val.get_text().strip()
                            if text and 'vessel' not in text.lower() and len(text) > 3:
                                vessel_name = text
                                break
                        if vessel_name:
                            break
            
            # Clean the vessel name
            if vessel_name:
                vessel_name = clean_vessel_name(vessel_name)
            
            if vessel_name:
                logger.info(f"Found vessel for {container_number}: {vessel_name}")
                return {
                    'vessel_name': vessel_name,
                    'source': 'SeaRates',
                    'container': container_number
                }
            else:
                logger.debug(f"SeaRates: No vessel name found for {container_number}")
    except Exception as e:
        logger.debug(f"SeaRates lookup failed: {e}")
    
    return None

def _lookup_containerstrack(container_number):
    """Lookup using containerstrack.com API"""
    logger = get_logger()
    try:
        # This service provides container tracking data
        url = f"https://www.containerstrack.com/track/{container_number}"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, 'html.parser')
            
            vessel_name = None
            
            # Strategy 1: Look for vessel in table cell
            vessel_info = soup.find('td', text=re.compile('Vessel', re.I))
            if vessel_info and vessel_info.find_next_sibling('td'):
                vessel_name = vessel_info.find_next_sibling('td').get_text().strip()
            
            # Strategy 2: Look for th/td structure
            if not vessel_name:
                vessel_th = soup.find('th', text=re.compile('Vessel', re.I))
                if vessel_th:
                    vessel_td = vessel_th.find_next_sibling('td')
                    if vessel_td:
                        vessel_name = vessel_td.get_text().strip()
            
            # Strategy 3: Look in any element with vessel data
            if not vessel_name:
                all_text = soup.find_all(text=re.compile('Vessel', re.I))
                for text in all_text:
                    parent = text.parent
                    if parent:
                        next_elem = parent.find_next(['td', 'div', 'span'])
                        if next_elem:
                            potential_name = next_elem.get_text().strip()
                            if potential_name and len(potential_name) > 3:
                                vessel_name = potential_name
                                break
            
            # Clean the vessel name
            if vessel_name:
                vessel_name = clean_vessel_name(vessel_name)
            
            if vessel_name:
                logger.info(f"Found vessel for {container_number}: {vessel_name}")
                return {
                    'vessel_name': vessel_name,
                    'source': 'ContainersTrack',
                    'container': container_number
                }
            else:
                logger.debug(f"ContainersTrack: No vessel name found for {container_number}")
    except Exception as e:
        logger.debug(f"ContainersTrack lookup failed: {e}")
    
    return None

def _lookup_tracktrace(container_number):
    """Lookup using track-trace.com"""
    logger = get_logger()
    try:
        url = f"https://www.track-trace.com/container"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        # This might require POST request with container number
        data = {'container': container_number}
        response = requests.post(url, data=data, headers=headers, timeout=10)
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Try multiple strategies to find vessel name
            vessel_name = None
            
            # Strategy 1: Find text "Vessel" and get next sibling
            vessel_elem = soup.find(text=re.compile('Vessel', re.I))
            if vessel_elem:
                # Try parent's next sibling
                parent = vessel_elem.parent
                if parent and parent.next_sibling:
                    vessel_name = parent.next_sibling.get_text().strip()
                # Try find_next on parent
                if not vessel_name and parent:
                    next_elem = parent.find_next(['td', 'div', 'span'])
                    if next_elem:
                        vessel_name = next_elem.get_text().strip()
            
            # Strategy 2: Look for specific table structure
            if not vessel_name:
                vessel_row = soup.find('th', text=re.compile('Vessel', re.I))
                if vessel_row:
                    vessel_td = vessel_row.find_next_sibling('td')
                    if vessel_td:
                        vessel_name = vessel_td.get_text().strip()
            
            # Clean the vessel name
            if vessel_name:
                vessel_name = clean_vessel_name(vessel_name)
            
            if vessel_name:
                logger.info(f"Found vessel for {container_number}: {vessel_name}")
                return {
                    'vessel_name': vessel_name,
                    'source': 'Track-Trace',
                    'container': container_number
                }
            else:
                logger.debug(f"Track-Trace found Vessel field but name was empty for {container_number}")
    except Exception as e:
        logger.debug(f"Track-Trace lookup failed: {e}")
    
    return None

def _lookup_findteu(container_number):
    """Lookup using findteu.com with Selenium for JavaScript rendering"""
    logger = get_logger()
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from webdriver_manager.chrome import ChromeDriverManager
        
        url = f"https://www.findteu.com/track/{container_number}"
        
        # Setup Chrome options for headless mode
        chrome_options = Options()
        chrome_options.add_argument('--headless')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--window-size=1920,1080')
        chrome_options.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
        
        logger.debug(f"Starting Selenium for FindTEU: {url}")
        
        # Initialize driver
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=chrome_options)
        
        try:
            driver.get(url)
            
            # Wait for the vessel name to appear (max 15 seconds)
            # Look for <small> tag containing <i> with vessel name
            wait = WebDriverWait(driver, 15)
            vessel_element = wait.until(
                EC.presence_of_element_located((By.XPATH, "//small[contains(text(), 'Vessel') or contains(text(), 'vessel')]//i"))
            )
            
            vessel_name = vessel_element.text.strip()
            logger.debug(f"Selenium extracted vessel name: {vessel_name}")
            
            driver.quit()
            
            # Clean the vessel name
            if vessel_name:
                vessel_name = clean_vessel_name(vessel_name)
            
            if vessel_name:
                logger.info(f"Found vessel for {container_number}: {vessel_name}")
                return {
                    'vessel_name': vessel_name,
                    'source': 'FindTEU',
                    'container': container_number
                }
                
        except Exception as e:
            logger.debug(f"Selenium wait/extraction failed: {e}")
            driver.quit()
            
    except ImportError:
        logger.warning("Selenium not available for FindTEU lookup")
    except Exception as e:
        logger.debug(f"FindTEU Selenium lookup failed: {e}")
    
    # Fallback to basic requests (likely won't work for JS sites)
    try:
        url = f"https://www.findteu.com/track/{container_number}"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, 'html.parser')
            
            vessel_name = None
            
            # Look for <i> tag inside small with "Departed by Vessel" or "Vessel"
            small_tags = soup.find_all('small')
            
            for small in small_tags:
                small_text = small.get_text().lower()
                if 'vessel' in small_text:
                    i_tag = small.find('i')
                    if i_tag:
                        vessel_name = i_tag.get_text().strip()
                        break
            
            # Strategy 2: Look for vessel name in table structure
            if not vessel_name:
                vessel_label = soup.find(text=re.compile('Vessel Name', re.I))
                if vessel_label:
                    parent = vessel_label.parent
                    if parent:
                        # Try next sibling
                        next_elem = parent.find_next_sibling(['td', 'div', 'span'])
                        if next_elem:
                            vessel_name = next_elem.get_text().strip()
                        # Try find_next
                        if not vessel_name:
                            next_elem = parent.find_next(['td', 'div', 'span', 'strong', 'i'])
                            if next_elem:
                                vessel_name = next_elem.get_text().strip()
            
            # Strategy 3: Look for th/td structure
            if not vessel_name:
                vessel_th = soup.find(['th', 'dt'], text=re.compile('Vessel', re.I))
                if vessel_th:
                    vessel_td = vessel_th.find_next_sibling(['td', 'dd'])
                    if vessel_td:
                        vessel_name = vessel_td.get_text().strip()
            
            # Strategy 4: Look in divs/spans with vessel class
            if not vessel_name:
                vessel_elem = soup.find(['div', 'span'], class_=re.compile('vessel', re.I))
                if vessel_elem:
                    vessel_name = vessel_elem.get_text().strip()
            
            # Strategy 5: Look for any <i> or <strong> tag near "Vessel" text
            if not vessel_name:
                all_vessel_text = soup.find_all(text=re.compile('Vessel', re.I))
                for text in all_vessel_text:
                    parent = text.parent
                    if parent:
                        # Look for <i> tag in same parent
                        i_tag = parent.find('i')
                        if i_tag:
                            vessel_name = i_tag.get_text().strip()
                            break
                        # Look for next elements
                        next_elem = parent.find_next(['i', 'strong', 'td', 'div', 'span'])
                        if next_elem:
                            potential_name = next_elem.get_text().strip()
                            # Filter out common non-vessel text
                            if (potential_name and 
                                len(potential_name) > 3 and 
                                'vessel' not in potential_name.lower() and
                                'name' not in potential_name.lower() and
                                'departed' not in potential_name.lower()):
                                vessel_name = potential_name
                                break
            
            # Clean the vessel name
            if vessel_name:
                vessel_name = clean_vessel_name(vessel_name)
            
            if vessel_name:
                logger.info(f"Found vessel for {container_number}: {vessel_name}")
                return {
                    'vessel_name': vessel_name,
                    'source': 'FindTEU',
                    'container': container_number
                }
            else:
                logger.debug(f"FindTEU: No vessel name found for {container_number}")
    except Exception as e:
        logger.debug(f"FindTEU lookup failed: {e}")
    
    return None

def lookup_vessel_mmsi(vessel_name):
    """
    Lookup MMSI for a vessel name using Marine Traffic or similar
    """
    logger = get_logger()
    try:
        # Use Marine Traffic vessel database search
        url = "https://www.marinetraffic.com/en/data/?asset_type=vessels"
        params = {'term': vessel_name}
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        response = requests.get(url, params=params, headers=headers, timeout=10)
        if response.status_code == 200:
            # Parse JSON response
            data = response.json()
            if data and len(data) > 0:
                vessel = data[0]
                return {
                    'vessel_name': vessel.get('SHIPNAME'),
                    'mmsi': vessel.get('MMSI'),
                    'imo': vessel.get('IMO'),
                    'type': vessel.get('SHIPTYPE'),
                    'flag': vessel.get('FLAG')
                }
    except Exception as e:
        logger.debug(f"Marine Traffic lookup failed: {e}")
    
    return None

def get_vessel_info_complete(container_number):
    """
    Complete lookup: container -> vessel name -> MMSI/IMO -> AIS data
    """
    logger = get_logger()
    result = {
        'container': container_number,
        'vessel_name': None,
        'mmsi': None,
        'imo': None,
        'source': None,
        'error': None
    }
    
    try:
        logger.info(f"Starting complete vessel info lookup for {container_number}")
        
        # Step 1: Get vessel name from container
        container_info = lookup_container_vessel(container_number)
        if not container_info:
            result['error'] = 'Container not found in tracking systems'
            logger.warning(f"Container {container_number} not found in any tracking system")
            return result
        
        result['vessel_name'] = container_info.get('vessel_name')
        result['source'] = container_info.get('source')
        logger.info(f"Found vessel: {result['vessel_name']} from {result['source']}")
        
        # Step 2: Get MMSI from vessel name
        if result['vessel_name']:
            logger.info(f"Looking up MMSI for vessel: {result['vessel_name']}")
            vessel_details = lookup_vessel_mmsi(result['vessel_name'])
            if vessel_details:
                result['mmsi'] = vessel_details.get('mmsi')
                result['imo'] = vessel_details.get('imo')
                logger.info(f"Found MMSI: {result['mmsi']}, IMO: {result['imo']}")
        
        logger.info(f"Complete lookup finished for {container_number}: {result}")
        return result
        
    except Exception as e:
        result['error'] = str(e)
        logger.error(f"Error in complete vessel lookup for {container_number}: {e}")
        logger.exception("Full traceback:")
        return result
