import boto3
import os

def get_r2_client():
    # Fix: Use the R2_ENDPOINT variable you already have
    endpoint = os.environ.get('R2_ENDPOINT')

    # robust check to ensure https:// is present
    if endpoint and not endpoint.startswith("http"):
        endpoint = f"https://{endpoint}"

    return boto3.client(
        's3',
        endpoint_url=endpoint,
        aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],
        region_name="auto"
    )
