"""Things that bite a tool used by many people on many versions: file formats
from a newer jamp, servers that ask for less, and where the cache lives."""
import email.message
import json
import urllib.error

import pytest

import fixtures
from jamp import phase1, restore, userdir
from jamp.config import load_config
from jamp.httpcache import HOST_FAILURE_LIMIT, FetchError, HttpCache
from jamp.state import FORMAT, NEWER, NewerFormat, STATE_NAME, read_state, write_state
from jamp.tagwriter import BACKUP_NAME, TagWriteError, restore_from_backup


# -- file formats ------------------------------------------------------------

def test_state_and_backup_say_their_format(tmp_path):
    from jamp.tagwriter import write_backup

    folder = tmp_path / "show"
    f = fixtures.make_flac(folder / "t1.flac", tags={"TITLE": "x"})
    write_state(folder, {"band": "gd"})
    write_backup(folder, [f])
    state_text = (folder / STATE_NAME).read_text(encoding="utf-8")
    assert json.loads(state_text)["etree_format"] == FORMAT
    assert state_text.lstrip("{ \n").startswith('"etree_format"')        # first, for a quick look
    assert json.loads((folder / BACKUP_NAME).read_text(encoding="utf-8"))["etree_format"] == 1


def test_a_file_without_a_format_is_format_one(tmp_path):
    (tmp_path / STATE_NAME).write_text(json.dumps({"band": "gd"}), encoding="utf-8")
    assert read_state(tmp_path) == {"band": "gd"}


def _newer_state(folder):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / STATE_NAME).write_text(json.dumps(
        {"etree_format": FORMAT + 1, "format": "flac16", "band": "gd", "folder_name": folder.name}), encoding="utf-8")


def test_a_newer_state_file_is_not_read(tmp_path):
    _newer_state(tmp_path)
    assert read_state(tmp_path) == {NEWER: FORMAT + 1}
    with pytest.raises(NewerFormat):
        write_state(tmp_path, {"band": "gd"})
    assert json.loads((tmp_path / STATE_NAME).read_text())["etree_format"] == FORMAT + 1


def test_a_folder_from_a_newer_version_is_blocked(tmp_path):
    root = tmp_path / "lib"
    show = root / "Grateful Dead" / "gd1977-05-08.sbd.flac16"
    fixtures.make_flac(show / "gd1977-05-08d1t01.flac", tags={"ARTIST": "Grateful Dead"})
    _newer_state(show)
    plan = phase1.build_plans(root, load_config(user_path=None))[0]
    assert "WRITTEN_BY_NEWER_VERSION" in {i.code for i in plan.analysis.issues}
    assert plan.analysis.blocked


def test_restore_refuses_a_newer_backup(tmp_path):
    folder = tmp_path / "show"
    fixtures.make_flac(folder / "t1.flac", tags={"TITLE": "x"})
    (folder / BACKUP_NAME).write_text(json.dumps(
        {"etree_format": 99, "files": {"t1.flac": {"__kind__": "vorbis", "fields": {}}}}),
        encoding="utf-8")
    with pytest.raises(TagWriteError, match="newer version"):
        restore_from_backup(folder)
    fr = restore.plan_folder(folder, tmp_path)
    assert any("newer version" in p for p in fr.problems)


# -- servers -----------------------------------------------------------------

def _error(code, retry_after=None):
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError("https://example.org/x", code, "no", headers, None)


def test_too_many_requests_waits_as_asked_then_succeeds(tmp_path):
    answers = [_error(429, retry_after=7), (200, b"ok")]
    slept = []

    def net(url, timeout):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    cache = HttpCache(tmp_path / "c.sqlite", opener=net, sleep=slept.append)
    assert cache.get("https://example.org/x").body == b"ok"
    assert 7.0 in slept


def test_a_server_still_failing_after_retries_is_an_error(tmp_path):
    calls = []

    def net(url, timeout):
        calls.append(url)
        raise _error(503)

    cache = HttpCache(tmp_path / "c.sqlite", opener=net, sleep=lambda s: None)
    with pytest.raises(FetchError):
        cache.get("https://example.org/x")
    assert len(calls) == 3                          # the first try and two retries


def test_a_site_that_keeps_failing_is_left_alone_for_the_run(tmp_path):
    calls = []

    def net(url, timeout):
        calls.append(url)
        raise _error(403)                           # not worth retrying

    cache = HttpCache(tmp_path / "c.sqlite", opener=net, sleep=lambda s: None)
    for n in range(HOST_FAILURE_LIMIT):
        with pytest.raises(FetchError):
            cache.get("https://example.org/%d" % n)
    with pytest.raises(FetchError, match="not asked"):
        cache.get("https://example.org/again")
    assert len(calls) == HOST_FAILURE_LIMIT
    assert cache.stats()["sites_given_up"] == ["example.org"]
    # Another site is still asked.
    with pytest.raises(FetchError):
        cache.get("https://other.org/x")
    assert calls[-1] == "https://other.org/x"


def test_a_404_is_an_answer_and_not_retried(tmp_path):
    calls = []

    def net(url, timeout):
        calls.append(url)
        raise _error(404)

    cache = HttpCache(tmp_path / "c.sqlite", opener=net, sleep=lambda s: None)
    assert cache.get("https://example.org/x").status == 404
    assert len(calls) == 1


def test_the_user_agent_names_the_version_and_the_project():
    from jamp import __version__
    from jamp.httpcache import USER_AGENT

    assert __version__ in USER_AGENT and "github.com" in USER_AGENT


# -- the cache ---------------------------------------------------------------

def test_the_cache_lives_in_the_user_cache_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("JAMP_HOME", str(tmp_path / "home"))
    path, why = userdir.cache_path(None, tmp_path / "reports")
    assert path == tmp_path / "home" / "cache" / "archive.sqlite"


def test_a_cache_already_in_the_reports_folder_is_still_used(tmp_path):
    legacy = tmp_path / "reports" / "cache" / "archive.sqlite"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"")
    assert userdir.cache_path(None, tmp_path / "reports")[0] == legacy
    assert userdir.cache_path(tmp_path / "mine.sqlite", tmp_path / "reports")[0] == \
        tmp_path / "mine.sqlite"


def test_the_platform_cache_folder_is_not_the_config_folder(monkeypatch):
    monkeypatch.delenv("JAMP_HOME", raising=False)
    assert userdir.user_cache_dir() != userdir.user_dir()


def test_the_audio_format_field_is_not_a_version_and_survives_a_write(tmp_path):
    """The state file has always stored the audio format as "format"."""
    (tmp_path / STATE_NAME).write_text(json.dumps(
        {"format": "flac24", "band": "dtb", "folder_name": tmp_path.name}), encoding="utf-8")
    assert read_state(tmp_path)["format"] == "flac24"
    write_state(tmp_path, {"band": "dtb", "format": "flac24"})
    data = json.loads((tmp_path / STATE_NAME).read_text(encoding="utf-8"))
    assert data["format"] == "flac24" and data["etree_format"] == FORMAT
