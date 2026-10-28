"""Keep tests from writing decision logs to a real user profile or reporting usage to a Router."""
import pytest
import os
import tempfile

_router_data = tempfile.TemporaryDirectory(prefix="ua-herald-tests-")
os.environ["HERALD_DATA_DIR"] = _router_data.name
_user_data = tempfile.TemporaryDirectory(prefix="ua-profile-tests-")
os.environ["LOCALAPPDATA"] = _user_data.name


@pytest.fixture(autouse=True)
def _isolated_decisions(monkeypatch):
    from ultimate_assistant import decision_interface
    monkeypatch.setattr(decision_interface, "REPORT_USAGE", False)
