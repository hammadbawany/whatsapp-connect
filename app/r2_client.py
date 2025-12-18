import boto3
import os
from botocore.config import Config

def get_r2_client():
    endpoint = os.environ.get('R2_ENDPOINT')

    # Clean the endpoint (remove trailing slash if present)
    if endpoint and endpoint.endswith('/'):
        endpoint = endpoint[:-1]

    return boto3.client(
        's3',
        endpoint_url=endpoint,
        aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],
        # ⬇️ THIS IS THE CRITICAL FIX ⬇️
        config=Config(
            signature_version='s3v4',
            retries={'max_attempts': 3},
            connect_timeout=5,
            read_timeout=10
        ),
        region_name='auto'
    )
