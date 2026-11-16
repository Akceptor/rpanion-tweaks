"""Exercise independent service polling, edits, and request ordering."""
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'requires Node')
class ServicesUiTest(unittest.TestCase):
    def test_old_poll_cannot_replace_action_result_or_settings_edits(self):
        html = (Path(__file__).resolve().parents[1] / 'python' / 'fly_ground_settings.html').read_text()
        functions = 'async function serviceAction(' + html.split('async function serviceAction(', 1)[1].split('async function api(', 1)[0]
        script = """
const assert = require('node:assert/strict');
let serviceSnapshot = {}, serviceBusy=false, servicePolling=false, serviceGeneration=0, serviceUpdatedAt=Date.now();
const document={hidden:false};
let busy=false, pendingRestart=true, editing=true;
const snapshot={settings:{draft:'unsaved'}};
const status={textContent:''}; const $=()=>status; const controls=()=>{};
let rendered=[]; const renderServices=data=>rendered.push(data);
let pollResolve;
const api=(path,payload)=>payload ? Promise.resolve('action-result') : new Promise(resolve=>pollResolve=resolve);
""" + functions + """
(async()=>{
 const pending = pollServices();
 await serviceAction('bridge','restart');
 pollResolve('stale-poll'); await pending;
 assert.deepEqual(rendered,['action-result']);
 assert.equal(snapshot.settings.draft,'unsaved'); assert.equal(editing,true);
 assert.equal(serviceBusy,false); assert.equal(servicePolling,false);
 assert.equal(pendingRestart,false);
 pendingRestart=true; await serviceAction('bridge','start');
 assert.equal(pendingRestart,true,'start on an active bridge does not apply saved settings');
})().catch(error=>{ console.error(error);process.exit(1); });
"""
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_action_clears_health_and_expired_status_cannot_act(self):
        html = (Path(__file__).resolve().parents[1] / 'python' / 'fly_ground_settings.html').read_text()
        functions = 'async function serviceAction(' + html.split('async function serviceAction(', 1)[1].split('async function api(', 1)[0]
        script = """
const assert=require('node:assert/strict');
let serviceSnapshot={}, serviceBusy=false, servicePolling=false, serviceGeneration=0, serviceUpdatedAt=Date.now();
let busy=false,pendingRestart=false;
const title={textContent:'Bridge — UP'},detail={textContent:'Service is active.'};
const document={hidden:false,querySelectorAll:query=>query.includes('title')?[title]:[detail]};
const $=()=>({textContent:''}),controls=()=>{};
let requests=0;
const api=async()=>{ requests++; throw new Error('Timed out'); };
const renderServices=()=>{};
""" + functions + """
(async()=>{
 await serviceAction('bridge','restart');
 assert.equal(serviceSnapshot,null,'failed action retained health from before unknown outcome');
 assert.match(title.textContent,/UNKNOWN/);
 serviceSnapshot={}; serviceUpdatedAt=Date.now()-16000;
 await serviceAction('bridge','restart');
 assert.equal(requests,1,'stale page executed a service action');
})().catch(error=>{console.error(error);process.exit(1)});
"""
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_service_fetch_timeout_covers_fetch_and_response_body(self):
        html = (Path(__file__).resolve().parents[1] / 'python' / 'fly_ground_settings.html').read_text()
        functions = 'async function api(' + html.split('async function api(', 1)[1].split('async function perform(', 1)[0]
        script = """
const assert=require('node:assert/strict');
let token='csrf-token', timers=[], options;
const setTimeout=(callback,delay)=>{timers.push({callback,delay});return timers.length};
const clearTimeout=()=>{};
let finishBody;
let fetch=async(path,opts)=>{options=opts;return {ok:true,json:()=>new Promise(resolve=>finishBody=resolve)}};
""" + functions + """
(async()=>{
 const pending=api('/api/services').then(()=>{throw new Error('unexpected success')},error=>error);
 await Promise.resolve(); await Promise.resolve();
 assert.equal(timers.length,1,'services request has no timeout');
 assert.equal(timers[0].delay,8000); timers[0].callback();
 assert.match((await pending).message,/timed out/i); assert.equal(options.signal.aborted,true);
 finishBody({late:true}); await Promise.resolve();
 assert.match((await pending).message,/timed out/i,'late response changed a timed-out request');
 timers=[]; fetch=()=>new Promise(()=>{});
 const action=api('/api/services',{service:'bridge',action:'restart'}).catch(error=>error);
 await Promise.resolve();assert.equal(timers[0].delay,65000);timers[0].callback();await action;
 timers=[]; fetch=async()=>({ok:true,json:async()=>({settings:'saved'})});
 assert.deepEqual(await api('/api/settings'),{settings:'saved'});assert.equal(timers.length,0);
})().catch(error=>{console.error(error);process.exit(1)});
"""
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_visibility_reset_rejects_pending_action_result_and_poll_failure_clears_health(self):
        html = (Path(__file__).resolve().parents[1] / 'python' / 'fly_ground_settings.html').read_text()
        functions = 'async function serviceAction(' + html.split('async function serviceAction(', 1)[1].split('async function api(', 1)[0]
        self.assertIn("document.addEventListener('visibilitychange', resumeServices)", html)
        self.assertIn("window.addEventListener('pageshow', resumeServices)", html)
        script = """
const assert=require('node:assert/strict');
let serviceSnapshot={},serviceBusy=false,servicePolling=false,serviceGeneration=0,serviceUpdatedAt=Date.now();
let busy=false,pendingRestart=true,rendered=[];
const title={textContent:'Bridge — UP'}, detail={textContent:'Active'};
const document={hidden:false,querySelectorAll:q=>q.includes('title')?[title]:[detail]};
const $=()=>({textContent:''}),controls=()=>{},renderServices=data=>rendered.push(data);
let resolveAction;
let api=()=>new Promise(resolve=>resolveAction=resolve);
""" + functions + """
(async()=>{
 const action=serviceAction('bridge','restart');
 document.hidden=true;resumeServices();
 assert.equal(serviceSnapshot,null);assert.match(title.textContent,/UNKNOWN/);
 resolveAction({old:'response'});await action;
 assert.deepEqual(rendered,[]);assert.equal(pendingRestart,true);
 document.hidden=false;serviceSnapshot={};serviceUpdatedAt=Date.now();
 api=async()=>{throw new Error('hung request timed out')};
 await pollServices();assert.equal(serviceSnapshot,null);assert.equal(servicePolling,false);
 assert.match(title.textContent,/UNKNOWN/);
})().catch(error=>{console.error(error);process.exit(1)});
"""
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
