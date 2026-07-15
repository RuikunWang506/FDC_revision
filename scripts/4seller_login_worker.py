import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from py.utilities.nssm_env import load_nssm_env_file

load_nssm_env_file()

from py.utilities.fourseller_login import login_4seller

email = os.environ.get("FOURSELLER_EMAIL", "").strip()
password = os.environ.get("FOURSELLER_PASSWORD", "").strip()
if not email or not password:
    print(json.dumps({"success": False, "error": "missing credentials"}))
    sys.exit(1)

try:
    result = login_4seller(email, password)
    print(
        json.dumps(
            {
                "success": True,
                "cookie_header": result.get("cookie_header", ""),
                "token": result.get("token"),
            }
        )
    )
except Exception as exc:
    print(json.dumps({"success": False, "error": str(exc)}))
    sys.exit(1)
