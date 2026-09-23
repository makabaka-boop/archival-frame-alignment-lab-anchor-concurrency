"""并发裁决验收：真实 PostgreSQL + 真实 HTTP。

用同步屏障（threading.Barrier）让两个修复师的请求在同一刻发出，交错竞争同一
作业；裁决权完全在服务端（PostgreSQL 行锁 + 条件 UPDATE），客户端不做任何
串行化。覆盖：

1. 两组**非空替换**从同一旧版本并发提交：恰好一份成功、一份结构化 409；
2. **清空与非空替换**并发：唯一裁决，清空落败时不得抹掉人工锚点；
3. **超时重放**：已成功的旧请求原样重试（含“清空锚点”的旧重试）必得 409，
   锚点、结果、version、updated_at 全部维持真值；
4. 顺序带前提替换、首次设置、合法清空、422 校验等既有行为保持兼容。

成功响应与随后（含重复）GET 读到的持久状态必须逐项一致，并用 psycopg 直查
PostgreSQL 交叉核对落库列。
"""

from __future__ import annotations

import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from brute import brute  # noqa: E402

from conftest import create_job  # noqa: E402

API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000")
PG_DSN = os.environ.get("PG_DSN", "postgresql://lcs:lcs@localhost:5432/lcs")

# 固定的小用例：两侧各 6 项，存在多组交叉对齐，便于让两组锚点导致不同结果。
LEFT = ["a", "b", "c", "a", "b", "c"]
RIGHT = ["b", "c", "a", "b", "c", "a"]
ANCHORS_A = [[0, 2]]  # a -> a
ANCHORS_B = [[1, 0]]  # b -> b（与 A 互不相容的另一种核实）
ANCHORS_C = [[4, 3]]  # b -> b，可与 A 共存（两侧索引都更大）


def _new_client() -> httpx.Client:
    return httpx.Client(base_url=API_BASE_URL, timeout=60.0)


def _pairs(body: dict) -> list[tuple[int, int]]:
    return [(p["left_index"], p["right_index"]) for p in body["result"]]


def _anchor_pairs(body: dict) -> list[tuple[int, int]]:
    return [(p["left_index"], p["right_index"]) for p in body["anchors"]]


def _put(
    client: httpx.Client,
    job_id: str,
    anchors: list[list[int]],
    expected_version: int | None,
) -> httpx.Response:
    body: dict[str, Any] = {"anchors": anchors}
    if expected_version is not None:
        body["expected_version"] = expected_version
    return client.put(f"/api/jobs/{job_id}/anchors", json=body)


def _assert_chain(body: dict) -> list[tuple[int, int]]:
    """结果必须是合法对应：两侧严格递增且指纹相等。"""
    pairs = _pairs(body)
    assert body["length"] == len(pairs)
    prev_i, prev_j = -1, -1
    for i, j in pairs:
        assert i > prev_i and j > prev_j
        assert LEFT[i] == RIGHT[j]
        prev_i, prev_j = i, j
    return pairs


def _assert_409(body: dict, expected: int, current: int) -> None:
    assert body["code"] == "version_conflict"
    assert body["expected_version"] == expected
    assert body["current_version"] == current
    assert isinstance(body["detail"], str) and body["detail"]


def _run_interleaved(job_id: str, call_a, call_b) -> tuple[tuple[int, dict], tuple[int, dict]]:
    """两个请求在屏障处会合后同时发出；各自使用独立连接。"""
    barrier = threading.Barrier(2)

    def task(call) -> tuple[int, dict]:
        with _new_client() as c:
            barrier.wait(timeout=10)
            resp = call(c)
            return resp.status_code, resp.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(task, call_a)
        future_b = pool.submit(task, call_b)
        return future_a.result(timeout=30), future_b.result(timeout=30)


