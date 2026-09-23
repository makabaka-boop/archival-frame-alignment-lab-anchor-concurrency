"""FastAPI 纯后端：扫描对应（LCS）任务的创建、查询与锚点重算。

协议见 README。所有非法输入（请求体域校验、非法锚点）统一返回 422，
且锚点非法时数据库原状态不变；任务不存在返回 404。

并发：``PUT anchors`` 在携带 ``expected_version`` 时是一次 compare-and-set——
只有该前提仍等于数据库当前版本，条件 UPDATE 才命中、替换才生效；否则返回
409，锚点、匹配结果与更新时间保持调用前状态。条件判断、写入与版本递增在
单条 UPDATE 内原子完成，PostgreSQL 的行锁保证同作业的并发替换只有一份能成功。
"""

from __future__ import annotations

import uuid

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, update
from sqlalchemy.orm import Session

from . import lcs
from .db import INITIAL_VERSION, Job, get_session, init_db
from .schemas import AnchorIn, JobCreate, JobOut, Pair
from .validation import InvalidInput, validate_side

app = FastAPI(
    title="胶片扫描对应 API",
    version="1.0.0",
    description="两台扫描机指纹数组的最长对应（LCS）求解，支持锚点重算。",
)


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.exception_handler(InvalidInput)
async def _invalid_input_handler(_request: Request, exc: InvalidInput) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(lcs.AnchorError)
async def _anchor_error_handler(_request: Request, exc: lcs.AnchorError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


def _serialize(job: Job) -> JobOut:
    return JobOut(
        id=job.id,
        left=job.left_data,
        right=job.right_data,
        anchors=[Pair(left_index=i, right_index=j) for i, j in job.anchors],
        result=[Pair(left_index=i, right_index=j) for i, j in job.result],
        length=len(job.result),
        version=job.version,
        updated_at=job.updated_at,
    )


@app.post("/api/jobs", response_model=JobOut, status_code=201)
def create_job(payload: JobCreate, session: Session = Depends(get_session)) -> JobOut:
    # 域校验失败抛 InvalidInput -> 422，不写库。
    validate_side("left", payload.left)
    validate_side("right", payload.right)
    result = lcs.solve(payload.left, payload.right)
    job = Job(
        id=str(uuid.uuid4()),
        left_data=payload.left,
        right_data=payload.right,
        anchors=[],
        result=result,
        version=INITIAL_VERSION,
    )
    session.add(job)
    session.commit()
    # 丢弃身份映射中可能过期的属性，强制回读数据库真值（含 updated_at）。
    session.expire_all()
    return _serialize(session.get(Job, job.id))


@app.get("/api/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, session: Session = Depends(get_session)) -> JobOut:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _serialize(job)


@app.put(
    "/api/jobs/{job_id}/anchors",
    response_model=JobOut,
    responses={
        409: {
            "description": "版本前提过期：已有其他替换成功，当前状态未改变",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "锚点所基于的版本 3 已过期，当前版本为 4；"
                        "请重新读取当前锚点与结果后再提交",
                        "code": "version_conflict",
                        "expected_version": 3,
                        "current_version": 4,
                    }
                }
            },
        }
    },
)
def replace_anchors(
    job_id: str,
    payload: AnchorIn,
    session: Session = Depends(get_session),
) -> JobOut:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    # 先完整校验（非法抛 AnchorError -> 422），通过后才重算与落库，
    # 保证“非法锚点返回 422，原状态不变”。校验与裁决均不写库，所以过期且
    # 非法的请求同样不留下任何痕迹。
    ordered = lcs.validate_anchors(job.left_data, job.right_data, payload.anchors)
    new_result = lcs.solve(job.left_data, job.right_data, ordered)

    # compare-and-set：携带前提时把版本写进 WHERE，未携带前提则仅按行主键
    # 更新（兼容旧客户端）。PostgreSQL 对命中行加排他行锁并在 READ COMMITTED
    # 下等待先行事务结束后重读最新版本，因此同作业的并发替换有唯一裁决。
    criteria = [Job.id == job_id]
    if payload.expected_version is not None:
        criteria.append(Job.version == payload.expected_version)
    stmt = (
        update(Job)
        .where(*criteria)
        .values(
            anchors=ordered,
            result=new_result,
            version=Job.version + 1,
            updated_at=func.now(),
        )
    )
    rowcount = session.execute(stmt).rowcount
    if rowcount == 0:
        # 仅可能因为版本前提过期：作业开始前已确认存在，且主键不会被删除。
        session.rollback()
        current = session.get(Job, job_id)
        return JSONResponse(
            status_code=409,
            content={
                "detail": (
                    f"锚点所基于的版本 {payload.expected_version} 已过期，"
                    f"当前版本为 {current.version}；请重新读取当前锚点与结果后再提交"
                ),
                "code": "version_conflict",
                "expected_version": payload.expected_version,
                "current_version": current.version,
            },
        )
    session.commit()
    # 回读数据库真值后再序列化，保证成功响应与随后 GET 逐项一致
    # （updated_at 取自 now()，不能用应用端时钟自造）。
    session.expire_all()
    return _serialize(session.get(Job, job_id))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
