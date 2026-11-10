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
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
import config  # noqa: E402

os.environ.pop('MOQBENCH_CONFIG', None)


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

    def test_oci_picks_the_image_for_its_arch(self):
        n = [dict(name='r', instance='VM.A', roles=['relay', 'subscriber', 'publisher'])]
        account = dict(image_ocid='arm-image', image_ocid_amd64='x86-image')
        x86 = config.Deployment('d', dict(provider='oci', arch='amd64', nodes=n), account).tfvars()
        arm = config.Deployment('d', dict(provider='oci', arch='arm64', nodes=n), account).tfvars()
        self.assertEqual((x86['image_ocid'], arm['image_ocid']), ('x86-image', 'arm-image'))
        self.assertNotIn('image_ocid_amd64', x86)
        self.assertNotIn('image_ocid_amd64', arm)

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
    def test_settings_name_their_source(self):
        plan = config.load_plan('example-aws')
        aio = plan.points(plan.suites[0])[0]['env']
        mp = plan.points(plan.suites[2])[0]['env']
        self.assertEqual((aio['STEP_SUBS'], mp['STEP_SUBS']), ((50, 'test'), (250, 'default')))
        self.assertEqual(mp['DRAFT'], (18, 'default'))
        self.assertEqual(mp['THREADS'], (8, 'relay vCPUs'))
        self.assertEqual(mp['PUB_HOST'], ('client:2', 'deployment'))
    def test_source_relays_need_a_ref_and_take_their_own_tests(self):
        suite = config.Suite('s', dict(test=[
            dict(type='capacity', tool='aiomoqt', video='720p', max_per_box=10, seconds=60, relay='moqdev'),
            dict(type='capacity', tool='aiomoqt', video='720p', max_per_box=10, seconds=60, relay='cf', step_subs=10),
        ]))
        data = dict(deployment='aws-example', relays=['moqdev:src', 'cf:src'], suites=[suite])
        with self.assertRaises(config.ConfigError):
            config.Plan('p', data)
        plan = config.Plan('p', dict(data, builds=dict(moqdev='abc', moq_rs='def')))
        points = plan.points(suite)
        self.assertEqual([p['run'].split('-')[1] for p in points], ['moqdev8', 'cf8'])
        self.assertEqual([p['env']['RELAY'][0] for p in points], ['moqdev', 'cf'])
        self.assertEqual([p['env']['STEP_SUBS'][0] for p in points], [250, 10])
        self.assertEqual({p['build'] for p in points}, {'src'})

    def test_a_test_can_set_its_own_relay_threads(self):
        test = dict(type='capacity', tool='aiomoqt', video='720p', max_per_box=10, seconds=60)
        suite = config.Suite('s', dict(test=[test, dict(test, threads=3)]))
        plan = config.Plan('p', dict(deployment='aws-example', relays=['cf:src'], suites=[suite], builds=dict(moq_rs='x')))
        points = plan.points(suite)
        self.assertEqual([p['env']['THREADS'] for p in points], [(8, 'relay vCPUs'), (3, 'test')])
        self.assertEqual([p['run'] for p in points], ['aio-cf8-2b-720-awsex', 'aio-cf3-2b-720-awsex'])
    def test_moqx_points_do_not_set_relay(self):
        plan = config.load_plan('example-aws')
        self.assertNotIn('RELAY', plan.points(plan.suites[0])[0]['env'])
    def test_sweep_text_is_a_plan_sweep_reads(self):
        plan = config.load_plan('example-aws')
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'x.plan'
            path.write_text(plan.sweep_text(plan.suites[0]))
            self.assertEqual(set(scenario_points(path)), {p['run'] for p in plan.points(plan.suites[0])})

    def test_descriptions_and_notes_ride_as_comments(self):
        test = dict(type='capacity', tool='aiomoqt', video='720p', max_per_box=10, seconds=60, note='Why this point')
        suite = config.Suite('s', dict(description='The suite', test=[test]))
        plan = config.Plan('p', dict(description='The plan', deployment='aws-example', relays=['moqdev:src'],
                                     suites=[suite], builds=dict(moqdev='x')))
        text = plan.sweep_text(suite)
        self.assertIn('# plan: The plan\n# suite: The suite\n', text)
        self.assertIn('# note: Why this point\naio src 720p', text)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'x.plan'
            path.write_text(text)
            self.assertEqual(set(scenario_points(path)), {p['run'] for p in plan.points(suite)})
    def test_a_note_is_one_line(self):
        test = dict(type='capacity', tool='aiomoqt', video='720p', max_per_box=10, seconds=10, note='a\nb')
        with self.assertRaisesRegex(config.ConfigError, 'one line'):
            config.Suite('s', dict(test=[test]))

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

    def test_a_relay_moqbench_cannot_run_is_refused(self):
        tree = self.in_tree(self.DEPLOYMENT, self.SUITE % (1, 'moqperf'),
                            'deployment = "x"\nsuites = ["x"]\nrelays = ["quinn:src"]\n')
        with self.assertRaisesRegex(config.ConfigError, 'moqx'):
            config.load_plan('x', tree)

    def test_aiomoqt_relay_needs_an_aiomoqt_ref(self):
        tree = self.in_tree(self.DEPLOYMENT, self.SUITE % (1, 'aiomoqt'),
                            'deployment = "x"\nsuites = ["x"]\nrelays = ["aiomoqt:src"]\n')
        with self.assertRaisesRegex(config.ConfigError, 'builds.aiomoqt'):
            config.load_plan('x', tree)

    def test_the_example_plans(self):
        relays = ('pgo8', 'moqdev8', 'aiomoqt8')
        for name, tag in (('example-aws', 'awsex'), ('example-oci', 'ociex')):
            with self.subTest(name):
                plan = config.load_plan(name)
                self.assertEqual([s.name for s in plan.suites], ['example-video', 'example-av', 'example-moqperf'])
                self.assertEqual([p['run'] for s in plan.suites for p in plan.points(s)],
                                 [f'aio-{r}-2b-720-{tag}' for r in relays]
                                 + [f'aio-{r}-2b-720-{tag}-{v}' for r in relays for v in ('av-stream', 'av-dgram')]
                                 + [f'mp-pgo8-2b-720-{tag}'])
                held = plan.points(plan.suites[0])
                self.assertEqual({(p['env']['HOLD'], p['env']['CONFIRM']) for p in held}, {((20, 'test'), (60, 'test'))})
                self.assertEqual(held[2]['env']['RELAY'], ('aiomoqt', 'plan'))
                self.assertNotIn('moqx', plan.builds)
    def test_the_join_rate_settings(self):
        test = dict(type='capacity', tool='aiomoqt', video='720p', max_per_box=800, seconds=600, join_search=5,
                    searcher_subs=400, suffix='join')
        suite = config.Suite('s', dict(test=[test]))
        plan = config.Plan('p', dict(deployment='aws-example', relays=['moqx:pgo'], suites=[suite]))
        env = plan.points(suite)[0]['env']
        self.assertEqual((env['JOIN_SEARCH'], env['SEARCHER_SUBS'], env['HOLD'][0]), ((5, 'test'), (400, 'test'), 0))
    def test_join_search_needs_counts_and_aiomoqt(self):
        base = dict(type='capacity', video='720p', max_per_box=10, seconds=10, tool='aiomoqt')
        for test, why in ((dict(base, join_search=5), 'positive counts'), (dict(base, join_search=0, searcher_subs=50), 'positive counts'),
                          (dict(base, join_search=5, searcher_subs=50, hold_s=20), 'no hold_s')):
            with self.assertRaisesRegex(config.ConfigError, why):
                config.Suite('s', dict(test=[test]))

    def test_hold_needs_seconds_and_the_aiomoqt_tool(self):
        base = dict(type='capacity', video='720p', max_per_box=10, seconds=10)
        for test, why in ((dict(base, tool='aiomoqt', hold_s=-1), 'hold_s must be seconds'),
                          (dict(base, tool='moqperf', type='ceiling', hold_s=20), 'hold_s needs the aiomoqt tool')):
            with self.assertRaisesRegex(config.ConfigError, why):
                config.Suite('s', dict(test=[test]))

    def test_moqx_stack_must_be_known(self):
        test = dict(type='capacity', tool='aiomoqt', video='720p', max_per_box=10, seconds=10, moqx_stack='quiche')
        with self.assertRaisesRegex(config.ConfigError, 'moqx_stack'):
            config.Suite('s', dict(test=[test]))

    def test_moqx_on_picoquic_takes_the_bench_certificate(self):
        def cmd(**env):
            with tempfile.TemporaryDirectory() as d:  # no relays.local.sh
                return subprocess.run(['bash', '-c', f'source {ROOT}/lib/run.sh; relay_cmd moqx 4; echo "${{LABEL[moqx]}}"'], cwd=d,
                                      capture_output=True, text=True, env={'PATH': '/usr/bin:/bin', **env}).stdout
        mvfst, pico = cmd(), cmd(MOQX_STACK='picoquic')
        self.assertIn(' --insecure ', mvfst)
        self.assertIn(' --cc bbr2 ', mvfst)
        self.assertNotIn('--quic-stack', mvfst)
        self.assertTrue(mvfst.endswith('moqx/mvfst\n'))
        self.assertIn(' --quic-stack picoquic --cert $PWD/bench/cert.pem --key $PWD/bench/key.pem ', pico)
        self.assertIn(' --cc bbr ', pico)
        self.assertNotIn('--insecure', pico)
        self.assertTrue(pico.endswith('moqx/picoquic\n'))

    def test_relay_cpus_must_be_a_cpu_list(self):
        test = dict(type='capacity', tool='aiomoqt', video='720p', max_per_box=10, seconds=10, relay_cpus='4 to 7')
        with self.assertRaisesRegex(config.ConfigError, 'relay_cpus'):
            config.Suite('s', dict(test=[test]))

    def test_the_aiomoqt_relay_workers_follow_their_setting(self):
        def cmd(**env):
            with tempfile.TemporaryDirectory() as d:  # no relays.local.sh
                return subprocess.run(['bash', '-c', f'source {ROOT}/lib/run.sh; relay_cmd aiomoqt 1'], cwd=d,
                                      capture_output=True, text=True, env={'PATH': '/usr/bin:/bin', **env}).stdout
        self.assertNotIn('--workers', cmd())
        self.assertIn(' --workers 3', cmd(AIOMOQT_WORKERS='3'))

    def test_threads_all_comes_from_the_relay_node(self):
        tree = self.in_tree(self.DEPLOYMENT, self.SUITE % (1, 'moqperf'), 'deployment = "x"\nsuites = ["x"]\n')
        plan = config.load_plan('x', tree)
        self.assertEqual(plan.resolved_threads(), 8)
        self.assertEqual(plan.points(plan.suites[0])[0]['run'], 'mp-pgo8-1b-1080')


