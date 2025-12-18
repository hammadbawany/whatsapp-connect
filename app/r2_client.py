import boto3
import os
from botocore.config import Config

def get_r2_client():
    # 1. Get endpoint
    endpoint = os.environ.get('R2_ENDPOINT')
    if endpoint and endpoint.endswith('/'):
        endpoint = endpoint[:-1]

    # 2. Configure Client
    return boto3.client(
        's3',
        endpoint_url=endpoint,
        aws_access_key_id=os.environ.get('R2_ACCESS_KEY_ID'),
        aws_secret_access_key=os.environ.get('R2_SECRET_ACCESS_KEY'),
        config=Config(
            signature_version='s3v4',
            retries={'max_attempts': 3}
        ),
        region_name='auto',

        # 🚨 FORCE BYPASS SSL ERRORS 🚨
        verify=False
    )
