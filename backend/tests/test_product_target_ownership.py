"""Independent product targets must overlap and never reuse/close a user's tab."""
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.domain.errors import DomainError
from app.services import amazon_page as module


class FakePage:
    def __init__(self, target_id, url):
        self.target_id = target_id
        self.url = url
        self.closed = False

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, target_id):
        self.target_id = target_id

    def send(self, method, params=None):
        if method == 'Target.getTargetInfo':
            return {'targetInfo': {'targetId': self.target_id}}
        return {}

    def detach(self):
        pass


class TargetOwnershipTests(unittest.TestCase):
    def playwright(self, pages):
        context = SimpleNamespace(pages=pages, new_cdp_session=lambda p: FakeSession(p.target_id))
        browser = SimpleNamespace(contexts=[context], new_browser_cdp_session=lambda: FakeSession('browser'))
        value = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=lambda *a, **kw: browser))
        class Manager:
            def __enter__(self):
                return value
            def __exit__(self, *exc):
                return False
        return Manager()

    def test_same_url_user_tab_is_never_used_instead_of_owned_target(self):
        url = 'https://www.amazon.com/dp/B000000001'
        user = FakePage('user-target', url)
        owned = FakePage('owned-target', url)
        closed = []
        with patch.object(module, 'ensure_port', return_value=9222), \
             patch.object(module, '_open_target', return_value='owned-target'), \
             patch.object(module, '_close_target', side_effect=lambda p,t: closed.append(t)), \
             patch('playwright.sync_api.sync_playwright', return_value=self.playwright([user, owned])):
            with module.amazon_product_page(Path('.'), url) as page:
                self.assertIs(page, owned)
        self.assertFalse(user.closed)
        self.assertTrue(owned.closed)
        self.assertEqual(closed, ['owned-target'])

    def test_owned_target_closed_even_when_context_selection_fails(self):
        closed = []
        user = FakePage('user-target', 'https://www.amazon.com/')
        with patch.object(module, 'ensure_port', return_value=9222), \
             patch.object(module, '_open_target', return_value='owned-target'), \
             patch.object(module, '_close_target', side_effect=lambda p,t: closed.append(t)), \
             patch('playwright.sync_api.sync_playwright', return_value=self.playwright([user])), \
             patch.object(module, '_reusable_context', side_effect=DomainError(503,'TEST_CONTEXT','unavailable')):
            with self.assertRaises(DomainError):
                with module.amazon_product_page(Path('.'), 'https://www.amazon.com/dp/B000000001'):
                    self.fail('Context selection should fail')
        self.assertEqual(closed, ['owned-target'])
        self.assertFalse(user.closed)

    def test_product_pages_really_overlap_without_a_global_read_lock(self):
        barrier = threading.Barrier(3)
        lock = threading.Lock()
        active = peak = 0
        pages = []
        errors = []
        def factory():
            page = FakePage(str(threading.get_ident()), 'https://www.amazon.com/dp/B000000001')
            pages.append(page)
            return self.playwright([page])
        def read():
            nonlocal active, peak
            try:
                with module.amazon_product_page(Path('.'), 'https://www.amazon.com/dp/B000000001'):
                    with lock:
                        active += 1
                        peak = max(peak,active)
                    try:
                        barrier.wait(timeout=2)
                    finally:
                        with lock:
                            active -= 1
            except Exception as error:
                errors.append(error)
        with patch.object(module, 'ensure_port', return_value=9222), \
             patch.object(module, '_open_target', side_effect=lambda *a: str(threading.get_ident())), \
             patch.object(module, '_close_target'), \
             patch('playwright.sync_api.sync_playwright', side_effect=factory):
            with ThreadPoolExecutor(max_workers=3) as pool:
                list(pool.map(lambda _: read(), range(3)))
        self.assertEqual(errors, [])
        self.assertEqual(peak, 3)
        self.assertTrue(all(p.closed for p in pages))


if __name__ == '__main__':
    unittest.main()
