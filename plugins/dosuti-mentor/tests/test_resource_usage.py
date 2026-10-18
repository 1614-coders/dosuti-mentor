"""Resource math, failure isolation, live persistence, report and cleanup contracts."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import resource_usage as ru
import pilot
import pilot_activity as activity
import pilot_cleanup as cleanup
import managed_storage as storage


class FakeProbe:
    def __init__(self): self.calls=0
    def sample(self):
        self.calls+=1
        return dict(processes={(10,100):(100*self.calls,float(self.calls)),
                               (11,200):(50,float(self.calls)*2)},missing=0,
                    system_used=1000+self.calls,system_total=16000,
                    system_cpu=(10*self.calls,100*self.calls))


class Resources(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
    def monitor(self,probe=FakeProbe,publish=lambda m:None):
        return ru.Monitor(self.root,publish,interval=.02,probe_factory=probe)
    def test_delta_cpu_and_sampled_ram(self):
        monitor=self.monitor()
        with patch.object(ru.time,'monotonic',side_effect=[10,12]):
            monitor.sample();monitor.sample()
        self.assertEqual(monitor.data['rss_sampled_peak_bytes'],250)
        self.assertEqual(monitor.data['cpu_average_cores'],1.5)
        self.assertEqual(monitor.data['system_cpu_sampled_peak_percent'],90)
    def test_pid_reuse_does_not_create_false_cpu_spike(self):
        monitor=self.monitor();monitor.sample()
        new=FakeProbe().sample();new['processes']={(10,999):(10,9000)}
        with patch.object(monitor.probe,'sample',return_value=new):monitor.sample()
        self.assertEqual(monitor.data['cpu_average_cores'],0)
    def test_missing_process_is_reported(self):
        monitor=self.monitor();sample=FakeProbe().sample();sample['missing']=2
        with patch.object(monitor.probe,'sample',return_value=sample):monitor.sample()
        self.assertEqual(monitor.data['missing_process_samples'],2)
    def test_unavailable_is_not_zero_ram(self):
        def unavailable():raise OSError('unsupported')
        with self.monitor(unavailable) as monitor: pass
        self.assertNotIn('rss_sampled_peak_bytes',monitor.data)
        self.assertEqual(monitor.data['platform_counters'],'unavailable')
        self.assertIn('нет замера',' '.join(ru.markdown(monitor.data)))
    def test_exception_stops_sampler_and_preserves_original_error(self):
        records=[];monitor=self.monitor(publish=records.append)
        with self.assertRaisesRegex(ValueError,'lesson failure'):
            with monitor:
                time.sleep(.05);raise ValueError('lesson failure')
        self.assertFalse(monitor.thread.is_alive())
        self.assertEqual(records[-1]['state'],'interrupted')
        self.assertGreaterEqual(records[-1]['samples'],2)
    def test_measurement_and_publish_failure_do_not_fail_lesson(self):
        def fail(value):raise OSError('journal busy')
        monitor=self.monitor(publish=fail)
        with patch.object(monitor.probe,'sample',side_effect=OSError('access denied')):
            with monitor:pass
        self.assertGreater(monitor.data['errors'],0)
    def test_nested_monitor_reuses_parent_and_restores_environment(self):
        with patch.dict(os.environ,{},clear=True),patch.object(ru,'Monitor') as factory:
            with ru.measure(self.root,lambda m:None):
                with ru.measure(self.root,lambda m:None):pass
                self.assertEqual(os.environ['DOSUTI_RESOURCE_MONITOR'],'1')
            self.assertNotIn('DOSUTI_RESOURCE_MONITOR',os.environ)
            self.assertEqual(factory.call_count,1)
    def test_folder_size_bounded_and_links_excluded(self):
        folder=self.root/'data';folder.mkdir();(folder/'a').write_bytes(b'x'*7)
        external=self.root/'outside';external.write_bytes(b'x'*50)
        (folder/'link').symlink_to(external)
        size=ru.folder_size(folder)
        self.assertEqual(size['bytes'],7);self.assertEqual(size['links_skipped'],1)
        self.assertTrue(size['complete']);self.assertFalse(ru.folder_size(folder,limit=0)['complete'])
    def test_resources_written_while_running_then_exported(self):
        root=self.root/'pilot';pilot.start(root)
        # Real monitor persists real disk observations; unsupported counters stay absent on Mac.
        with activity.span('preparation',root):
            item=activity.read(root)['activities'][0]
            self.assertEqual(item['resources']['state'],'running')
            self.assertEqual(item['status'],'running')
        item=activity.read(root)['activities'][0]
        self.assertEqual(item['resources']['state'],'finished')
        report=Path(pilot.export(root)['report']).read_text()
        self.assertIn('Ресурсы компьютера',report)
        self.assertIn('Свободно на диске',report)
        self.assertEqual(item['status'],'passed')
    def test_metrics_survive_export_and_owned_cleanup_only(self):
        root=self.root/'pilot';pilot.start(root)
        with activity.span('preparation',root):pass
        external=self.root/'shared-python';external.mkdir();(external/'keep').write_text('shared')
        plan=self.root/'cleanup.json';cleanup.plan(root,plan)
        result=cleanup.apply(root,plan)
        self.assertFalse(root.exists());self.assertTrue((external/'keep').exists())
        self.assertIn('Ресурсы компьютера',Path(result['report']).read_text())
        self.assertFalse(result['plugin_removed']);self.assertFalse(result['chat_history_removed'])
    def test_auto_cleanup_keeps_source_models_and_reports(self):
        root=storage.initialize(self.root/'home')
        with storage.session(root):
            for name in ['models/keep','reports/keep','course/keep','cache/disposable']:
                p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'abc')
        storage.maintain(root,apply=True,max_cache_bytes=0)
        for name in ['models/keep','reports/keep','course/keep']:self.assertTrue((root/name).exists())
        self.assertFalse((root/'cache/disposable').exists())
        self.assertEqual(list((root/'temp').iterdir()),[])

    @unittest.skipUnless(os.name=='nt','Native Win32 counters require Windows')
    def test_native_windows_counters_and_live_child(self):
        import subprocess
        probe=ru.WindowsProbe()
        self.assertEqual(ru.c.sizeof(probe.Entry),568 if ru.c.sizeof(ru.c.c_void_p)==8 else 556)
        child=subprocess.Popen([sys.executable,'-c','import time; a=bytearray(16000000); print("ready",flush=True); time.sleep(30)'],stdout=subprocess.PIPE,text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(),'ready')
            result=probe.sample()
            self.assertGreater(result['system_total'],0)
            self.assertTrue(any(pid==child.pid for pid,created in result['processes']))
            self.assertTrue(any(pid==os.getpid() for pid,created in result['processes']))
            self.assertGreater(sum(rss for rss,cpu in result['processes'].values()),0)
        finally:
            child.terminate();child.wait(timeout=10);child.stdout.close()

    def test_managed_scope_includes_models_outside_run(self):
        home=storage.initialize(self.root/'home');run=home/'run';run.mkdir()
        with patch.dict(os.environ,{},clear=True),patch.object(ru,'Monitor') as monitor:
            with ru.measure(run,lambda m:None):pass
        self.assertEqual(monitor.call_args.args[0],home)


if __name__=='__main__':unittest.main()
