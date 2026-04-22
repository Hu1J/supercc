"""Tests for cron_scheduler.py."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from supercc.cron_scheduler import (
    parse_schedule,
    create_job,
    get_job,
    list_jobs,
    update_job,
    delete_job,
    mark_run,
    get_due_jobs,
    compute_next_run,
    _CronStore,
)


HAS_CRONITER = True
try:
    import croniter
except ImportError:
    HAS_CRONITER = False


class TestParseSchedule:
    def test_interval_every(self):
        r = parse_schedule("every 30m")
        assert r["kind"] == "interval"
        assert r["minutes"] == 30
        assert r["display"] == "every 30m"

    def test_interval_hours(self):
        r = parse_schedule("every 2h")
        assert r["kind"] == "interval"
        assert r["minutes"] == 120

    def test_once_duration_minutes(self):
        r = parse_schedule("30m")
        assert r["kind"] == "once"
        assert r["display"] == "once in 30m"

    def test_once_duration_hours(self):
        r = parse_schedule("2h")
        assert r["kind"] == "once"
        assert "run_at" in r

    @pytest.mark.skipif(not HAS_CRONITER, reason="croniter not installed")
    def test_cron_expression(self):
        r = parse_schedule("0 9 * * *")
        assert r["kind"] == "cron"
        assert r["expr"] == "0 9 * * *"

    def test_invalid(self):
        with pytest.raises(ValueError):
            parse_schedule("foobar")


class TestCronStore:
    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _CronStore(tmpdir)
            jobs = [{"id": "test1", "name": "test"}]
            store.save_jobs(jobs)
            loaded = store.load_jobs()
            assert len(loaded) == 1
            assert loaded[0]["id"] == "test1"

    def test_output_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _CronStore(tmpdir)
            path = store.output_path("abc", "2026-04-20_10-00-00")
            assert path.parent.name == "abc"
            assert path.name == "2026-04-20_10-00-00.md"


class TestCreateJob:
    def test_create_interval_job(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            job = create_job(
                prompt="分析日志",
                schedule="every 1h",
                chat_id="oc_123",
                name="日志分析",
                data_dir=tmpdir,
            )
            assert job["id"]
            assert job["name"] == "日志分析"
            assert job["schedule"]["kind"] == "interval"
            assert job["chat_id"] == "oc_123"
            assert job["enabled"] is True
            assert job["state"] == "scheduled"

    def test_create_once_job_repeat_1(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            job = create_job(
                prompt="提醒",
                schedule="30m",
                chat_id="oc_123",
                data_dir=tmpdir,
            )
            assert job["repeat"]["times"] == 1
            assert job["repeat"]["completed"] == 0

    @pytest.mark.skipif(not HAS_CRONITER, reason="croniter not installed")
    def test_create_cron_job(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            job = create_job(
                prompt="日报",
                schedule="0 9 * * *",
                chat_id="oc_456",
                name="每日日报",
                repeat=7,
                data_dir=tmpdir,
            )
            assert job["schedule"]["kind"] == "cron"
            assert job["repeat"]["times"] == 7


class TestCrudOperations:
    def test_get_job(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            created = create_job("test", "every 1h", "oc_123", data_dir=tmpdir)
            fetched = get_job(created["id"], tmpdir)
            assert fetched is not None
            assert fetched["id"] == created["id"]

    def test_list_jobs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            create_job("job1", "every 1h", "oc_123", data_dir=tmpdir)
            create_job("job2", "every 2h", "oc_123", data_dir=tmpdir)
            jobs = list_jobs(tmpdir)
            assert len(jobs) == 2

    def test_update_job(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            job = create_job("test", "every 1h", "oc_123", data_dir=tmpdir)
            updated = update_job(job["id"], {"name": "新名字"}, tmpdir)
            assert updated is not None
            assert updated["name"] == "新名字"

    def test_delete_job(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            job = create_job("test", "every 1h", "oc_123", data_dir=tmpdir)
            ok = delete_job(job["id"], tmpdir)
            assert ok is True
            assert get_job(job["id"], tmpdir) is None

    def test_pause_resume(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            job = create_job("test", "every 1h", "oc_123", data_dir=tmpdir)
            paused = update_job(job["id"], {"enabled": False, "state": "paused"}, tmpdir)
            assert paused["enabled"] is False
            assert paused["state"] == "paused"
            resumed = update_job(job["id"], {"enabled": True, "state": "scheduled"}, tmpdir)
            assert resumed["enabled"] is True


class TestDueJobs:
    def test_due_jobs_only_returns_due(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            job = create_job("test", "every 1h", "oc_123", data_dir=tmpdir)
            # Manually set next_run_at to the past to make it due
            update_job(job["id"], {"next_run_at": "2020-01-01T00:00:00+00:00"}, tmpdir)
            due = get_due_jobs(tmpdir)
            assert len(due) == 1
            assert due[0]["id"] == job["id"]

    def test_disabled_job_not_due(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            job = create_job("test", "every 1h", "oc_123", data_dir=tmpdir)
            update_job(job["id"], {"enabled": False, "next_run_at": "2020-01-01T00:00:00+00:00"}, tmpdir)
            due = get_due_jobs(tmpdir)
            assert len(due) == 0


class TestMarkRun:
    def test_mark_run_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            job = create_job("test", "every 1h", "oc_123", data_dir=tmpdir)
            mark_run(job["id"], success=True, data_dir=tmpdir)
            updated = get_job(job["id"], tmpdir)
            assert updated["last_status"] == "ok"
            assert updated["last_run_at"] is not None

    def test_mark_run_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            job = create_job("test", "every 1h", "oc_123", data_dir=tmpdir)
            mark_run(job["id"], success=False, error="oops", data_dir=tmpdir)
            updated = get_job(job["id"], tmpdir)
            assert updated["last_status"] == "error"
            assert updated["last_error"] == "oops"

    def test_oneshot_completes_after_repeat(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            job = create_job("test", "30m", "oc_123", repeat=1, data_dir=tmpdir)
            mark_run(job["id"], success=True, data_dir=tmpdir)
            # Job should be auto-deleted (repeat exhausted)
            assert get_job(job["id"], tmpdir) is None
