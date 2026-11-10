"""Use the unchanged AI image with Cloud's reversible maintenance adapter."""
import os
import uvicorn
from runpod_control import install

control = install(int(os.environ['JOBS_STORY_CONCURRENCY']))
from app.v2_main import create_app

if __name__ == '__main__':
    uvicorn.run(control.middleware(create_app()), host='0.0.0.0', port=8000, timeout_graceful_shutdown=35)