class ConfigPath(unittest.TestCase):
    DEPLOYMENT = Plans.DEPLOYMENT
    SUITE = Plans.SUITE % (1, 'moqperf')

    def folder(self, files=None):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        for name, text in (files or {}).items():
            (d / name).parent.mkdir(parents=True, exist_ok=True)
            (d / name).write_text(text)
        return d

    def using(self, *folders):
        patcher = mock.patch.dict(os.environ, MOQBENCH_CONFIG=os.pathsep.join(map(str, folders)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_plan_in_a_folder_finds_its_deployment_and_suites_in_the_repo(self):
        repo = self.folder({'deployments/x.toml': self.DEPLOYMENT, 'suites/x.toml': self.SUITE})
        mine = self.folder({'plans/mine.toml': 'deployment = "x"\nsuites = ["x"]\n'})
        self.using(mine)
        plan = config.load_plan('mine', repo)
        self.assertEqual((plan.path, plan.deployment.name, plan.suites[0].name), (mine / 'plans/mine.toml', 'x', 'x'))

    def test_the_first_folder_wins(self):
        repo = self.folder({'deployments/x.toml': self.DEPLOYMENT})
        a = self.folder({'deployments/x.toml': self.DEPLOYMENT.replace('vcpus = 8', 'vcpus = 4')})
        b = self.folder({'deployments/x.toml': self.DEPLOYMENT.replace('vcpus = 8', 'vcpus = 2')})
        self.using(a, b)
        self.assertEqual(config.load_deployment('x', repo).relay['vcpus'], 4)

    def test_account_settings_are_found_in_the_folders(self):
        repo = self.folder({'deployments/x.toml': self.DEPLOYMENT})
        mine = self.folder({'deployments/aws.local.toml': '[account]\nprofile = "mine"\n'})
        self.using(mine)
        self.assertEqual(config.load_deployment('x', repo).account, {'profile': 'mine'})

    def test_a_missing_name_says_where_it_looked(self):
        repo, mine = self.folder(), self.folder()
        self.using(mine)
        with self.assertRaisesRegex(config.ConfigError, f'^no plans/nope.toml in {mine}, {repo}$'):
            config.load_plan('nope', repo)

    def test_deployment_names_span_the_folders_without_account_files(self):
        repo = self.folder({'deployments/x.toml': '', 'deployments/aws.local.toml': '', 'deployments/lab.local.toml': ''})
        mine = self.folder({'deployments/y.toml': '', 'deployments/oci.local.toml': ''})
        self.using(mine)
        self.assertEqual(config.deployment_names(repo), ['lab', 'x', 'y'])

    def test_relays_local_sh_in_a_folder_is_sourced_and_the_first_folder_wins(self):
        a = self.folder({'relays.local.sh': 'LABEL[mine]=a\n'})
        b = self.folder({'relays.local.sh': 'LABEL[mine]=b\nLABEL[other]=b\n'})
        with tempfile.TemporaryDirectory() as d:  # no relays.local.sh
            out = subprocess.run(['bash', '-c', f'source {ROOT}/lib/run.sh; echo "${{LABEL[mine]}} ${{LABEL[other]}}"'], cwd=d,
                                 capture_output=True, text=True,
                                 env={'PATH': '/usr/bin:/bin', 'MOQBENCH_CONFIG': f'{a}{os.pathsep}{b}'}).stdout
        self.assertEqual(out, 'a b\n')


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
        run = self.moqbench('run', 'example-aws', '--dry-run', '--no-price')
        self.assertEqual(run.returncode, 0, run.stderr)
        for text in ('10 points', './bin/testbed-provision.sh', 'AIOMOQT_SRC=0.15.0rc1', 'RPS=1', './bin/sweep.sh',
                     'mp-pgo8-2b-720-awsex', 'THREADS=8  (relay vCPUs)', 'testbed.sh aws-example destroy'):
            self.assertIn(text, run.stdout)
        self.assertEqual(subprocess.check_output(['git', 'status', '--short'], cwd=ROOT), before)
    def test_aiomoqt_relay_is_built_by_provisioning(self):
        run = self.moqbench('run', 'example-oci', '--dry-run', '--no-price')
        self.assertEqual(run.returncode, 0, run.stderr)
        for text in ('AIOMOQT_SRC=0.15.0rc1', 'AIOPQUIC_SRC=v0.8.0rc1', 'aio-aiomoqt8-2b-720-ociex', 'relay-build.sh moqdev'):
            self.assertIn(text, run.stdout)
        self.assertNotIn('relay-build.sh aiomoqt', run.stdout)
    def test_a_plan_from_moqbench_config_dry_runs(self):
        with tempfile.TemporaryDirectory() as d:
            for name, text in (('deployments/mine.toml', Plans.DEPLOYMENT.replace('provider = "aws"', 'provider = "aws"\nregion = "r"')),
                               ('suites/mine.toml', Plans.SUITE % (1, 'moqperf')),
                               ('plans/mine.toml', 'deployment = "mine"\nsuites = ["mine"]\n')):
                (Path(d) / name).parent.mkdir(exist_ok=True)
                (Path(d) / name).write_text(text)
            run = subprocess.run([str(ROOT / 'bin/moqbench'), 'run', 'mine', '--dry-run', '--no-price'], capture_output=True,
                                 text=True, timeout=60, env={**os.environ, 'MOQBENCH_CONFIG': d})
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('mp-pgo8-1b-1080', run.stdout)

    def test_a_config_problem_exits_2_without_a_traceback(self):
        run = self.moqbench('run', 'no-such-plan', '--dry-run')
        self.assertEqual(run.returncode, 2)
        self.assertNotIn('Traceback', run.stderr)

    def test_one_test_dry_run(self):
        run = self.moqbench('test', 'capacity', '--deployment', 'aws-example', '--tool', 'moqperf', '--video', '1080p',
                            '--max-per-box', '100', '--seconds', '60', '--dry-run')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('mp-pgo8-2b-1080-awsex', run.stdout)


if __name__ == '__main__':
    unittest.main()
