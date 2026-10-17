import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch


class AudioRouteTests(unittest.TestCase):
    def load_routes(self):
        path = Path(__file__).resolve().parents[1]/'deploy'/'audio'/'ensure-routes.py'
        self.assertTrue(path.exists(), 'idempotent session routing is missing')
        spec = importlib.util.spec_from_file_location('routes', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_repeated_setup_does_not_duplicate_nullsink_or_loopback(self):
        routes = self.load_routes()
        sinks = [{'name':'analog'}]
        modules = []
        loaded = []
        def run(argv, **kw):
            self.assertLessEqual(kw['timeout'],2)
            if argv == ['pactl','--format=json','list','sinks']:
                data = sinks
            elif argv == ['pactl','--format=json','list','modules']:
                data = modules
            elif argv[0:2] == ['pactl','load-module']:
                name, args = argv[2], argv[3:]
                loaded.append(name)
                modules.append({'name':name,'argument':' '.join(args)})
                if name == 'module-null-sink':
                    sinks.append({'name':'lacasa_spotify'})
                return subprocess.CompletedProcess(argv,0,'9','')
            else:
                self.fail(f'forbidden routing command {argv}')
            return subprocess.CompletedProcess(argv,0,json.dumps(data),'')
        with patch.object(routes.subprocess, 'run', side_effect=run):
            routes.ensure_routes('analog')
            routes.ensure_routes('analog')
        self.assertEqual(loaded, ['module-null-sink','module-loopback'])

    def test_missing_analog_fails_before_loading_or_changing_anything(self):
        routes = self.load_routes()
        with patch.object(routes.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'[]','')):
            with self.assertRaisesRegex(RuntimeError,'analog'):
                routes.ensure_routes('analog')


if __name__ == '__main__':
    unittest.main()
