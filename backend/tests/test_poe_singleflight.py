import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4
from app.services.competitors import CompetitorService
from app.domain.errors import DomainError

class CollectionStopped(Exception): pass

class SingleFlightTests(unittest.TestCase):
    def setUp(self):
        repo=MagicMock();repo.storage_root=Path('/tmp/singleflight-test')
        self.service=CompetitorService(repo,MagicMock(),MagicMock())
        self.project,self.run=uuid4(),uuid4()
        self.entered=threading.Event();self.release=threading.Event()
        self.errors=[]
        self.calls=0
        def core(project,run):
            self.calls+=1
            if self.calls==1:
                self.entered.set()
                if not self.release.wait(5): raise AssertionError('test timed out')
            raise CollectionStopped()
        self.service._core=core
    def first(self):
        try:self.service.download_poe_all(self.project,self.run,0)
        except Exception as e:self.errors.append(e)
    def test_duplicate_is_rejected_before_browser_or_database_work(self):
        worker=threading.Thread(target=self.first);worker.start()
        try:
            self.assertTrue(self.entered.wait(2))
            with self.assertRaises(DomainError) as caught:
                self.service.download_poe_all(self.project,self.run,0,['keyword'])
            self.assertEqual(caught.exception.code,'COMPETITOR_COLLECTION_RUNNING')
            self.assertEqual(self.calls,1)
        finally:self.release.set();worker.join(5)
        self.assertIsInstance(self.errors[0],CollectionStopped)
        # A failed collection must release its reservation.
        with self.assertRaises(CollectionStopped):
            self.service.download_poe_all(self.project,self.run,0)
    def test_success_releases_reservation_and_preserves_inputs(self):
        result=object()
        delegate=MagicMock(return_value=result)
        self.service._download_poe_all=delegate
        for _ in range(2):
            self.assertIs(self.service.download_poe_all(self.project,self.run,7,['keyword']),result)
        self.assertEqual(delegate.call_count,2)
        delegate.assert_called_with(self.project,self.run,7,['keyword'])
    def test_other_run_is_not_blocked(self):
        worker=threading.Thread(target=self.first);worker.start()
        try:
            self.assertTrue(self.entered.wait(2))
            with self.assertRaises(CollectionStopped):
                self.service.download_poe_all(self.project,uuid4(),0)
        finally:self.release.set();worker.join(5)

if __name__=='__main__':unittest.main()