def _assert_exactly_one_winner(
    outcome_a: tuple[int, dict],
    outcome_b: tuple[int, dict],
    base_version: int,
) -> tuple[int, dict, int, dict]:
    """返回 (赢家下标, 赢家响应体, 输家下标, 输家响应体)。"""
    outcomes = (outcome_a, outcome_b)
    statuses = [status for status, _ in outcomes]
    assert sorted(statuses) == [200, 409], f"应恰好一份 200、一份 409，实际 {statuses}"

    winner_idx = 0 if statuses[0] == 200 else 1
    loser_idx = 1 - winner_idx
    _, winner = outcomes[winner_idx]
    _, loser = outcomes[loser_idx]

    # 成功方版本严格 +1；输家的 409 指明同样的当前版本。
    assert winner["version"] == base_version + 1
    _assert_409(loser, expected=base_version, current=base_version + 1)
    return winner_idx, winner, loser_idx, loser


# ------------------------------------------------- 两组非空替换交错


def test_two_nonempty_replacements_interleaved_single_arbitrator(
    client: httpx.Client,
) -> None:
    job = create_job(client, LEFT, RIGHT)
    job_id = job["id"]
    assert job["version"] == 1

    outcome_a, outcome_b = _run_interleaved(
        job_id,
        lambda c: _put(c, job_id, ANCHORS_A, 1),
        lambda c: _put(c, job_id, ANCHORS_B, 1),
    )
    winner_idx, winner, _, _ = _assert_exactly_one_winner(outcome_a, outcome_b, 1)
    expected_anchors = ANCHORS_A if winner_idx == 0 else ANCHORS_B

    # 最终持久状态只属于赢家：锚点恰为赢家集合，结果是含该锚点的最优解。
    got = client.get(f"/api/jobs/{job_id}").json()
    assert got == winner  # 成功响应与随后 GET 逐项一致（含 version、updated_at）
    assert _anchor_pairs(got) == [tuple(expected_anchors[0])]
    pairs = _assert_chain(got)
    assert pairs == brute(LEFT, RIGHT, expected_anchors)

    # 重复 GET 必须完全一致。
    got_again = client.get(f"/api/jobs/{job_id}").json()
    assert got_again == got

    # 直查 PostgreSQL：落库列与 API 真值逐项一致。
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(PG_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT anchors, result, version, updated_at FROM jobs WHERE id = %s",
            (job_id,),
        )
        db_anchors, db_result, db_version, db_updated = cur.fetchone()
    assert [tuple(p) for p in db_anchors] == _anchor_pairs(got)
    assert [tuple(p) for p in db_result] == pairs
    assert db_version == got["version"] == 2
    # PG 的 timestamptz 与 JSON 里的 updated_at 是同一时刻。
    assert db_updated == datetime.fromisoformat(got["updated_at"])


# ------------------------------------------------- 清空 与 非空 交错


def test_clear_and_nonempty_interleaved_only_winner_survives(
    client: httpx.Client,
) -> None:
    job = create_job(client, LEFT, RIGHT)
    job_id = job["id"]
    global_pairs = brute(LEFT, RIGHT)

    # 修复师甲：清空锚点（恢复全局最优）；修复师乙：设置非空人工锚点。
    # 两人都基于版本 1。
    outcome_clear, outcome_set = _run_interleaved(
        job_id,
        lambda c: _put(c, job_id, [], 1),
        lambda c: _put(c, job_id, ANCHORS_B, 1),
    )
    winner_idx, winner, _, _ = _assert_exactly_one_winner(
        outcome_clear, outcome_set, 1
    )

    got = client.get(f"/api/jobs/{job_id}").json()
    assert got == winner
    got_again = client.get(f"/api/jobs/{job_id}").json()
    assert got_again == got

    if winner_idx == 0:  # 清空胜出：无锚点，结果为全局最优
        assert _anchor_pairs(got) == []
        assert _pairs(got) == global_pairs
    else:  # 非空替换胜出：清空落败，人工锚点与重算结果完整保留
        assert _anchor_pairs(got) == [tuple(ANCHORS_B[0])]
        assert _pairs(got) == brute(LEFT, RIGHT, ANCHORS_B)
    _assert_chain(got)


# ------------------------------------------------- 超时重放


