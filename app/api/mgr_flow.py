from typing import List, Dict, Optional
from typing import Annotated
from fastapi import Body, FastAPI
import asyncio
import uuid
import traceback
import json
import aiofiles
from aiofiles import os as aiofiles_os
from fastapi import APIRouter
from loguru import logger
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi.background import BackgroundTasks
from sse_starlette.sse import EventSourceResponse
from app.core.config import settings
from app.schemas.fanode import FARunnerStatus
from app.schemas.vfnode import VFlowData
from app.services.FARunner import FARunner
from app.services.FAValidator import FAValidator
from app.services.messageMgr import ALL_MESSAGES_MGR
from app.services.taskMgr import ALL_TASKS_MGR
from app.schemas.farequest import (
    ValidationError,
    FANodeUpdateType,
    FANodeUpdateData,
    SSEResponse,
    SSEResponseData,
    SSEResponseType,
    FAWorkflowNodeResult,
    FAWorkflowResult,
    FAWorkflow,
    FAWorkflowLocation,
    FAWorkflowUpdateRequset,
    FAWorkflowReadRequest,
    FAWorkflowOperationResponse,
    FAWorkflowInfo,
    FAReleaseWorkflowInfo,
    FAWorkflowNodeRequest,
)
from app.services.FARunner import FARunner
from app.db.session import get_db_ctxmgr
from app.models.fastore import (
    FAWorkflowModel,
    FAReleasedWorkflowModel,
    FANodeCacheModel,
)
from sqlalchemy import select, update, exc, exists, delete
from uuid_extensions import uuid7str

router = APIRouter()


@router.post("/create")
async def create_workflow(create_request: FAWorkflow):
    try:
        async with get_db_ctxmgr() as db:
            db_wf = FAWorkflowModel(
                wid=uuid7str().replace("-", ""),
                name=create_request.name,
                curVFlow=create_request.vflow,
                lastModified=datetime.now(ZoneInfo("Asia/Shanghai")),
            )
            db.add(db_wf)
            await db.commit()
            await db.refresh(db_wf)
            return FAWorkflowOperationResponse(success=True, data=db_wf.wid)
    except Exception as e:
        errmsg = traceback.format_exc()
        logger.error(f"create workflow error: {errmsg}")
        return FAWorkflowOperationResponse(success=False, message=errmsg)


@router.get("/readall")
async def read_all_workflows():
    try:
        async with get_db_ctxmgr() as db:
            stmt = select(
                FAWorkflowModel.wid, FAWorkflowModel.name, FAWorkflowModel.lastModified
            )
            db_result = await db.execute(stmt)
            db_workflows = db_result.mappings().all()
            result: List[FAWorkflowInfo] = []
            for db_wf in db_workflows:
                result.append(
                    FAWorkflowInfo(
                        wid=db_wf["wid"],
                        name=db_wf["name"],
                        lastModified=db_wf["lastModified"],
                    )
                )
            # 按照最近修改时间排序
            result.sort(key=lambda x: x.lastModified, reverse=True)
            return FAWorkflowOperationResponse(success=True, data=result)
    except Exception as e:
        errmsg = traceback.format_exc()
        logger.error(f"read all workflows error: {errmsg}")
        return FAWorkflowOperationResponse(success=False, message=errmsg)


@router.post("/read")
async def read_workflow(read_request: FAWorkflowReadRequest):
    try:
        async with get_db_ctxmgr() as db:
            stmt = select(exists().where(FAWorkflowModel.wid == read_request.wid))
            db_result = await db.execute(stmt)
            db_exists = db_result.scalar()
            if db_exists:
                result = {}
                for location in read_request.locations:
                    if location == FAWorkflowLocation.name:
                        stmt = select(FAWorkflowModel.name).filter(
                            FAWorkflowModel.wid == read_request.wid
                        )
                        db_result = await db.execute(stmt)
                        name = db_result.scalars().first()
                        result[location.value] = name
                    elif location == FAWorkflowLocation.vflow:
                        stmt = select(FAWorkflowModel.curVFlow).filter(
                            FAWorkflowModel.wid == read_request.wid
                        )
                        db_result = await db.execute(stmt)
                        vflow = db_result.scalars().first()
                        result[location.value] = vflow
                    elif location == FAWorkflowLocation.release:
                        stmt = (
                            select(FAReleasedWorkflowModel)
                            .filter(FAReleasedWorkflowModel.rwid == read_request.rwid)
                            .filter(FAReleasedWorkflowModel.wid == read_request.wid)
                        )
                        db_result = await db.execute(stmt)
                        db_result = db_result.scalars().first()
                        wfresult = FAReleaseWorkflowInfo(
                            rwid=db_result.rwid,
                            releaseTime=db_result.releaseTime,
                            name=db_result.name,
                            description=db_result.description,
                        )
                        result[location.value] = wfresult
                    elif location == FAWorkflowLocation.allReleases:
                        stmt = (
                            select(FAReleasedWorkflowModel)
                            .filter(FAReleasedWorkflowModel.wid == read_request.wid)
                            .order_by(FAReleasedWorkflowModel.releaseTime.desc())
                        )
                        db_result = await db.execute(stmt)
                        db_results = db_result.scalars().all()
                        wfresults = []
                        for db_res in db_results:
                            wfresults.append(
                                FAReleaseWorkflowInfo(
                                    rwid=db_res.rwid,
                                    releaseTime=db_res.releaseTime,
                                    name=db_res.name,
                                    description=db_res.description,
                                )
                            )
                        result[location.value] = wfresults
                return FAWorkflowOperationResponse(success=True, data=result)
            else:
                return FAWorkflowOperationResponse(
                    success=False, message="Workflow not found"
                )
    except Exception as e:
        errmsg = traceback.format_exc()
        logger.error(f"read workflow error: {errmsg}")
        return FAWorkflowOperationResponse(success=False, message=errmsg)


