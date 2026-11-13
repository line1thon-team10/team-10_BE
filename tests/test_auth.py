from unittest.mock import Mock
from uuid import uuid4
import pytest
from app.core.auth import current_user
from app.core.responses import ApiError


@pytest.mark.parametrize("value", [None, "", "bad-id", "x" * 100, "00000000-0000-0000-0000-00000000000z"])
def test_invalid_session_never_reaches_database(value):
    conn = Mock()
    with pytest.raises(ApiError) as error:
        current_user(value, conn)
    assert error.value.status == 401
    conn.execute.assert_not_called()


def test_unknown_valid_uuid():
    conn = Mock()
    conn.execute.return_value.mappings.return_value.first.return_value = None
    with pytest.raises(ApiError) as error:
        current_user(str(uuid4()), conn)
    assert error.value.code == "UNKNOWN_SESSION"
