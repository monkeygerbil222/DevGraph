"""The agent's shutdown closes the graph engine only once no caller is inside a
query, and in bounded time even when one never returns.

Every engine user is covered by the engine's own fence (see
tests/graph/test_engine_close.py); these pin the races the agent's callers
used to lose: an insights computation, a dashboard request and the health
check, each in flight at stop."""

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from devgraph.agent.shutdown import shutdown
from devgraph.analytics import insights
from devgraph.analytics.insights import InsightsScheduler
from devgraph.graph.engine import EngineClosed
from tests.graph.test_engine_close import fake_engine, run_in_thread


class Idle:
    """A scheduler or watcher with nothing running."""

    def __init__(self):
        self.stopped = False

    def stop(self, timeout=None):
        self.stopped = True


class Registry:
    def list_repos(self, active_only=False):
        return [SimpleNamespace(repo_id="r", last_indexed="2026-10-01T00:00:00+00:00")]


def stop_agent(engine, *, insights_scheduler=None, dashboard_server=None, dashboard_thread=None, wait_s=3.0):
    shutdown(
        engine, Idle(), Idle(), insights_scheduler or Idle(),
        dashboard_server=dashboard_server, dashboard_thread=dashboard_thread, wait_s=wait_s,
    )


def blocking_compute(monkeypatch):
    computing, release = threading.Event(), threading.Event()

    def compute(nodes, edges):
        computing.set()
        release.wait(10)
        return SimpleNamespace(communities=[], node_rows=[], node_count=0, modularity=0.0)

    monkeypatch.setattr(insights, "compute_insights", compute)
    return computing, release


def test_an_insights_computation_running_at_stop_writes_before_the_close(monkeypatch):
    engine, driver = fake_engine(monkeypatch)
    computing, release = blocking_compute(monkeypatch)
    scheduler = InsightsScheduler(engine, Registry(), interval_s=60)
    scheduler.start()
    assert computing.wait(5)
    threading.Timer(0.3, release.set).start()
    stop_agent(engine, insights_scheduler=scheduler)
    assert driver.events[-2:] == ["write", "close"]
    assert not driver.used_after_close


def test_an_insights_computation_outlasting_stop_is_refused_quietly(monkeypatch, caplog):
    engine, driver = fake_engine(monkeypatch)
    computing, release = blocking_compute(monkeypatch)
    scheduler = InsightsScheduler(engine, Registry(), interval_s=60)
    scheduler.start()
    assert computing.wait(5)
    thread = scheduler._thread
    with caplog.at_level("DEBUG"):
        stop_agent(engine, insights_scheduler=scheduler, wait_s=0.3)
        assert driver.closed  # no session was open: the close went ahead
        release.set()
        thread.join(5)
    assert "write" not in driver.events and not driver.used_after_close
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []


def test_a_dashboard_request_in_flight_at_stop_finishes_before_the_close(monkeypatch):
    engine, driver = fake_engine(monkeypatch)
    driver.gate = threading.Event()
    server = SimpleNamespace(should_exit=False)
    request, errors = run_in_thread(engine.run_cypher, "MATCH (n) RETURN n")  # uvicorn's worker thread
    assert driver.inside.wait(5)
    threading.Timer(0.3, driver.gate.set).start()
    stop_agent(engine, dashboard_server=server, dashboard_thread=request)
    assert server.should_exit is True
    assert driver.events == ["run", "close"] and errors == [] and not driver.used_after_close
    # A request arriving after stop gets the "database unavailable" error.
    late, late_errors = run_in_thread(engine.run_cypher, "MATCH (n) RETURN n")
    late.join(5)
    assert len(late_errors) == 1 and isinstance(late_errors[0], EngineClosed)


def test_stop_is_bounded_when_a_query_hangs(monkeypatch, caplog):
    engine, driver = fake_engine(monkeypatch)
    driver.gate = threading.Event()
    hung, errors = run_in_thread(engine.run_cypher, "MATCH (n) RETURN n")
    assert driver.inside.wait(5)
    stuck_dashboard = threading.Thread(target=threading.Event().wait, args=(10,), daemon=True)
    stuck_dashboard.start()
    started = time.monotonic()
    with caplog.at_level("WARNING"):
        stop_agent(
            engine, dashboard_server=SimpleNamespace(should_exit=False), dashboard_thread=stuck_dashboard,
            wait_s=0.5,
        )
    assert time.monotonic() - started < 1.5
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1
    assert not driver.closed  # abandoned, not closed under the query
    driver.gate.set()
    hung.join(5)
    assert errors == [] and not driver.used_after_close


def test_the_health_check_running_at_stop_ends_before_the_close(monkeypatch, caplog):
    engine, driver = fake_engine(monkeypatch)
    settings = MagicMock(health_check_interval_s=60, dashboard_enabled=False)
    with patch("devgraph.agent.headless.get_settings", return_value=settings), \
         patch("devgraph.agent.headless.RepoRegistry"), \
         patch("devgraph.agent.headless.GraphEngine", return_value=engine), \
         patch("devgraph.agent.headless.WatcherManager"):
        from devgraph.agent.headless import HeadlessAgent

        agent = HeadlessAgent()
    driver.gate = threading.Event()
    health = threading.Thread(target=agent._health_check_loop, daemon=True)
    health.start()
    assert driver.inside.wait(5)
    threading.Timer(0.3, driver.gate.set).start()
    with caplog.at_level("WARNING"):
        agent.stop()
        health.join(5)
    assert not health.is_alive()
    assert driver.events == ["verify", "close"] and not driver.used_after_close
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []
