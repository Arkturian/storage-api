"""#3969: tests stand in a made-up value for the retired public default master.

The real value never appears in the tree (only its hash, storage/public_default.py).
conftest.py points the hash at this stand-in for every test.
"""
import hashlib

PUBLIC_DEFAULT_TEST = "test-public-default-3969"
PUBLIC_DEFAULT_TEST_SHA256 = hashlib.sha256(PUBLIC_DEFAULT_TEST.encode()).hexdigest()
