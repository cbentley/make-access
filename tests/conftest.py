import logging

import pytest

import make_access as ci


@pytest.fixture(autouse=True)
def reset_logger():
    """main() adds a file and a console handler on every call; remove them after each test."""
    yield
    for handler in ci.logger.handlers[:]:
        handler.close()
        ci.logger.removeHandler(handler)
    ci.logger.setLevel(logging.NOTSET)
