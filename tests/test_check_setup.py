from app.config import Settings
from scripts.check_setup import FAIL, OK, WARN, check_config

GOOD_ID = "U" + "a" * 32


def full(**kw):
    base = dict(line_channel_secret="s", line_channel_access_token="t",
                google_sa_info={"type": "service_account"}, google_drive_token_info={"refresh_token": "r"},
                sheet_id="sid", drive_folder_id="fid", allowed_user_ids=frozenset({GOOD_ID}))
    base.update(kw)
    return Settings(**base)


def test_complete_config_passes():
    assert [r.status for r in check_config(full())] == [OK]


def test_reports_missing_and_malformed_settings():
    results = {r.name: r for r in check_config(full(sheet_id="", allowed_user_ids=frozenset({"bob"}),
                                                     google_drive_token_info={}))}
    assert results["Required settings"].status == FAIL and "SHEET_ID" in results["Required settings"].detail
    assert results["ALLOWED_USER_IDS"].status == FAIL
    assert results["GOOGLE_DRIVE_TOKEN_JSON"].status == FAIL


def test_missing_drive_token_is_a_warning():
    results = {r.name: r for r in check_config(full(google_drive_token_info=None))}
    assert results["GOOGLE_DRIVE_TOKEN_JSON"].status == WARN
