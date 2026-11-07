"""Shared harness: one manager process on its own port, fresh database each run."""

import os
import socket
import subprocess
import sys
import time

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8898
MCP = f"http://127.0.0.1:{PORT}/mcp"
API = f"http://127.0.0.1:{PORT}/api/v1"
ADMIN = "testadmin"
# seeded keys: which group a key belongs to is the whole product
ALPHA = "mgrtest_alpha"   # group full
BETA = "mgrtest_beta"     # group imagelibrary
DB = os.path.join(ROOT, "tests", "test.db")
CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config_test.yaml")
LOCAL = os.path.join(ROOT, "tests", "upstreams.local.yaml")
# The live suites run against real MCP servers. Their addresses are a developer's own, so they
# live in a gitignored file; see upstreams.example.yaml.
UPSTREAMS = {}
if os.path.exists(LOCAL):
    with open(LOCAL, encoding="utf-8") as f:
        UPSTREAMS = yaml.safe_load(f) or {}


@pytest.fixture(scope="session")
def real_servers():
    """For a suite that asserts real servers' tools: without them it skips, it does not fail."""
    if not UPSTREAMS:
        pytest.skip("needs real MCP servers: see tests/upstreams.example.yaml")


@pytest.fixture(scope="session")
def manager(tmp_path_factory):
    config = yaml.safe_load(open(CONFIG, encoding="utf-8"))
    config["upstreams"] = [{"name": name, "url": url} for name, url in UPSTREAMS.items()]
    config_file = tmp_path_factory.mktemp("manager") / "config.yaml"
    config_file.write_text(yaml.safe_dump(config), encoding="utf-8")
    dbfile = os.path.join(ROOT, "tests", "test.db")
    if os.path.exists(dbfile):
        os.remove(dbfile)  # the seed only runs on an empty DB; a stale one silently wins
    logfile = open(os.path.join(ROOT, "tests", "test.log"), "w", encoding="utf-8")
    # A PIPE nobody drains fills up and blocks the child — the fast tests pass, then
    # everything after hangs on a log line. Send it to a file and read it on failure.
    p = subprocess.Popen([sys.executable, "-m", "mcp_manager", str(config_file)], cwd=ROOT,
                         stdout=logfile, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(60):
            time.sleep(1)
            if p.poll() is not None:
                pytest.fail(f"manager exited:\n{open(os.path.join(ROOT, 'tests', 'test.log')).read()}")
            try:
                socket.create_connection(("127.0.0.1", PORT), timeout=2).close()
                break
            except OSError:
                continue
        else:
            pytest.fail("manager did not open its port in 60s")
        yield p
    finally:
        p.terminate()
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
        logfile.close()
