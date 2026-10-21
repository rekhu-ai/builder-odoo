from odoo.tests import TransactionCase, tagged

from ..tools import registry as tools


@tagged('post_install', '-at_install', 'rekhu_builder')
class TestTools(TransactionCase):
    """The tools as the platform calls them, through registry.run, here inside the test's transaction."""

    def setUp(self):
        super().setUp()
        self.params = self.env['ir.config_parameter'].sudo()
        self.params.set_param('rekhu_builder.environment', 'local')

    def call(self, tool, given=None):
        return tools.run(self.env.registry, tool, given or {}, {'run': 'test-run'}, env=self.env)

    def test_every_tool_has_a_definition(self):
        definitions = tools.definitions()
        self.assertEqual(len(definitions), len(tools.tools()))
        for d in definitions:
            self.assertTrue(d['slug'] and d['description'] and d['input'].get('type') == 'object', d['slug'])

    def test_context_reads_the_database(self):
        out = self.call('context')
        self.assertEqual(out['database'], self.env.cr.dbname)
        self.assertEqual(out['environment'], 'local')
        self.assertIn('addons_paths', out)

    def test_models_reads_a_model(self):
        out = self.call('models', {'model': 'res.partner'})
        self.assertEqual(out['table'], 'res_partner')
        self.assertIn('name', [f['name'] for f in out['fields']])
        self.assertIn('base', [c['module'] for c in out['defined_in']])

    def test_models_of_a_module(self):
        out = self.call('models', {'module': 'rekhu_builder'})
        self.assertIn('rekhu.builder.seen', [m['model'] for m in out['models']])

    def test_personal_values_are_masked(self):
        self.env['res.partner'].create({'name': 'Masked Person', 'email': 'person@example.com'})
        out = self.call('records', {'model': 'res.partner', 'domain': [['name', '=', 'Masked Person']], 'fields': ['name', 'phone']})
        self.assertEqual(out['records'][0]['name'], 'Masked Person')
        # sql-read masks by column name, whatever the query
        out = self.call('sql-read', {'sql': "SELECT 'person@example.com' AS email, 'kept' AS note"})
        self.assertNotIn('person@example.com', str(out))
        self.assertEqual(out['rows'][0][1], 'kept')

    def test_sql_read_does_not_write(self):
        self.assertIn('error', self.call('sql-read', {'sql': 'DELETE FROM res_partner'}))
        self.assertIn('error', self.call('sql-read', {'sql': 'SELECT 1; DELETE FROM res_partner'}))
        # a write hidden in a WITH: the read-only transaction refuses it (sql-read has a cursor of its own)
        out = tools.run(self.env.registry, 'sql-read', {'sql': 'WITH x AS (DELETE FROM res_partner RETURNING id) SELECT * FROM x'}, {'run': 'test-run'})
        self.assertIn('read-only', out.get('error', ''))

    def test_unknown_tool(self):
        self.assertIn('no tool', self.call('nope')['error'])

    def test_production_reads_only(self):
        self.params.set_param('rekhu_builder.environment', 'production')
        self.assertIn('writing is off', self.call('code-write', {'path': 'some_module/x.py', 'content': 'x'})['error'])
        self.assertIn('writing is off', self.call('module-upgrade', {'module': 'rekhu_builder'})['error'])
        self.assertNotIn('error', self.call('context'))

    def test_staging_asks_first(self):
        self.params.set_param('rekhu_builder.environment', 'staging')
        self.assertIn('confirmation_needed', self.call('module-upgrade', {'module': 'base'}))
        self.assertIn('confirmation_needed', self.call('assistant', {'action': 'rules', 'rules': {'res.partner': 'read'}}))

    def test_its_own_modules_are_not_upgraded(self):
        self.assertIn("Rekhu's own module", self.call('module-upgrade', {'module': 'rekhu_builder'})['error'])
