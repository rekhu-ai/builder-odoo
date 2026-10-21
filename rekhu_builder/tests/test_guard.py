from odoo.tests import TransactionCase, tagged

from ..lib import guard


@tagged('post_install', '-at_install', 'rekhu_builder')
class TestGuard(TransactionCase):

    def test_paths_stay_inside_modules(self):
        for path in ('', '../etc/passwd', 'base/../../etc/passwd', 'Not A Module/x.py', '/etc/passwd'):
            with self.assertRaises(guard.Refusal):
                guard.readable(path)

    def test_private_files_are_never_read(self):
        for path in ('base/.git/config', 'base/odoo.conf', 'base/key.pem', 'base/__pycache__/x.pyc', 'base/.env'):
            with self.assertRaises(guard.Refusal):
                guard.split(path)

    def test_a_module_file_is_read(self):
        self.assertTrue(guard.readable('base/__manifest__.py').endswith('base/__manifest__.py'))

    def test_odoo_and_its_own_modules_are_never_written(self):
        with self.assertRaisesRegex(guard.Refusal, 'does not write'):
            guard.writable(self.env, 'base/models/res_partner.py')
        with self.assertRaisesRegex(guard.Refusal, "Rekhu's own module"):
            guard.writable(self.env, 'rekhu_builder/models/x.py')

    def test_odoo_addons_are_recognised(self):
        base = guard.module_root('base')
        self.assertEqual(guard.kind_of(base.rsplit('/', 1)[0]), 'odoo')

    def test_secrets_are_masked(self):
        out = guard.redact({'db_password': 'x', 'url': 'postgres://u:p@host/db', 'name': 'kept'})
        self.assertEqual(out['db_password'], '•••• (set)')
        self.assertEqual(out['url'], 'postgres://••••@host/db')
        self.assertEqual(out['name'], 'kept')
