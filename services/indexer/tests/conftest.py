from io import BytesIO
import os
from pathlib import Path

import boto3
import psycopg2
import pytest
from pgvector.psycopg2 import register_vector
from reportlab.pdfgen import canvas


@pytest.fixture(scope="session")
def s3_client():
    endpoint = os.getenv("TEST_S3_ENDPOINT", "http://minio:9000")
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id="minioadmin",
        aws_secret_access_key="minioadmin",
        region_name="us-east-1",
    )


@pytest.fixture(scope="session")
def test_bucket(s3_client):
    bucket = "vault-test"
    try:
        s3_client.create_bucket(Bucket=bucket)
    except s3_client.exceptions.BucketAlreadyOwnedByYou:
        pass
    except s3_client.exceptions.BucketAlreadyExists:
        pass
    return bucket


@pytest.fixture(scope="session")
def db_conn():
    host = os.getenv("TEST_DB_HOST", "postgres")
    conn = psycopg2.connect(host=host, port=5432, dbname="vault", user="vault", password="vault")
    schema_path = Path(__file__).resolve().parents[1] / "app" / "db" / "schema.sql"
    with conn.cursor() as cur:
        cur.execute(schema_path.read_text(encoding="utf-8"))
    conn.commit()
    register_vector(conn)
    yield conn
    conn.close()


@pytest.fixture
def sample_pdf(tmp_path):
    path = tmp_path / "sample.pdf"
    buffer = BytesIO()
    c = canvas.Canvas(buffer)
    c.drawString(100, 750, "Hello PDF")
    c.save()
    path.write_bytes(buffer.getvalue())
    return path
