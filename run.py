import os
from pathlib import Path
from dotenv import load_dotenv
import uvicorn

if __name__ == '__main__':
    load_dotenv(Path(__file__).parent / '.env')
    uvicorn.run('pm_agent.app:app', host='127.0.0.1', port=int(os.getenv('PM_PORT', '8787')))
