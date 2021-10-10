"""
Crawl Config API handling
"""

from typing import List, Union, Optional
from enum import Enum
import uuid

from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException

from users import User
from archives import Archive

from db import BaseMongoModel


# ============================================================================
class ScopeType(str, Enum):
    """Crawl scope type"""

    PAGE = "page"
    PAGE_SPA = "page-spa"
    PREFIX = "prefix"
    HOST = "host"
    ANY = "any"


# ============================================================================
class Seed(BaseModel):
    """Crawl seed"""

    url: str
    scopeType: Optional[ScopeType] = ScopeType.PREFIX

    include: Union[str, List[str], None]
    exclude: Union[str, List[str], None]
    sitemap: Union[bool, str, None]
    allowHash: Optional[bool]
    depth: Optional[int]


# ============================================================================
class RawCrawlConfig(BaseModel):
    """Base Crawl Config"""

    seeds: List[Union[str, Seed]]

    collection: Optional[str] = "my-web-archive"

    scopeType: Optional[ScopeType] = ScopeType.PREFIX
    scope: Union[str, List[str], None] = ""
    exclude: Union[str, List[str], None] = ""

    depth: Optional[int] = -1
    limit: Optional[int] = 0

    behaviorTimeout: Optional[int] = 90

    workers: Optional[int] = 1

    headless: Optional[bool] = False

    generateWACZ: Optional[bool] = False
    combineWARC: Optional[bool] = False

    logging: Optional[str] = ""
    behaviors: Optional[str] = "autoscroll"


# ============================================================================
class CrawlConfigIn(BaseModel):
    """CrawlConfig input model, submitted via API"""

    schedule: Optional[str] = ""
    runNow: Optional[bool] = False

    crawlTimeout: Optional[int] = 0
    parallel: Optional[int] = 1

    config: RawCrawlConfig


# ============================================================================
class CrawlConfig(BaseMongoModel):
    """Schedulable config"""

    schedule: Optional[str] = ""
    runNow: Optional[bool] = False

    archive: Optional[str]

    user: Optional[str]

    config: RawCrawlConfig

    crawlTimeout: Optional[int] = 0
    parallel: Optional[int] = 1

    crawlCount: Optional[int] = 0


# ============================================================================
class UpdateSchedule(BaseModel):
    """ Update the crawl schedule """

    schedule: str


# ============================================================================
class CrawlOps:
    """Crawl Config Operations"""

    def __init__(self, mdb, archive_ops, crawl_manager):
        self.crawl_configs = mdb["crawl_configs"]
        self.archive_ops = archive_ops
        self.crawl_manager = crawl_manager

        self.router = APIRouter(
            prefix="/crawlconfigs",
            tags=["crawlconfigs"],
            responses={404: {"description": "Not found"}},
        )

    async def add_crawl_config(
        self, config: CrawlConfigIn, archive: Archive, user: User
    ):
        """Add new crawl config"""
        data = config.dict()
        data["archive"] = archive.id
        data["user"] = str(user.id)
        data["_id"] = str(uuid.uuid4())

        result = await self.crawl_configs.insert_one(data)

        crawlconfig = CrawlConfig.from_dict(data)

        new_name = await self.crawl_manager.add_crawl_config(
            crawlconfig=crawlconfig, storage=archive.storage
        )

        return result, new_name

    async def update_crawl_schedule(self, cid: str, update: UpdateSchedule):
        """ Update schedule for existing crawl config"""

        if not await self.crawl_configs.find_one_and_update(
            {"_id": cid}, {"$set": {"schedule": update.schedule}}
        ):
            return False

        await self.crawl_manager.update_crawl_schedule(cid, update.schedule)
        return True

    async def inc_crawls(self, cid: str):
        """ Increment Crawl Counter """
        await self.crawl_configs.find_one_and_update(
            {"_id": cid}, {"$inc": {"crawlCount": 1}}
        )

    async def get_crawl_configs(self, archive: Archive):
        """Get all crawl configs for an archive is a member of"""
        cursor = self.crawl_configs.find({"archive": archive.id})
        results = await cursor.to_list(length=1000)
        return [CrawlConfig.from_dict(res) for res in results]

    async def get_crawl_config(self, cid: str, archive: Archive):
        """Get an archive for user by unique id"""
        res = await self.crawl_configs.find_one({"_id": cid, "archive": archive.id})
        return CrawlConfig.from_dict(res)

    async def delete_crawl_config(self, cid: str, archive: Archive):
        """Delete config"""
        await self.crawl_manager.delete_crawl_config_by_id(cid)

        return await self.crawl_configs.delete_one({"_id": cid, "archive": archive.id})

    async def delete_crawl_configs(self, archive: Archive):
        """Delete all crawl configs for user"""
        await self.crawl_manager.delete_crawl_configs_for_archive(archive.id)

        return await self.crawl_configs.delete_many({"archive": archive.id})


