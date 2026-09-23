"""FastAPI 纯后端：扫描对应（LCS）任务的创建、查询与锚点重算。

协议见 README。所有非法输入（请求体域校验、非法锚点）统一返回 422，
且锚点非法时数据库原状态不变；任务不存在返回 404。
"""

from __future__ import annotations

import uuid

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from . import lcs
from .db import Job, get_session, init_db
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
    )
    session.add(job)
    session.commit()
    return _serialize(job)


@app.get("/api/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, session: Session = Depends(get_session)) -> JobOut:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _serialize(job)


@app.put("/api/jobs/{job_id}/anchors", response_model=JobOut)
def replace_anchors(
    job_id: str,
    payload: AnchorIn,
    session: Session = Depends(get_session),
) -> JobOut:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    # 先完整校验（非法抛 AnchorError -> 422），通过后才重算与落库，
    # 保证“非法锚点返回 422，原状态不变”。
    ordered = lcs.validate_anchors(job.left_data, job.right_data, payload.anchors)
    job.anchors = ordered
    job.result = lcs.solve(job.left_data, job.right_data, ordered)
    session.commit()
    return _serialize(job)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
