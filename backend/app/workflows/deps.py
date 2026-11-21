"""编排节点可用的服务集合；只注入既有 service，不引入新的业务实现。"""

from dataclasses import dataclass

from app.adapters.remote_browser import AgentChannel
from app.db.repository import Repository
from app.services.aba import AbaService
from app.services.aba_expansion import ExpansionService
from app.services.browser_devices import BrowserDeviceStore
from app.services.cleaning import CleaningService
from app.services.competitors import CompetitorService
from app.services.core_terms import CoreService
from app.services.hot_search_collection import HotSearchCollectionService
from app.services.hot_search_core_analysis import HotSearchCoreAnalysisService
from app.services.library import LibraryService
from app.services.library_relevance import LibraryRelevanceService
from app.services.product_confirmation import ProductConfirmationService
from app.services.product_profiles import ProductProfileService
from app.services.product_term_analysis import ProductTermAnalysisService
from app.services.product_understanding import ProductUnderstandingService
from app.services.qwen import QwenClient
from app.services.roots import RootService
from app.services.sellersprite_collection import SellerspriteCollectionService
from app.services.third_party import ThirdPartyService
from app.services.traffic import TrafficService
from app.workflows.browser_operations import BrowserOperationStore


@dataclass(frozen=True)
class WorkflowDeps:
    repository: Repository
    understanding: ProductUnderstandingService
    confirmation: ProductConfirmationService
    profiles: ProductProfileService
    term_analysis: ProductTermAnalysisService
    roots: RootService
    aba: AbaService
    core: CoreService
    hot_search_core_analysis: HotSearchCoreAnalysisService
    hot_search_collection: HotSearchCollectionService
    competitors: CompetitorService
    expansion: ExpansionService
    third_party: ThirdPartyService
    sellersprite: SellerspriteCollectionService
    cleaning: CleaningService
    traffic: TrafficService
    library: LibraryService
    model: QwenClient | None = None
    library_relevance: LibraryRelevanceService | None = None
    browser_operations: BrowserOperationStore | None = None
    server_only: bool = False
    devices: BrowserDeviceStore | None = None
    channel: AgentChannel | None = None