# ============================================================================
# pylint: disable=redefined-builtin,invalid-name,too-many-locals
def init_crawl_config_api(mdb, user_dep, archive_ops, crawl_manager):
    """Init /crawlconfigs api routes"""
    ops = CrawlOps(mdb, archive_ops, crawl_manager)

    router = ops.router

    archive_crawl_dep = archive_ops.archive_crawl_dep

    async def crawls_dep(cid: str, archive: Archive = Depends(archive_crawl_dep)):
        crawl_config = await ops.get_crawl_config(cid, archive)
        if not crawl_config:
            raise HTTPException(
                status_code=404, detail=f"Crawl Config '{cid}' not found"
            )

        return archive

    @router.get("")
    async def get_crawl_configs(archive: Archive = Depends(archive_crawl_dep)):
        results = await ops.get_crawl_configs(archive)
        return {"crawl_configs": [res.serialize() for res in results]}

    @router.get("/{cid}")
    async def get_crawl_config(crawl_config: CrawlConfig = Depends(crawls_dep)):
        return crawl_config.serialize()

    @router.post("/")
    async def add_crawl_config(
        config: CrawlConfigIn,
        archive: Archive = Depends(archive_crawl_dep),
        user: User = Depends(user_dep),
    ):
        res, new_job_name = await ops.add_crawl_config(config, archive, user)
        return {"added": str(res.inserted_id), "run_now_job": new_job_name}

    @router.patch("/{cid}/schedule", dependencies=[Depends(archive_crawl_dep)])
    async def update_crawl_schedule(
        update: UpdateSchedule,
        cid: str,
    ):

        success = False
        try:
            success = await ops.update_crawl_schedule(cid, update)

        except Exception as e:
            # pylint: disable=raise-missing-from
            raise HTTPException(
                status_code=403, detail=f"Error updating crawl config: {e}"
            )

        if not success:
            raise HTTPException(
                status_code=404, detail=f"Crawl Config '{cid}' not found"
            )

        return {"updated": cid}

    @router.post("/{cid}/run")
    async def run_now(cid: str, archive: Archive = Depends(archive_crawl_dep)):
        crawl_config = await ops.get_crawl_config(cid, archive)

        if not crawl_config:
            raise HTTPException(
                status_code=404, detail=f"Crawl Config '{cid}' not found"
            )

        crawl_id = None
        try:
            crawl_id = await crawl_manager.run_crawl_config(cid)
        except Exception as e:
            # pylint: disable=raise-missing-from
            raise HTTPException(status_code=500, detail=f"Error starting crawl: {e}")

        return {"started": crawl_id}

    @router.delete("")
    async def delete_crawl_configs(archive: Archive = Depends(archive_crawl_dep)):
        result = await ops.delete_crawl_configs(archive)
        return {"deleted": result.deleted_count}

    @router.delete("/{cid}")
    async def delete_crawl_config(
        cid: str, archive: Archive = Depends(archive_crawl_dep)
    ):
        result = await ops.delete_crawl_config(cid, archive)
        if not result or not result.deleted_count:
            raise HTTPException(
                status_code=404, detail=f"Crawl Config '{cid}' Not Found"
            )

        return {"deleted": 1}

    archive_ops.router.include_router(router)

    return ops