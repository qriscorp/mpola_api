import os
from dotenv import load_dotenv
from pathlib import Path

base = Path(__file__).resolve().parents[1]

if os.environ.get('PYTEST_RUNNING') == '1':
    example = base / '.env.example'
    if example.exists():
        load_dotenv(dotenv_path=str(example), override=False)
else:
    load_dotenv()

DATABASE_URL = os.getenv('DATABASE_URL')
BASE_URL = os.getenv('BASE_URL')
JWT_SECRET = os.getenv('JWT_SECRET')
EGOSMS_USERNAME = os.getenv('EGOSMS_USERNAME')
EGOSMS_APIKEY = os.getenv('EGOSMS_APIKEY')
SMTP_USERNAME = os.getenv('SMTP_USERNAME')
SMTP_PASSWORD = os.getenv('SMTP_PASSWORD')
SMTP_SERVER = os.getenv('SMTP_SERVER')
SMTP_PORT = int(os.getenv('SMTP_PORT'))
