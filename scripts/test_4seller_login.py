"""Standalone test: set FOURSELLER_EMAIL/FOURSELLER_PASSWORD in nssm-fdcef-environment.env."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from py.utilities.nssm_env import load_nssm_env_file
from py.utilities.fourseller_login import login_from_env, _verify_logged_in

if __name__ == "__main__":
    load_nssm_env_file()
    result = login_from_env()
    session = result["session"]
    print("token:", (result.get("token") or "")[:20], "...")
    print("cookie:", result.get("cookie_header", "")[:80], "...")
    print("dashboard ok:", _verify_logged_in(session))
