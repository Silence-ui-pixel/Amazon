"""Auto POE union size must not block price-first filtering of all core words."""
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4

from app.adapters.competitor_csv import parse_competitor_csv
from app.adapters.poe_downloads import PoeArchive
from app.domain.asin_product import ProductSnapshot
from app.domain.competitors import Competitor, Competitors, DiscoveryInput, PoeIntentCandidate, PoeIntentChoice
from app.domain.errors import DomainError
from app.domain.product_terms import BatchComparisonSet, ProductProfile
from app.services.competitors import CompetitorService


def asin(index):
    return f'B{index:09d}'


class AutomaticLimitTests(unittest.TestCase):
    def make_service(self, qualified_prices, low_scores=()):
        self.project,self.run,self.core_id=uuid4(),uuid4(),uuid4()
        self.previous=None
        self.fetched=[]
        self.compared=[]
        self.core=SimpleNamespace(id=self.core_id,stale=False,items=[SimpleNamespace(keyword=k) for k in ['rack','stand']])
        self.product=SimpleNamespace(marketplace='US',asin='B999999999',reference_price=Decimal('100'),reference_currency='USD')
        profile=ProductProfile(purpose=['storage'],style_structure=['rack'],specifications=['steel'],
            price=Decimal('100'),currency='USD',package_quantity=Decimal('1'),unit_basis='piece')
        def csv_for(start):
            return 'ASIN,Title,Average Selling Price Past 30 Days (USD)\n'+''.join(
                f'{asin(i)},Storage rack,{qualified_prices.get(i,"200")}\n' for i in range(start,start+300))
        payloads={'rack':csv_for(0),'stand':csv_for(250)}
        def download(run,keyword,ref):
            text=payloads[keyword]
            return PoeArchive(uuid4(),Path('unused'),text,'abc',parse_competitor_csv(text,'report',require_30_day=True),keyword+'.csv','https://sellercentral.amazon.com/opportunity-explorer/')
        def fetch_many(marketplace,asins):
            self.fetched.extend(asins)
            return [(a,ProductSnapshot(asin=a,title='Storage rack',price=Decimal('100'),currency='USD'),None) for a in asins]
        def compare(reference,products):
            self.compared.append([p.asin for p in products])
            return BatchComparisonSet.model_validate({'items':[{
                'asin':p.asin,'valid':True,'purpose':['storage'],'style_structure':['rack'],'specifications':['steel'],
                'package_quantity':1,'unit_basis':'piece','scores':{
                    'purpose':30 if p.asin in low_scores else 80,'style_structure':30 if p.asin in low_scores else 80,
                    'specifications':30 if p.asin in low_scores else 80,
                    'purpose_reason':'purpose evidence','style_structure_reason':'structure evidence','specifications_reason':'spec evidence'}} for p in products]})
        self.model=SimpleNamespace(config=SimpleNamespace(validate=lambda:None),
            select_poe_intent=lambda *a:PoeIntentChoice(candidate_ref='selected',reason='Closest intent',confidence='high'),
            compare_product_batch=compare)
        temp=TemporaryDirectory();self.addCleanup(temp.cleanup)
        repository=SimpleNamespace(storage_root=Path(temp.name),get_run=lambda *a:SimpleNamespace(input=self.product))
        profiles=SimpleNamespace(get=lambda *a:SimpleNamespace(status='confirmed',profile=profile))
        downloader=SimpleNamespace(intents=lambda keyword:[PoeIntentCandidate(ref='selected',name='Storage')],download=download)
        service=CompetitorService(repository,SimpleNamespace(selection=lambda *a:self.core),SimpleNamespace(fetch_many=fetch_many),
            profiles=profiles,client=self.model,poe_downloader=downloader)
        service._latest=lambda run:self.previous
        service._shared_product_snapshots=lambda run:{}
        service._cache_product_snapshot=lambda *a:None
        service._write=lambda result,version:result
        return service

    def test_550_unique_raw_candidates_filter_prices_before_fetch_and_keep_relevance_rule(self):
        service=self.make_service({0:'85',300:'115',501:'100',502:'84.99',503:'115.01'},low_scores={asin(501)})
        result=service.download_poe_all(self.project,self.run,0)
        self.assertEqual(self.fetched,[asin(0),asin(300),asin(501)])
        self.assertEqual([i.asin for i in result.items],[asin(0),asin(300)])
        self.assertEqual([i.price for i in result.items],[Decimal('85'),Decimal('115')])
        self.assertTrue(all(i.price_basis=='average_30d' and i.decision=='include' for i in result.items))
        self.assertEqual(len(result.poe_downloads),2)
        self.assertIsNotNone(result.confirmed_at)

    def test_more_than_100_qualified_candidates_still_analyse_all_then_keep_top_100(self):
        service=self.make_service({i:'100' for i in range(105)})
        result=service.download_poe_all(self.project,self.run,0)
        self.assertEqual(len(self.fetched),105)
        self.assertEqual(sum(len(batch) for batch in self.compared),105)
        self.assertTrue(all(len(batch)<=10 for batch in self.compared))
        self.assertEqual([i.asin for i in result.items],[asin(i) for i in range(100)])
        self.assertTrue(any('105' in warning and '100' in warning for warning in result.warnings))

    def test_manual_workspace_still_rejects_more_than_500_candidates(self):
        service=self.make_service({})
        self.previous=Competitors(id=uuid4(),project_id=self.project,run_id=self.run,core_selection_id=self.core_id,
            core_keywords=['rack','stand'],version=1,created_at=datetime.now(UTC),items=[Competitor(asin=asin(i)) for i in range(500)])
        with self.assertRaises(DomainError) as caught:
            service.discover(self.project,self.run,DiscoveryInput(expected_version=1,source='manual',asins=[asin(600)]))
        self.assertEqual(caught.exception.code,'COMPETITOR_LIMIT')


if __name__=='__main__':
    unittest.main()
