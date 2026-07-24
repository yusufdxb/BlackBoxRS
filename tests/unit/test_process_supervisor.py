import os
import signal
import subprocess
import sys
import time


def test_supervisor_terminates_owned_group_when_owner_dies():
    code = (
        "import os,subprocess,sys,time; "
        "env=os.environ.copy(); env['BLACKBOXRS_OWNER_PID']=str(os.getpid()); "
        "p=subprocess.Popen([sys.executable,'-m','blackboxrs.prevention.process_supervisor','--',"
        "sys.executable,'-c','import time; time.sleep(30)'],start_new_session=True,env=env); "
        "print(p.pid,flush=True); time.sleep(30)"
    )
    owner = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert owner.stdout is not None
    wrapper_pid = int(owner.stdout.readline().strip())
    time.sleep(0.2)
    os.kill(owner.pid, signal.SIGKILL)
    owner.wait(timeout=2)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(wrapper_pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError("owned supervisor survived owner death")
