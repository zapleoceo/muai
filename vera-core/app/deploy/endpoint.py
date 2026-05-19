import logging

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException
from pydantic import BaseModel

from app.config import get_settings
from app.deploy.runner import deploy

log = logging.getLogger(__name__)
router = APIRouter()


class DeployPayload(BaseModel):
    ref: str = "refs/heads/master"
    message: str = ""


@router.post("/deploy")
async def deploy_endpoint(
    payload: DeployPayload,
    background_tasks: BackgroundTasks,
    authorization: str = Header(...),
) -> dict:
    settings = get_settings()
    expected = f"Bearer {settings.deploy_secret}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Invalid deploy secret")

    background_tasks.add_task(deploy, payload.ref, payload.message)
    log.info("Deploy triggered for ref=%s", payload.ref)
    return {"status": "deploying", "ref": payload.ref}
