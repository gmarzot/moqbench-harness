import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
import config  # noqa: E402


def nodes(*specs):
    return [dict(name=n, instance='c7gn.12xlarge', roles=r, **({'vcpus': 48} if 'relay' in r else {})) for n, r in specs]


def deployment(**over):
    data = dict(provider='aws', region='r', nodes=nodes(('relay', ['relay']), ('load-1', ['subscriber']),
                                                        ('load-2', ['subscriber', 'publisher'])))
    return config.Deployment('d', {**data, **over})


def scenario_points(path):
    """A sweep plan's points, each with the environment in force at that line."""
    env, out = {}, {}
    for line in path.read_text().splitlines():
        line = line.split('#')[0].strip()
        if not line:
            continue
        if '=' in line:
            key, value = line.split('=', 1)
            env[key] = value
        else:
            tool, build, shape, per_box, seconds, run = line.split()
            out[run] = dict(tool=tool, build=build, shape=shape, max_per_box=int(per_box), seconds=int(seconds), env=dict(env))
    return out


class Deployments(unittest.TestCase):
    def test_roles_become_relay_load_placement(self):
        d = deployment()
        self.assertEqual(d.pub_host(), 'client:2')
        self.assertEqual(d.sub_hosts(), 'clients')
        self.assertEqual(d.sub_hosts(1), 'client:1')
        with self.assertRaises(config.ConfigError):
            d.sub_hosts(3)

    def test_refusals(self):
        cases = dict(
            provider=dict(provider='gcp'),
            no_nodes=dict(nodes=[]),
            two_relays=dict(nodes=nodes(('a', ['relay']), ('b', ['relay']), ('c', ['subscriber']))),
            no_subscriber=dict(nodes=nodes(('a', ['relay']))),
            bad_role=dict(nodes=nodes(('a', ['relay']), ('b', ['subscribers']))),
            same_name=dict(nodes=nodes(('a', ['relay']), ('a', ['subscriber']))),
            typo=dict(regoin='x'),
        )
        for name, over in cases.items():
            with self.subTest(name), self.assertRaises(config.ConfigError):
                deployment(**over)

    def test_tfvars_carry_the_nodes_and_the_account(self):
        d = config.Deployment('d', dict(provider='aws', region='r', zone='z', nodes=deployment().nodes), dict(owner='me'))
        v = d.tfvars()
        self.assertEqual((v['relay_instance_type'], v['client_count'], v['availability_zone'], v['owner']),
                         ('c7gn.12xlarge', 2, 'z', 'me'))
        self.assertIn('client_count = 2', config.render_tfvars(d))

    def test_oci_uses_its_own_variable_names(self):
        n = [dict(name='r', instance='VM.A', roles=['relay'], ocpus=8, memory_gb=16),
             dict(name='l', instance='VM.A', roles=['subscriber', 'publisher'], ocpus=4)]
        v = config.Deployment('d', dict(provider='oci', nodes=n)).tfvars()
        self.assertEqual((v['relay_shape'], v['relay_ocpus'], v['relay_memory_gb'], v['client_shape'], v['client_ocpus']),
                         ('VM.A', 8, 16, 'VM.A', 4))

    def test_network_type_reaches_oci_only(self):
        n = [dict(name='r', instance='VM.Standard.A2.Flex', roles=['relay', 'subscriber', 'publisher'])]
        self.assertEqual(config.Deployment('d', dict(provider='oci', network_type='VFIO', nodes=n)).tfvars()['network_type'], 'VFIO')
        with self.assertRaises(config.ConfigError):
            config.Deployment('d', dict(provider='aws', network_type='VFIO', nodes=n))

    def test_tfvars_refuse_to_replace_a_file_written_by_hand(self):
        with tempfile.TemporaryDirectory() as d:
            envs = Path(d) / 'aws/envs'
            envs.mkdir(parents=True)
            config.write_tfvars(deployment(), d)
            config.write_tfvars(deployment(), d)
            (envs / 'd.local.tfvars').write_text('profile = "mine"\n')
            with self.assertRaises(config.ConfigError):
                config.write_tfvars(deployment(), d)


class Suites(unittest.TestCase):
    @staticmethod
    def spec(**over):
        return dict(dict(type='capacity', tool='moqperf', video='1080p', max_per_box=10, seconds=10), **over)

    def test_refusals(self):
        for over in (dict(type='soak'), dict(tool='iperf'), dict(video='8k'), dict(max_per_box=0), dict(seconds='x'),
                     dict(typo=1), dict(type='ceiling', tool='aiomoqt')):
            with self.subTest(over), self.assertRaises(config.ConfigError):
                config.Suite('s', dict(test=[self.spec(**over)]))
        with self.assertRaises(config.ConfigError):
            config.Suite('s', dict(test=[]))


