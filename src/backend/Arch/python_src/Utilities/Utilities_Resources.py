"""Compatibility exports for legacy callers; never store provider secrets here."""
import os

# New clients read the environment at construction time. This alias remains for
# older notebooks; importing this module does not require a funded/data account.
apiKey = os.environ.get("POLYGON_API_KEY", "")
payload = {}
headers = {"Accept": "application/json"}
