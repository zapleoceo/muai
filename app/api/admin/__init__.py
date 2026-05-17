from fastapi import APIRouter

from app.api.admin.chats import router as chats_router
from app.api.admin.deploy import router as deploy_router
from app.api.admin.embedder import router as embedder_router
from app.api.admin.settings import router as settings_router
from app.api.admin.stats import router as stats_router
from app.api.admin.sync import router as sync_router
from app.api.admin.tokens import router as tokens_router
from app.api.admin.router_suggestions import router as router_suggestions_router
from app.api.admin.interactions import router as interactions_router
from app.api.admin.style_profile import router as style_profile_router
from app.api.admin.executors import router as executor_router
from app.api.admin.llm_proxy import router as llm_proxy_router
from app.api.admin.rag_proxy import router as rag_proxy_router
from app.api.admin.vera_credentials import router as vera_creds_router

router = APIRouter()
router.include_router(stats_router)
router.include_router(deploy_router)
router.include_router(embedder_router)
router.include_router(tokens_router)
router.include_router(chats_router)
router.include_router(sync_router)
router.include_router(settings_router)
router.include_router(router_suggestions_router)
router.include_router(interactions_router)
router.include_router(style_profile_router)
router.include_router(executor_router)
router.include_router(llm_proxy_router)
router.include_router(rag_proxy_router)
router.include_router(vera_creds_router)
