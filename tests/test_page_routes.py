"""Localized deep links and release-safe static cache regression tests."""
from pathlib import Path
import hashlib,os,re,shutil,tempfile,unittest
from unittest.mock import patch
import app as core
import webapp

class PageRoutesTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'static').mkdir()
        for name in ('index.html','app.js','locale.js','theme.js','style.css'):
            shutil.copyfile(core.ROOT/'static'/name,self.root/'static'/name)
        self.addCleanup(patch.stopall)
        patch.object(core,'ROOT',self.root).start();patch.object(core,'DB_PATH',self.root/'test.sqlite3').start();patch.object(core,'_DATABASE_URL',None).start()
        self.web=webapp.create_app({'TESTING':True});self.client=self.web.test_client()

    def test_localized_deep_links_use_html_language_and_keep_api_authenticated(self):
        for language,pages in webapp.PAGE_ROUTES.items():
            for slug in pages:
                result=self.client.get('/'+language+'/'+slug)
                self.assertEqual(result.status_code,200);self.assertIn('<html lang="'+language+'">',result.text)
                self.assertEqual(result.headers['Cache-Control'],'no-store')
        for url in ('/tr/unknown','/en/genel-bakis','/ru/overview'):
            self.assertEqual(self.client.get(url).status_code,404)
        self.assertEqual(self.client.get('/tr/').headers['Location'],'/tr/genel-bakis')
        self.assertEqual(self.client.get('/en/').headers['Location'],'/en/overview')
        self.assertEqual(self.client.get('/api/profile').status_code,401)

    def test_html_always_loads_content_versioned_assets_despite_old_conditional_headers(self):
        result=self.client.get('/',headers={'If-Modified-Since':'Sat, 20 Oct 2018 01:46:40 GMT','If-None-Match':'"old"'})
        self.assertEqual(result.status_code,200)
        self.assertNotIn('Last-Modified',result.headers)
        for name in ('app.js','locale.js','theme.js','style.css'):
            digest=hashlib.sha256((self.root/'static'/name).read_bytes()).hexdigest()[:16]
            self.assertIn('/static/'+name+'?v='+digest,result.text)

    def test_same_size_same_artifact_mtime_does_not_reuse_an_old_javascript_response(self):
        path=self.root/'static'/'app.js';path.write_bytes(b'old-release');os.utime(path,(1540000000,1540000000))
        old_client=webapp.create_app({'TESTING':True}).test_client();old=old_client.get('/static/app.js');self.addCleanup(old.close)
        path.write_bytes(b'new-release');os.utime(path,(1540000000,1540000000))
        new_client=webapp.create_app({'TESTING':True}).test_client()
        changed=new_client.get('/static/app.js',headers={'If-None-Match':old.headers['ETag'],'If-Modified-Since':'Sat, 20 Oct 2018 01:46:40 GMT'})
        self.addCleanup(changed.close)
        self.assertEqual(changed.status_code,200);self.assertEqual(changed.data,b'new-release')
        self.assertNotEqual(changed.headers['ETag'],old.headers['ETag']);self.assertNotIn('Last-Modified',changed.headers)
        cached=new_client.get('/static/app.js',headers={'If-None-Match':changed.headers['ETag']});self.addCleanup(cached.close);self.assertEqual(cached.status_code,304)
