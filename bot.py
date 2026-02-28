import os
token = os.environ.get("TELEGRAM_BOT_TOKEN")
print(f"TOKEN LENGTH: {len(token) if token else 0}")
print(f"TOKEN EXISTS: {token is not None and len(token) > 5}")
print(f"ALL ENV KEYS: {[k for k in os.environ.keys() if 'TELEGRAM' in k]}")
