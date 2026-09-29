# Fake settings so the apps import and run without GCP credentials.
import os

for key, value in {
    "GCP_PROJECT_ID": "test-project",
    "WEBHOOK_SECRET_TOKEN": "test-secret",
    "WEBHOOK_PATH": "test-path",
    "TELEGRAM_BOT_TOKEN": "123:test",
    "DEEPSEEK_API_KEY": "test-key",
}.items():
    os.environ.setdefault(key, value)
