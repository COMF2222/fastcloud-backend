import copy
import unittest
from unittest.mock import patch
import mobile_updates as updates
from media import MediaError

def manifest(version='0.1.1'):
    return {'version':version,'build':1001,'packageId':'work.comf.fastcloud.mobile','sha256':'a'*64,'signatureSha256':'b'*64,'bytes':4000000,'url':f'https://github.com/{updates.REPOSITORY}/releases/download/v{version}/Veirune-Mobile-{version}.apk','notes':{'ru':['Улучшено воспроизведение'],'en':['Improved playback']}}

class UpdateTests(unittest.TestCase):
    def test_manifest_identity_hash_and_build_are_verified(self):
        self.assertEqual(updates.validate_manifest(manifest(),'v0.1.1')['build'],1001)
        for change in ({'build':1000},{'version':'0.1.2'},{'packageId':'attacker.app'},{'url':'https://attacker.example/app.apk'},{'sha256':'abc'},{'bytes':updates.MAX_APK+1}):
            value=manifest();value.update(change)
            self.assertRaises(ValueError,updates.validate_manifest,value,'v0.1.1')
    def test_redirects_cannot_reach_another_repository_or_internal_host(self):
        for url in ('http://github.com/'+updates.REPOSITORY+'/releases/download/v0.1.1/x','https://github.com/attacker/app/releases/download/v0.1.1/x','https://127.0.0.1/private','https://release-assets.githubusercontent.com@attacker.example/x'):
            self.assertRaises(MediaError,updates.checked_url,url)
        updates.checked_url('https://release-assets.githubusercontent.com/public-file')
    def test_public_metadata_exposes_only_its_own_relay_download_path(self):
        with patch.object(updates,'latest_android',return_value=manifest()):
            result=updates.info('android')
            self.assertNotIn('url',result['release'])
            self.assertEqual(result['release']['downloadPath'],'/v1/mobile/update/apk/0.1.1')
    def test_empty_repository_does_not_advertise_a_nonexistent_update(self):
        with patch.object(updates,'latest_android',return_value=None):
            self.assertEqual(updates.info('android'),{'supported':True,'release':None})
    def test_iphone_has_no_apk_fallback_or_unconfigured_store_link(self):
        with patch.dict(updates.os.environ,{'FASTCLOUD_IOS_STORE_URL':'https://attacker.example/download.ipa'}):
            self.assertEqual(updates.info('ios'),{'supported':False,'release':None})
    def test_version_ordering_and_noncanonical_aliases(self):
        self.assertGreater(updates.version_build('0.10.0'),updates.version_build('0.9.99'))
        for version in ('0.01.1','0.1.1-a','../secret',None):self.assertRaises(ValueError,updates.version_build,version)

if __name__=='__main__':unittest.main()