class Plans(unittest.TestCase):
    def test_this_series_matches_its_sweep_plan(self):
        plan = config.load_plan('moqx-c7gn12x')
        points = {p['run']: p for s in plan.suites for p in plan.points(s)}
        want = scenario_points(ROOT / 'scenarios/sweep-c7gn12x.plan')
        self.assertEqual(sorted(points), sorted(want))
        for run, expect in want.items():
            with self.subTest(run):
                got = points[run]
                for key in ('tool', 'build', 'shape', 'max_per_box', 'seconds'):
                    self.assertEqual(got[key], expect[key])
                for var, value in expect['env'].items():
                    if not (var.startswith('MP_') and got['tool'] == 'aio'):
                        self.assertEqual(str(got['env'][var][0]), value, var)

    def test_settings_name_their_source(self):
        plan = config.load_plan('moqx-c7gn12x')
        env = next(p for p in plan.points(plan.suites[1]) if p['tool'] == 'mp')['env']
        self.assertEqual(env['STEP_SUBS'], (50, 'test'))
        self.assertEqual(env['DRAFT'], (18, 'default'))
        self.assertEqual(env['THREADS'], (48, 'relay vCPUs'))
        self.assertEqual(env['PUB_HOST'], ('client:2', 'deployment'))

    def test_sweep_text_is_a_plan_sweep_reads(self):
        plan = config.load_plan('moqx-c7gn12x')
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'x.plan'
            path.write_text(plan.sweep_text(plan.suites[0]))
            self.assertEqual(set(scenario_points(path)), {p['run'] for p in plan.points(plan.suites[0])})

    def in_tree(self, deployment_text, suite_text, plan_text):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        for folder, text in (('deployments', deployment_text), ('suites', suite_text), ('plans', plan_text)):
            (Path(d) / folder).mkdir()
            (Path(d) / folder / 'x.toml').write_text(text)
        return d

    DEPLOYMENT = ('provider = "aws"\n[[nodes]]\nname = "r"\ninstance = "i"\nvcpus = 8\nroles = ["relay"]\n'
                  '[[nodes]]\nname = "l"\ninstance = "i"\nroles = ["subscriber", "publisher"]\n')
    SUITE = ('requires = { subscriber = %d }\n[[test]]\ntype = "capacity"\ntool = "%s"\nvideo = "1080p"\n'
             'max_per_box = 10\nseconds = 10\n')

    def test_a_suite_is_refused_on_a_deployment_without_its_roles(self):
        tree = self.in_tree(self.DEPLOYMENT, self.SUITE % (2, 'moqperf'), 'deployment = "x"\nsuites = ["x"]\n')
        with self.assertRaisesRegex(config.ConfigError, 'needs 2 subscriber'):
            config.load_plan('x', tree)

    def test_a_relay_other_than_moqx_is_refused(self):
        tree = self.in_tree(self.DEPLOYMENT, self.SUITE % (1, 'moqperf'),
                            'deployment = "x"\nsuites = ["x"]\nrelays = ["cf:src"]\n')
        with self.assertRaisesRegex(config.ConfigError, 'moqx'):
            config.load_plan('x', tree)

    def test_threads_all_comes_from_the_relay_node(self):
        tree = self.in_tree(self.DEPLOYMENT, self.SUITE % (1, 'moqperf'), 'deployment = "x"\nsuites = ["x"]\n')
        plan = config.load_plan('x', tree)
        self.assertEqual(plan.resolved_threads(), 8)
        self.assertEqual(plan.points(plan.suites[0])[0]['run'], 'mp-pgo8-1b-1080')


class Command(unittest.TestCase):
    def test_missing_builds_names_only_absent_files(self):
        loader = importlib.machinery.SourceFileLoader('moqbench_cli', str(ROOT / 'bin/moqbench'))
        spec = importlib.util.spec_from_loader('moqbench_cli', loader)
        cli = importlib.util.module_from_spec(spec)
        loader.exec_module(cli)
        with tempfile.TemporaryDirectory() as d:
            present = Path(d) / 'moqx.tar.gz'
            present.write_bytes(b'x')
            absent = Path(d) / 'gone.tar.gz'
            plan = lambda path: types.SimpleNamespace(builds={'moqx': str(path)}, relays=['moqx:pgo'])
            self.assertEqual(cli.missing_builds(plan(present)), [])
            self.assertEqual(cli.missing_builds(plan(absent)), [str(absent)])

    def moqbench(self, *args):
        return subprocess.run([str(ROOT / 'bin/moqbench'), *args], capture_output=True, text=True, timeout=60,
                              env={k: v for k, v in os.environ.items() if not k.startswith('AWS')})

    def test_dry_run_prints_the_series_and_changes_nothing(self):
        before = subprocess.check_output(['git', 'status', '--short'], cwd=ROOT)
        run = self.moqbench('run', 'moqx-c7gn12x', '--dry-run', '--no-price')
        self.assertEqual(run.returncode, 0, run.stderr)
        for text in ('6 points', './bin/testbed-provision.sh', 'AIOMOQT_SRC=bd2bc66', 'RPS=0', './bin/sweep.sh',
                     'mp-pgo48-1b-1080-12x', 'THREADS=48  (relay vCPUs)', 'testbed.sh aws-c7gn12x destroy'):
            self.assertIn(text, run.stdout)
        self.assertEqual(subprocess.check_output(['git', 'status', '--short'], cwd=ROOT), before)

    def test_a_config_problem_exits_2_without_a_traceback(self):
        run = self.moqbench('run', 'no-such-plan', '--dry-run')
        self.assertEqual(run.returncode, 2)
        self.assertNotIn('Traceback', run.stderr)

    def test_one_test_dry_run(self):
        run = self.moqbench('test', 'capacity', '--deployment', 'aws-c7gn12x', '--tool', 'moqperf', '--video', '1080p',
                            '--max-per-box', '100', '--seconds', '60', '--dry-run')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('mp-pgo48-2b-1080-12x', run.stdout)


if __name__ == '__main__':
    unittest.main()