def test_timeout_replay_stale_request_never_overwrites(client: httpx.Client) -> None:
    job = create_job(client, LEFT, RIGHT)
    job_id = job["id"]

    # 修复师甲基于版本 1 完成核实 A（请求其实成功，但客户端超时未见到响应）。
    resp = _put(client, job_id, ANCHORS_A, 1)
    assert resp.status_code == 200, resp.text
    after_a = resp.json()
    assert after_a["version"] == 2
    assert _pairs(after_a) == brute(LEFT, RIGHT, ANCHORS_A)

    # 客户端按超时逻辑原样重放同一请求（仍带着旧前提版本 1）：
    # 必须被判过期，且状态分毫不改。
    replay = _put(client, job_id, ANCHORS_A, 1)
    assert replay.status_code == 409, replay.text
    _assert_409(replay.json(), expected=1, current=2)
    got = client.get(f"/api/jobs/{job_id}").json()
    assert got == after_a  # 含 updated_at 在内完全不变

    # 修复师乙重新 GET 后基于版本 2 追加兼容锚点 C：替换语义要求提交完整并集。
    combined_anchors = sorted([ANCHORS_A[0], ANCHORS_C[0]])
    resp2 = _put(client, job_id, combined_anchors, 2)
    assert resp2.status_code == 200, resp2.text
    after_c = resp2.json()
    assert after_c["version"] == 3
    combined = sorted([tuple(ANCHORS_A[0]), tuple(ANCHORS_C[0])])
    assert _anchor_pairs(after_c) == combined
    assert _pairs(after_c) == brute(LEFT, RIGHT, combined)
    assert client.get(f"/api/jobs/{job_id}").json() == after_c

    # 更老的“清空锚点”重试（基于版本 1）尤其不得把已确认结论抹掉。
    stale_clear = _put(client, job_id, [], 1)
    assert stale_clear.status_code == 409
    _assert_409(stale_clear.json(), expected=1, current=3)
    got = client.get(f"/api/jobs/{job_id}").json()
    assert got == after_c  # 锚点、结果、updated_at 全部维持版本 3 的真值
    assert _anchor_pairs(got) == combined

    # 重放版本 2 的旧请求（提交的是仅含 C 的旧集合）同样过期。
    stale_c = _put(client, job_id, ANCHORS_C, 2)
    assert stale_c.status_code == 409
    _assert_409(stale_c.json(), expected=2, current=3)
    assert client.get(f"/api/jobs/{job_id}").json() == after_c

    # 过期且非法的锚点：先校验出 422，依旧不触碰任何状态。
    invalid = _put(client, job_id, [[99, 99]], 1)
    assert invalid.status_code == 422
    assert isinstance(invalid.json()["detail"], str) and invalid.json()["detail"]
    assert client.get(f"/api/jobs/{job_id}").json() == after_c

    # 终态与重复读一致性。
    final = client.get(f"/api/jobs/{job_id}").json()
    assert final == after_c
    _assert_chain(final)


# ------------------------------------------------- 顺序兼容


def test_sequential_pinned_replacements_first_set_and_clear(
    client: httpx.Client,
) -> None:
    job = create_job(client, LEFT, RIGHT)
    job_id = job["id"]
    global_pairs = brute(LEFT, RIGHT)

    # 首次设置：基于版本 1 成功。
    r1 = _put(client, job_id, ANCHORS_A, 1)
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert body1["version"] == 2
    assert _anchor_pairs(body1) == [tuple(ANCHORS_A[0])]
    assert client.get(f"/api/jobs/{job_id}").json() == body1

    # 合法清空：基于版本 2 成功，恢复全局最优。
    r2 = _put(client, job_id, [], 2)
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["version"] == 3
    assert _anchor_pairs(body2) == []
    assert _pairs(body2) == global_pairs
    assert client.get(f"/api/jobs/{job_id}").json() == body2

    # 顺序重试旧前提必失败，状态停留在版本 3。
    stale = _put(client, job_id, ANCHORS_A, 2)
    assert stale.status_code == 409
    _assert_409(stale.json(), expected=2, current=3)
    assert client.get(f"/api/jobs/{job_id}").json() == body2


def test_invalid_expected_version_is_422(client: httpx.Client) -> None:
    job = create_job(client, LEFT, RIGHT)
    job_id = job["id"]
    for bad in (0, -3):
        resp = _put(client, job_id, ANCHORS_A, bad)
        assert resp.status_code == 422, bad
    # 校验失败不改变状态。
    got = client.get(f"/api/jobs/{job_id}").json()
    assert got["version"] == 1
    assert _anchor_pairs(got) == []
