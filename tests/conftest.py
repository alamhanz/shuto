import os

# handlers.py creates boto3 clients at import time; give them a region.
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-northeast-1")
