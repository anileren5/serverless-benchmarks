EVENT_HEADERS = [
    "event_id", "transaction_id", "event_time", "transaction_time", "product_id",
    "store_id", "quantity", "unit_price_cents", "discount_bps", "status", "currency",
]
PRODUCT_HEADERS = ["product_id", "product_name", "category"]
STORE_HEADERS = ["store_id", "store_name", "region"]
OUTPUT_HEADERS = EVENT_HEADERS + [
    "product_name", "category", "store_name", "region", "transaction_date",
    "gross_cents", "discount_cents", "total_cents",
]
INTEGER_COLUMNS = {
    "quantity", "unit_price_cents", "discount_bps", "gross_cents", "discount_cents", "total_cents",
}
OUTPUT_TYPES = [
    "BIGINT" if name in INTEGER_COLUMNS
    else "TIMESTAMP" if name in ("event_time", "transaction_time")
    else "DATE" if name == "transaction_date"
    else "VARCHAR"
    for name in OUTPUT_HEADERS
]

ASCII_WHITESPACE = " \t\r\n"
ASCII_LOWERCASE = "abcdefghijklmnopqrstuvwxyz"
ASCII_UPPERCASE = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
STATUS_PENDING = "PENDING"
STATUS_COMPLETED = "COMPLETED"
STATUS_CANCELLED = "CANCELLED"
STATUSES = (STATUS_PENDING, STATUS_COMPLETED, STATUS_CANCELLED)
CURRENCY = "USD"
REGIONS = ("north", "south", "east", "west")
CATEGORIES = ("home", "lighting", "apparel", "grocery")
QUANTITY_MIN, QUANTITY_MAX = 1, 1000
PRICE_CENTS_MIN, PRICE_CENTS_MAX = 0, 10_000_000
BASIS_POINTS = 10_000
DISCOUNT_BPS_MIN, DISCOUNT_BPS_MAX = 0, BASIS_POINTS
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
TIMESTAMP_PATTERN = r"20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
EVENT_ID_PATTERN = r"E[0-9]{12}"
TRANSACTION_ID_PATTERN = r"[A-Z0-9_-]{1,32}"
PRODUCT_ID_PATTERN = r"P[0-9]{8}"
STORE_ID_PATTERN = r"S[0-9]{8}"

TRANSACTION_DATE = "2025-01-01"
DEFAULT_DUPLICATE_BPS = 2000
INVALID_RECORD_EVERY = 100
PARTITION_COLUMNS = ("transaction_date", "region")
PARQUET_COMPRESSION = "ZSTD"

DEFAULT_MEMORY_MB = 1024
LAMBDA_MIN_MEMORY_MB = 128
LAMBDA_MAX_MEMORY_MB = 10_240
LAMBDA_MB_PER_VCPU = 1769
LAMBDA_MAX_THREADS = 6
DUCKDB_MEMORY_PERCENT = 60
BYTES_PER_MIB = 1024 * 1024