@router.post("/update")
async def update_workflow(update_request: FAWorkflowUpdateRequset):
    try:
        async with get_db_ctxmgr() as db:
            stmt = select(exists().where(FAWorkflowModel.wid == update_request.wid))
            db_result = await db.execute(stmt)
            db_exists = db_result.scalar()
            if db_exists:
                if update_request.location == FAWorkflowLocation.name and isinstance(
                    update_request.data, str
                ):
                    await db.execute(
                        update(FAWorkflowModel)
                        .where(FAWorkflowModel.wid == update_request.wid)
                        .values(name=update_request.data)
                    )
                    await db.commit()
                elif update_request.location == FAWorkflowLocation.vflow and isinstance(
                    update_request.data, dict
                ):
                    await db.execute(
                        update(FAWorkflowModel)
                        .where(FAWorkflowModel.wid == update_request.wid)
                        .values(curVFlow=update_request.data)
                    )
                    await db.commit()
                else:
                    pass
                pass
                # 更新最近时间
                await db.execute(
                    update(FAWorkflowModel)
                    .where(FAWorkflowModel.wid == update_request.wid)
                    .values(lastModified=datetime.now(ZoneInfo("Asia/Shanghai")))
                )
                await db.commit()
                return FAWorkflowOperationResponse(success=True)
            else:
                return FAWorkflowOperationResponse(
                    success=False, message="Workflow not found"
                )

    except Exception as e:
        errmsg = traceback.format_exc()
        logger.error(f"update workflow error: {errmsg}")
        return FAWorkflowOperationResponse(success=False, message=errmsg)


@router.post("/delete")
async def delete_workflow(wid: int):
    try:
        async with get_db_ctxmgr() as db:
            # 使用 ORM 查询
            stmt = select(FAWorkflowModel).where(FAWorkflowModel.wid == wid)
            result = await db.execute(stmt)
            workflow = result.scalar()

            if workflow:
                # 使用 ORM 的 delete 方法
                await db.delete(workflow)
                await db.commit()
                return FAWorkflowOperationResponse(success=True)
            else:
                return FAWorkflowOperationResponse(
                    success=False, message="Workflow not found"
                )
    except Exception as e:
        errmsg = traceback.format_exc()
        logger.error(f"delete workflow error: {errmsg}")
        return FAWorkflowOperationResponse(success=False, message=errmsg)


@router.post("/deleteresult")
async def delete_result(wid: int, tid: str):
    try:
        farunner: FARunner = await ALL_TASKS_MGR.get(tid)
        if farunner:
            await ALL_TASKS_MGR.remove(tid)

        async with get_db_ctxmgr() as db:
            await db.execute(
                delete(FAWorkflowResultModel).where(
                    FAWorkflowResultModel.tid == tid,
                    FAWorkflowResultModel.wid == wid,
                )
            )
            await db.commit()
            return FAWorkflowOperationResponse(success=True)
    except Exception as e:
        errmsg = traceback.format_exc()
        logger.error(f"delete result error: {errmsg}")
        return FAWorkflowOperationResponse(success=False, message=errmsg)


@router.get("/readallresults")
async def read_all_results(wid: int):
    try:
        result = []
        addedtid = set()
        runnertid = await ALL_TASKS_MGR.getAllTaskID()
        for tid in runnertid:
            farunner: FARunner = await ALL_TASKS_MGR.get(tid)
            if farunner and farunner.wid == wid:
                addedtid.add(tid)
                result.append(
                    FAResultBaseInfo(
                        tid=farunner.tid,
                        status=farunner.status,
                        starttime=farunner.starttime,
                        endtime=farunner.endtime,
                    )
                )

        async with get_db_ctxmgr() as db:
            stmt = select(
                FAWorkflowResultModel.tid,
                FAWorkflowResultModel.status,
                FAWorkflowResultModel.starttime,
                FAWorkflowResultModel.endtime,
            ).filter(FAWorkflowResultModel.wid == wid)
            db_result = await db.execute(stmt)
            db_results = db_result.mappings().all()
            for db_res in db_results:
                tid = db_res["tid"]
                if tid in addedtid:
                    continue
                result.append(
                    FAResultBaseInfo(
                        tid=db_res["tid"],
                        status=db_res["status"],
                        starttime=db_res["starttime"],
                        endtime=db_res["endtime"],
                    )
                )
        # 将result按照开始时间最新在前排序
        result.sort(
            key=lambda x: x.starttime.replace(tzinfo=ZoneInfo("Asia/Shanghai")),
            reverse=True,
        )
        return FAWorkflowOperationResponse(success=True, data=result)
    except Exception as e:
        errmsg = traceback.format_exc()
        logger.error(f"read all results error: {errmsg}")
        return FAWorkflowOperationResponse(success=False, message=errmsg)


@router.post("/loadresult")
async def load_result(wid: int, tid: str):
    try:
        test_farunner: FARunner = await ALL_TASKS_MGR.get(tid)
        if test_farunner:
            return FAWorkflowOperationResponse(
                success=True,
                data=test_farunner.oriflowdata,
            )
        else:
            test_farunner = FARunner(tid)
            loadOK = await test_farunner.loadResult(wid, tid)
            if loadOK:
                await ALL_TASKS_MGR.add(test_farunner)
                return FAWorkflowOperationResponse(
                    success=True,
                    data=test_farunner.oriflowdata,
                )
            else:
                return FAWorkflowOperationResponse(
                    success=False, message="Result not found"
                )
    except Exception as e:
        errmsg = traceback.format_exc()
        logger.error(f"load result error: {errmsg}")
        return FAWorkflowOperationResponse(success=False, message=errmsg)
